# -*- coding: utf-8 -*-
"""
Многоисточниковый сбор тендеров.

Каждый источник — функция collect_*(max_pages) -> list[dict]. Словарь одного
тендера приводится к единому виду (см. _norm_item). collect_all() запускает все
включённые источники и убирает дубли между ними по сигнатуре (название+заказчик).

Рабочий источник — rostender (через существующий rostender_parser.scrape).
Остальные (СберА/АСТ, Контур.Закупки, порталы крупных компаний) — заготовки:
возвращают [], пока не подключены реальные эндпоинты/ключи. Это места, куда
команда с доступом впишет интеграцию; всё остальное (дедуп, фильтр по выручке,
скоринг, обогащение) уже работает.
"""
from __future__ import annotations
import dataclasses
import re


def _to_dict(obj) -> dict:
    if isinstance(obj, dict):
        return dict(obj)
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)
    return {}


def _norm_item(d: dict, source: str) -> dict:
    """Единый вид записи тендера от любого источника."""
    d = dict(d)
    d.setdefault("source", source)
    # обязательные поля (могут отсутствовать у некоторых источников)
    for k in ("tender_id", "number", "title", "url", "subject", "customer",
              "region", "location", "category", "price_rub", "price_display",
              "published_at", "deadline"):
        d.setdefault(k, None)
    if not d.get("tender_id"):
        # синтетический id из источника и номера/URL, чтобы не терять запись
        base = d.get("number") or d.get("url") or d.get("title") or ""
        d["tender_id"] = f"{source}:{base}"[:120]
    return d


# ---------------------------------------------------------------------------
# Источник 1 — rostender.info (рабочий, через существующий парсер проекта)
# ---------------------------------------------------------------------------
def collect_rostender(max_pages: int = 50, known_ids=None) -> list[dict]:
    try:
        import rostender_parser
    except ImportError:
        return []
    try:
        # инкрементально: парсер сам остановит категорию, дойдя до уже известных
        raw = rostender_parser.scrape(max_pages=max_pages, known_ids=known_ids)
    except TypeError:
        try:
            raw = rostender_parser.scrape(max_pages=max_pages)
        except TypeError:
            raw = rostender_parser.scrape()
    return [_norm_item(_to_dict(t), "rostender.info") for t in (raw or [])]


# ---------------------------------------------------------------------------
# Источник 2 — СберА / АСТ ГОЗ (заготовка)
# ---------------------------------------------------------------------------
def collect_sber_ast(max_pages: int = 50, known_ids=None) -> list[dict]:
    """Тендеры со Сбер А / электронных площадок Сбера.

    TODO: у площадки своя авторизация/поиск. Здесь нужно:
      1) выполнить поиск по интересующим ОКПД/ключевым словам,
      2) распарсить карточки в словари полей (см. _norm_item),
      3) вернуть список _norm_item(item, "sber-ast").
    Пока источник отключён (возвращает пустой список).
    """
    return []


# ---------------------------------------------------------------------------
# Источник 3 — Контур.Закупки (заготовка, платный API)
# ---------------------------------------------------------------------------
def collect_kontur(max_pages: int = 50, known_ids=None) -> list[dict]:
    """Контур.Закупки — платный сервис, нужен доступ/API-ключ.

    TODO: подключить их API (ключ хранить в переменной окружения),
    получить тендеры по фильтрам и вернуть _norm_item(item, "kontur").
    Пока отключён.
    """
    return []


# ---------------------------------------------------------------------------
# Источник 4 — порталы закупок крупных компаний (заготовка)
# ---------------------------------------------------------------------------
# Компании (>=10 млрд ₽), публикующие тендеры у себя. Для каждой — свой парсер.
COMPANY_PORTALS = {
    # "sberbank": "https://zakupki.sber.ru/ ... ",
    # "gazprom":  "https://zakupki.gazprom.ru/ ... ",
    # "rzd":      "https://zakupki.rzd.ru/ ... ",
    # "rosseti":  "https://zakupki.rosseti.ru/ ... ",
}


def collect_company_portals(max_pages: int = 50, known_ids=None) -> list[dict]:
    """Обход собственных порталов закупок крупных компаний (Сбер, Газпром, РЖД…).

    TODO: у каждого портала своя структура — добавить парсер на компанию в
    COMPANY_PORTALS и собирать здесь. Пока отключён.
    """
    return []


# ---------------------------------------------------------------------------
# Реестр источников
# ---------------------------------------------------------------------------
# (имя, включён, функция). Включай источник, когда его парсер готов.
SOURCES = [
    ("rostender.info", True, collect_rostender),
    ("sber-ast", False, collect_sber_ast),
    ("kontur", False, collect_kontur),
    ("company-portals", False, collect_company_portals),
]


def _signature(t: dict) -> str:
    """Ключ для дедупликации между источниками: заказчик + название."""
    def norm(s):
        return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (s or "").lower())).strip()
    num = norm(t.get("number"))
    if num:
        return "num:" + num
    return "tc:" + norm(t.get("customer")) + "|" + norm(t.get("title"))


def dedupe(items: list[dict]) -> list[dict]:
    """Убирает дубли между источниками (оставляет первую встреченную запись)."""
    seen = {}
    for t in items:
        sig = _signature(t)
        if sig not in seen:
            seen[sig] = t
    return list(seen.values())


def collect_all(max_pages: int = 50, known_ids=None) -> dict:
    """Собирает включённые источники. Возвращает {'items': [...], 'per_source': {...}}."""
    all_items: list[dict] = []
    per_source: dict[str, int] = {}
    for name, enabled, func in SOURCES:
        if not enabled:
            per_source[name] = 0
            continue
        try:
            got = func(max_pages, known_ids) or []
        except Exception as e:  # noqa: BLE001
            per_source[name] = f"ошибка: {e}"
            continue
        per_source[name] = len(got)
        all_items.extend(got)
    before = len(all_items)
    all_items = dedupe(all_items)
    return {"items": all_items, "per_source": per_source,
            "collected": before, "after_dedupe": len(all_items)}