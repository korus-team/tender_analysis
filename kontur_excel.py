# -*- coding: utf-8 -*-
"""Импорт выгрузок Контур.Закупок в общую модель тендеров приложения."""

from __future__ import annotations

import hashlib
import json
import re
from zipfile import BadZipFile
from datetime import date, datetime
from pathlib import PurePosixPath
from urllib.parse import parse_qs, unquote, urlparse

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

import company_size
import directions
import storage
from icp_config import load_icp
from scoring import score_tender, score_tender_llm


SOURCE_NAME = "kontur-excel"
MAX_ROWS = 100_000
MAX_COLUMNS = 100


class KonturExcelError(ValueError):
    """Понятная пользователю ошибка структуры Excel-выгрузки."""


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _iso(value) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time()).isoformat(timespec="seconds")
    text = _text(value)
    if not text:
        return None
    for fmt in (None, "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            parsed = datetime.fromisoformat(text) if fmt is None else datetime.strptime(text, fmt)
            return parsed.isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


def _days_left(deadline: str | None, now: datetime | None = None) -> int | None:
    if not deadline:
        return None
    try:
        return (datetime.fromisoformat(deadline) - (now or datetime.now())).days
    except (TypeError, ValueError):
        return None


def _deadline_disposition(tender: dict, now: datetime) -> str | None:
    """Причина удаления по сроку или None, если тендер нужно оставить."""
    deadline = tender.get("deadline")
    if not deadline:
        return None
    try:
        remaining = datetime.fromisoformat(deadline) - now
    except (TypeError, ValueError):
        return None
    if remaining.total_seconds() < 0:
        return "expired"
    if remaining.days <= 3 and directions.classify(tender) != "license":
        return "short_non_license"
    return None


def _number(value) -> int | float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return value
    cleaned = re.sub(r"[^\d,.-]", "", str(value)).replace(",", ".")
    try:
        result = float(cleaned)
    except ValueError:
        return None
    return int(result) if result.is_integer() else result


def _direct_url(link: str | None) -> str | None:
    """Достаёт постоянный URL из kontur redirect-ссылки с временным token."""
    link = _text(link)
    if not link:
        return None
    try:
        parsed = urlparse(link)
        target = parse_qs(parsed.query).get("url", [None])[0]
        return unquote(target) if target else link
    except (TypeError, ValueError):
        return link


def _hyperlink(cell) -> str | None:
    link = getattr(cell, "hyperlink", None)
    return _direct_url(getattr(link, "target", None)) if link else None


def _stable_id(kontur_url: str | None, number: str, etp: str,
               customer_inn: str, title: str) -> str:
    if kontur_url:
        path = PurePosixPath(urlparse(kontur_url).path)
        slug = path.name.strip()
        if slug:
            return f"kontur:{slug}"[:120]
    raw = "|".join((number, etp, customer_inn, title))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"kontur:{number or digest}:{digest}"[:120]


def _price_display(amount, currency: str) -> str | None:
    if amount is None:
        return None
    suffix = "₽" if currency.upper() in ("", "RUB", "RUR", "РУБ") else currency
    if isinstance(amount, float) and not amount.is_integer():
        shown = f"{amount:,.2f}".replace(",", " ")
    else:
        shown = f"{int(amount):,}".replace(",", " ")
    return f"{shown} {suffix}".strip()


def _header_indexes(ws, header_row: int) -> dict[str, int]:
    headers = [_text(ws.cell(header_row, col).value) for col in range(1, ws.max_column + 1)]

    def one(name: str, occurrence: int = 0, required: bool = False) -> int | None:
        found = [i + 1 for i, value in enumerate(headers) if value == name]
        if len(found) > occurrence:
            return found[occurrence]
        if required:
            raise KonturExcelError(f"В выгрузке не найден обязательный столбец «{name}».")
        return None

    return {
        "number": one("Номер", required=True),
        "title": one("Название", 0, required=True),
        "price": one("НМЦ", required=False),
        "bid_security": one("Обеспечение заявки", required=False),
        "contract_security": one("Обеспечение контракта", required=False),
        "advance": one("Аванс", required=False),
        "currency": one("Валюта закупки", required=False),
        "published": one("Дата публикации", required=True),
        "deadline": one("Окончание приема заявок", required=True),
        "selection_date": one("Проведение отбора", required=False),
        "stage": one("Этап отбора", required=False),
        "trade_type": one("Тип торгов", required=False),
        "eis_url": one("Ссылка на ЕИС", required=False),
        "method": one("Способ отбора", required=False),
        "etp": one("ЭТП", required=False),
        "smp": one("СМП, СОНО", required=False),
        "tag": one("Метка", required=False) or one("Метка ", required=False),
        "comment": one("Комментарий", required=False),
        "responsible": one("Ответственный", required=False),
        "region": one("Регион", required=False),
        "customer": one("Название", 1, required=True),
        "inn": one("ИНН", 0, required=False),
        "kpp": one("КПП", 0, required=False),
        "location": one("Место поставки", required=False),
        "location_docs": one("Место поставки из документов", required=False),
    }


def _find_header_row(ws) -> int:
    for row in range(1, min(ws.max_row, 10) + 1):
        values = {_text(ws.cell(row, col).value) for col in range(1, ws.max_column + 1)}
        if {"Номер", "Окончание приема заявок", "ИНН"}.issubset(values):
            return row
    raise KonturExcelError(
        "Не удалось найти заголовки Контур.Закупок. Нужна исходная выгрузка в формате .xlsx."
    )


def parse_kontur_xlsx(source) -> dict:
    """Читает путь или бинарный поток и возвращает нормализованные записи."""
    try:
        workbook = load_workbook(source, read_only=False, data_only=True)
    except (BadZipFile, InvalidFileException, OSError, ValueError) as exc:
        raise KonturExcelError("Файл не удалось прочитать как Excel (.xlsx).") from exc

    try:
        if not workbook.sheetnames:
            raise KonturExcelError("В Excel-файле нет листов.")
        ws = workbook[workbook.sheetnames[0]]
        if ws.max_row > MAX_ROWS or ws.max_column > MAX_COLUMNS:
            raise KonturExcelError("Выгрузка слишком большая для безопасного импорта.")

        header_row = _find_header_row(ws)
        columns = _header_indexes(ws, header_row)
        records: list[dict] = []
        skipped_blank = 0

        for row_number in range(header_row + 1, ws.max_row + 1):
            def value(key: str):
                col = columns.get(key)
                return ws.cell(row_number, col).value if col else None

            number = _text(value("number"))
            title = _text(value("title"))
            if not number and not title:
                skipped_blank += 1
                continue
            if not number or not title:
                skipped_blank += 1
                continue

            customer = _text(value("customer")) or None
            customer_inn = _text(value("inn"))
            etp = _text(value("etp"))
            number_cell = ws.cell(row_number, columns["number"])
            kontur_url = _hyperlink(number_cell)
            etp_cell = ws.cell(row_number, columns["etp"]) if columns.get("etp") else None
            etp_url = _hyperlink(etp_cell) if etp_cell else None
            eis_value = value("eis_url")
            eis_cell = ws.cell(row_number, columns["eis_url"]) if columns.get("eis_url") else None
            eis_url = _hyperlink(eis_cell) if eis_cell else _text(eis_value) or None

            amount = _number(value("price"))
            currency = _text(value("currency"))
            rub_amount = amount if currency.upper() in ("", "RUB", "RUR", "РУБ") else None
            published = _iso(value("published"))
            deadline = _iso(value("deadline"))
            location = _text(value("location_docs")) or _text(value("location")) or None
            trade_type = _text(value("trade_type"))
            method = _text(value("method"))

            details = {
                "provider": "Контур.Закупки",
                "kontur_url": kontur_url,
                "etp": etp or None,
                "etp_url": etp_url,
                "customer_inn": customer_inn or None,
                "customer_kpp": _text(value("kpp")) or None,
                "trade_type": trade_type or None,
                "selection_method": method or None,
                "selection_stage": _text(value("stage")) or None,
                "selection_date": _iso(value("selection_date")),
                "smp_sono": _text(value("smp")) or None,
                "eis_url": eis_url,
                "kontur_tag": _text(value("tag")) or None,
                "kontur_comment": _text(value("comment")) or None,
                "kontur_responsible": _text(value("responsible")) or None,
                "bid_security_amount": _number(value("bid_security")),
                "contract_security_amount": _number(value("contract_security")),
                "advance": _number(value("advance")),
            }
            details = {key: val for key, val in details.items() if val is not None}

            records.append({
                "tender_id": _stable_id(kontur_url, number, etp, customer_inn, title),
                "number": number,
                "title": title,
                "url": kontur_url or etp_url,
                "subject": title,
                "customer": customer,
                "region": _text(value("region")) or None,
                "location": location,
                "category": trade_type or method or "Закупка",
                "price_rub": rub_amount,
                "price_display": _price_display(amount, currency),
                "published_at": published,
                "deadline": deadline,
                # В Excel Контур эти поля содержат суммы, а текущая схема ожидает проценты.
                # Суммы сохранены в details, чтобы не показывать их как ошибочные проценты.
                "contract_security_pct": None,
                "bid_security_pct": None,
                "source": SOURCE_NAME,
                "_details": details,
            })

        if not records:
            raise KonturExcelError("В выгрузке не найдено ни одного тендера с номером и названием.")
        return {
            "items": records,
            "sheet": ws.title,
            "rows": ws.max_row - header_row,
            "skipped_blank": skipped_blank,
        }
    finally:
        workbook.close()


def import_kontur_xlsx(source, conn=None, icp: dict | None = None,
                       now: datetime | None = None, use_llm: bool = False) -> dict:
    """Импортирует Excel, применяя текущий фильтр компаний и скоринг приложения."""
    parsed = parse_kontur_xlsx(source)
    icp = icp or load_icp()
    own_connection = conn is None
    conn = conn or storage.connect()
    now = now or datetime.now()
    llm_scorer = None
    if use_llm:
        from LLM_scoring import OpenAITenderScorer

        llm_scorer = OpenAITenderScorer()
    kept: list[dict] = []
    skipped_small = 0
    skipped_expired = 0
    skipped_short = 0
    purge_ids: set[str] = set()

    try:
        # Чистим ранее загруженные из Контура записи по тому же правилу, даже
        # если их уже нет в свежей выгрузке.
        for existing in storage.query_tenders(conn, limit=None):
            if existing.get("source") != SOURCE_NAME:
                continue
            if _deadline_disposition(existing, now):
                purge_ids.add(existing["tender_id"])

        for source_item in parsed["items"]:
            item = dict(source_item)
            details = item.pop("_details", {})
            if not company_size.passes_revenue(item.get("customer")):
                skipped_small += 1
                purge_ids.add(item["tender_id"])
                continue
            disposition = _deadline_disposition(item, now)
            if disposition == "expired":
                skipped_expired += 1
                purge_ids.add(item["tender_id"])
                continue
            if disposition == "short_non_license":
                skipped_short += 1
                purge_ids.add(item["tender_id"])
                continue

            item["days_left"] = _days_left(item.get("deadline"), now)
            result = (
                score_tender_llm(item, icp, scorer=llm_scorer)
                if llm_scorer is not None
                else score_tender(item, icp)
            )
            item["score"] = result.score
            item["verdict"] = result.verdict
            revenue = company_size.annual_revenue_rub(item.get("customer"))
            if revenue is not None:
                revenue_reason = "Оборот заказчика не ниже 10 млрд ₽ — основной критерий выполнен"
                revenue_label = "оборот подтверждён"
            else:
                revenue_reason = "Оборот заказчика нужно подтвердить: основной порог — 10 млрд ₽"
                revenue_label = "оборот не подтверждён"
            item["reasons"] = [revenue_reason, *result.reasons]
            item["labels"] = [revenue_label, *result.labels]
            item["_details"] = details
            kept.append(item)

        removed = storage.delete_tenders(conn, purge_ids, commit=False)
        to_save = [{key: val for key, val in item.items() if key != "_details"} for item in kept]
        saved = storage.save_scored(conn, to_save) if to_save else {"new": [], "updated": []}
        imported_at = now.isoformat(timespec="seconds")
        for item in kept:
            conn.execute(
                "UPDATE tenders SET details = ?, enriched_at = ? WHERE tender_id = ?",
                (json.dumps(item["_details"], ensure_ascii=False), imported_at, item["tender_id"]),
            )
        conn.commit()
        return {
            "sheet": parsed["sheet"],
            "rows": parsed["rows"],
            "parsed": len(parsed["items"]),
            "skipped_blank": parsed["skipped_blank"],
            "skipped_small_company": skipped_small,
            "skipped_expired": skipped_expired,
            "skipped_short_non_license": skipped_short,
            "removed_by_deadline": removed,
            "kept": len(kept),
            "new": len(saved.get("new", [])),
            "new_ids": list(saved.get("new", [])),
            "updated": len(saved.get("updated", [])),
        }
    finally:
        if own_connection:
            conn.close()
