# -*- coding: utf-8 -*-
"""
Обогащение тендеров с детальной страницы (ТЗ 9.3): для каждого тендера заходим
на его страницу и достаём то, чего нет в списке:
  - ссылки на документы (обоснование НМЦК, описание объекта закупки, проект
    контракта и т.д.) — файлы на files.rostender.info, их можно открыть/скачать;
  - начальную цену и аванс (иногда заполняет пропуски там, где в списке было «— ₽»).

Чего НЕ достаём:
  - имя заказчика — rostender скрывает его заглушкой (░) и на детальной странице
    тоже, для анонимного доступа. Вернуть можно только через авторизованный сбор
    (если появится аккаунт) — это отдельная задача.
  - полный текст описания — он лежит внутри файла «Описание объекта закупки», а не
    на странице. Скачивание и разбор файлов — возможный следующий шаг.

Стоимость: один запрос на тендер. Поэтому обогащаем ВЫБОРОЧНО — сначала самые
релевантные (высокий балл) и только те, что ещё не обогащались, с паузами.

Запуск в PyCharm: правой кнопкой -> Run 'enrich'. Настрой LIMIT/MIN_SCORE ниже.
Селекторы грунтованы под реальную страницу; если структура где-то отличается —
пришли вывод, подстроим (как со списком).
"""

from __future__ import annotations
import re
import time

import requests
from bs4 import BeautifulSoup

from rostender_parser import HEADERS, REQUEST_DELAY_SEC, fetch_page, _clean
import storage

# --- Настройки прогона ---
LIMIT = 20          # сколько тендеров обогатить за один запуск
MIN_SCORE = 50      # обогащать только тендеры с баллом не ниже (экономим запросы)

FILES_PREFIX = "https://files.rostender.info/"
RE_PRICE = re.compile(r"Начальная цена\s*([\d\s\u00a0]+)\s*₽")
RE_ADVANCE = re.compile(r"Аванс:\s*(\d+)\s*%")
RE_SIZE = re.compile(r"\s*(\d+(?:[.,]\d+)?)\s*(?:КБ|МБ|Кб|Мб|KB|MB)\s*$", re.IGNORECASE)

# Номер закупки в ЕИС (госзакупки) — по нему документы доступны БЕСПЛАТНО на
# официальном сайте zakupki.gov.ru, даже если rostender их прячет за регистрацией.
RE_EIS = re.compile(r"Закупка:\s*(\d{11,})")
RE_CONTRACT_TERM = re.compile(r"Срок исполнения контракта[^\d]{0,40}?(\d{2}\.\d{2}\.\d{4})")
# Способ размещения ищем по ключевым словам (надёжнее, чем разбирать вёрстку).
PLACEMENT_KEYWORDS = [
    ("Электронный аукцион", "электронный аукцион"),
    ("Открытый конкурс", "открытый конкурс"),
    ("Запрос котировок", "запрос котировок"),
    ("Запрос предложений", "запрос предложений"),
    ("Закупка у единственного поставщика", "единственн"),
]


def _eis_url(number: str) -> str:
    """Ссылка на официальную страницу закупки в ЕИС (там документы бесплатны)."""
    return ("https://zakupki.gov.ru/epz/order/extendedsearch/results.html"
            f"?searchString={number}")


def parse_detail(html: str) -> dict:
    """Разбирает HTML детальной страницы -> документы, цена, аванс. Без сети (тестируемо)."""
    soup = BeautifulSoup(html, "lxml")
    text = _clean(soup.get_text(" "))

    # --- документы: ссылки на files.rostender.info ---
    documents, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a.get("href")
        if not isinstance(href, str) or not href.startswith(FILES_PREFIX) or href in seen:
            continue
        label = _clean(a.get_text())
        # пропускаем технические дубли, где текст ссылки — сам URL
        if not label or label.lower().startswith("http"):
            continue
        seen.add(href)
        name, size = label, None
        if (m := RE_SIZE.search(label)):
            size = m.group(0).strip()
            name = label[:m.start()].strip()
        documents.append({"name": name, "size": size, "url": href})

    # --- цена и аванс ---
    price_rub = None
    if (m := RE_PRICE.search(text)):
        digits = re.sub(r"[^\d]", "", m.group(1))
        price_rub = int(digits) if digits else None
    advance_pct = None
    if (m := RE_ADVANCE.search(text)):
        advance_pct = int(m.group(1))

    # --- прочие полезные поля (в т.ч. номер ЕИС -> ссылка на бесплатные документы) ---
    details: dict = {}
    if (m := RE_EIS.search(text)):
        details["eis_number"] = m.group(1)
        details["eis_url"] = _eis_url(m.group(1))
    if (m := RE_CONTRACT_TERM.search(text)):
        details["contract_term"] = m.group(1)          # срок исполнения контракта
    if "СМП" in text or "субъектов малого предпринимательства" in text.lower():
        details["sme_only"] = True                     # преимущество/ограничение для малого бизнеса
    low = text.lower()
    for label, kw in PLACEMENT_KEYWORDS:
        if kw in low:
            details["placement"] = label               # способ размещения
            break

    return {"documents": documents, "price_rub": price_rub,
            "advance_pct": advance_pct, "details": details}


def enrich_tender(url: str, session: requests.Session) -> dict | None:
    """Загружает страницу тендера и разбирает её. None — если не загрузилась."""
    html = fetch_page(url, session)          # вежливый запрос с повторами (из парсера)
    if not html:
        return None
    return parse_detail(html)


def enrich_pending(conn, limit: int = LIMIT, min_score: int = MIN_SCORE,
                   delay: float = REQUEST_DELAY_SEC) -> dict:
    """
    Обогащает ещё не обогащённые тендеры (по убыванию балла) с паузами.
    Возвращает {'attempted', 'done', 'failed'} для честного отчёта.
    """
    rows = storage.tenders_to_enrich(conn, limit=limit, min_score=min_score)
    session = requests.Session()
    session.headers.update(HEADERS)

    attempted = done = failed = 0
    for r in rows:
        url = r.get("url")
        if not url:
            continue
        attempted += 1
        print(f"[enrich] {r['tender_id']} {url}")
        data = enrich_tender(url, session)
        if data is None:
            failed += 1
            print("  страница не загрузилась, пропускаю.")
            continue
        storage.save_enrichment(conn, r["tender_id"], data)
        det = data.get("details", {})
        print(f"  документов: {len(data['documents'])} | цена: {data['price_rub']} | "
              f"ЕИС: {det.get('eis_number', '—')} | СМП: {'да' if det.get('sme_only') else 'нет'}")
        done += 1
        time.sleep(delay)

    print(f"\nОбогащено: {done} из {attempted} (не удалось: {failed})")
    return {"attempted": attempted, "done": done, "failed": failed}


if __name__ == "__main__":
    conn = storage.connect()
    enrich_pending(conn)
    conn.close()