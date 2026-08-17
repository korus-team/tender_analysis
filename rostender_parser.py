# -*- coding: utf-8 -*-
"""
Парсер тендеров с rostender.info.

Что делает:
  1. Скачивает страницы категории (по расписанию/по кнопке) с вежливыми задержками.
  2. Из каждой карточки тендера достаёт структурированные поля.
  3. Нормализует их (даты -> ISO, цена -> число в рублях) и складывает в список словарей.
  4. Убирает дубликаты по номеру тендера.
  5. Сохраняет всё в JSON.

Важно про надёжность:
  Извлечение полей построено на РЕГУЛЯРНЫХ текстовых подписях, которые есть в
  разметке сайта ("Предмет тендера: ... Цена: ... руб.", дедлайн в ISO и т.д.),
  а не на конкретных CSS-классах. Так парсер меньше ломается при смене вёрстки.
  Единственное, что стоит проверить на живом сайте, — функция find_cards():
  как именно сгруппированы карточки в DOM. Она уже умеет несколько стратегий
  и в крайнем случае сама находит карточки по ссылкам-заголовкам.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup, Tag

# --------------------------------------------------------------------------- #
#  Настройки
# --------------------------------------------------------------------------- #

BASE_URL = "https://rostender.info"

# Источники (категории rostender). Для СЕРВИСНОЙ ИТ-компании берём разделы про
# услуги/разработку, а не общий «IT», который в основном про ПОСТАВКУ товаров и
# перепродажу лицензий. Это ровно вопрос выбора источников из ТЗ (раздел 20.3).
# Парсер и структура карточек одинаковы для всех разделов — можно добавлять свои.
CATEGORY_PATHS = [
    "/category/tendery-na-razrabotku-po",                  # Разработка ПО
    "/tendery-uslugi-v-oblasti-programmirovaniya",         # Услуги в области программирования
    "/category/tendery-razrabotka-informacionnoj-sistemy", # Разработка информационной системы
    # "/category/tendery-v-oblasti-it",  # общий IT — в основном поставка товаров/лицензий, для сервисной компании шумно
]

# Вежливость к сайту: реалистичный User-Agent, пауза между запросами, таймаут.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}
REQUEST_DELAY_SEC = 1.5      # пауза между страницами
REQUEST_TIMEOUT_SEC = 20
MAX_RETRIES = 3

# --------------------------------------------------------------------------- #
#  Регулярные выражения под подписи сайта
# --------------------------------------------------------------------------- #

# Ссылка на карточку тендера бывает двух видов:
#   /region/<...>/<id>-tender-<slug>   и   /tender/<id>
DETAIL_HREF_RE = re.compile(r"rostender\.info/(?:[\w\-/]+/)?\d{5,}-tender-|rostender\.info/tender/\d{5,}")

RE_TENDER_ID   = re.compile(r"/(\d{5,})-tender-|/tender/(\d{5,})")
RE_NUMBER      = re.compile(r"№\s*(\d+)\s+от\s+(\d{2}\.\d{2}\.\d{2})")
RE_DEADLINE_HU = re.compile(r"Окончание\s*\(МСК\)\s*(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2})")
RE_ISO_LINE    = re.compile(
    r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*:\s*"      # 1: дата публикации
    r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*:\s*"      # 2: дедлайн
    r"(.+?)\s+at\s+(.+?),\s*(.+?)\s*,\s*Russia,\s*RU"      # 3: заголовок, 4: место, 5: регион(ы)
)
RE_SUBJECT_PRICE = re.compile(r"Предмет тендера:\s*(.+?)\.\s*Цена:\s*(\d+)\s*руб\.")
RE_CUSTOMER      = re.compile(r"Заказчик\s+(.+?)\s+Окончание\s*\(МСК\)")
RE_CONTRACT_SEC  = re.compile(r"Обеспечение контракта:\s*([\d.]+)\s*%")
RE_BID_SEC       = re.compile(r"Обеспечение заявки:\s*([\d.]+)\s*%")
RE_PRICE_DISPLAY = re.compile(r"Начальная цена\s*([—\-]|[\d\s\u00a0]+(?:₽|USD|\$|руб))")


# --------------------------------------------------------------------------- #
#  Модель данных одного тендера
# --------------------------------------------------------------------------- #

@dataclass
class Tender:
    tender_id: str
    number: Optional[str] = None
    title: Optional[str] = None
    url: Optional[str] = None
    subject: Optional[str] = None
    customer: Optional[str] = None
    region: Optional[str] = None
    location: Optional[str] = None
    category: Optional[str] = None
    price_rub: Optional[int] = None          # цена в рублях (сайт уже нормализует валюту)
    price_display: Optional[str] = None      # как показано на сайте ("42 625 ₽", "17 035 USD", "—")
    published_at: Optional[str] = None       # ISO
    deadline: Optional[str] = None           # ISO
    days_left: Optional[int] = None
    contract_security_pct: Optional[float] = None
    bid_security_pct: Optional[float] = None
    source: str = "rostender.info"
    parsed_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


# --------------------------------------------------------------------------- #
#  Вспомогательные функции
# --------------------------------------------------------------------------- #

# Блочные символы-заглушки (░ ▒ ▓ █ и полублоки), которыми сайт скрывает поля
# (например, заказчика) от анонимных запросов. Считаем их отсутствием данных.
_BLOCK_RE = re.compile(r"[\u2580-\u259F]+")


def _clean(text: str) -> str:
    """Убирает символы-заглушки и схлопывает пробелы/неразрывные пробелы."""
    text = _BLOCK_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text.replace("\u00a0", " ")).strip()


def extract_tender_id(href: str) -> Optional[str]:
    m = RE_TENDER_ID.search(href or "")
    if not m:
        return None
    return m.group(1) or m.group(2)


def _is_iso_text(s: str) -> bool:
    """True, если текст ссылки — служебная ISO-строка (начинается с даты YYYY-MM-DD HH:MM:SS)."""
    return bool(re.match(r"\s*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", s or ""))


def _to_iso(date_str: str, fmt: str) -> Optional[str]:
    try:
        return datetime.strptime(date_str.strip(), fmt).isoformat()
    except ValueError:
        return None


def _price_display_to_number(token: str) -> Optional[int]:
    """'42 625 ₽' -> 42625. Валюты кроме рубля не пересчитываем (для этого есть price_rub)."""
    if not token or token.strip() in {"—", "-"}:
        return None
    digits = re.sub(r"[^\d]", "", token)
    return int(digits) if digits else None


# --------------------------------------------------------------------------- #
#  Поиск карточек в DOM (единственное место, зависящее от структуры страницы)
# --------------------------------------------------------------------------- #

def _looks_like_card(el: Tag) -> bool:
    txt = el.get_text(" ", strip=True)
    has_link = el.find("a", href=DETAIL_HREF_RE) is not None
    return has_link and ("Предмет тендера" in txt or "Окончание (МСК)" in txt)


def _card_scope(title_link: Tag) -> Tag:
    """
    По ссылке-заголовку поднимается вверх до наибольшего контейнера,
    который всё ещё относится к ОДНОМУ тендеру (не захватывает соседний).
    """
    node = title_link
    best = title_link.parent or title_link
    for _ in range(10):
        parent = node.parent
        if parent is None:
            break
        title_links_inside = [
            a for a in parent.find_all("a", href=DETAIL_HREF_RE) if a.get("title")
        ]
        if len(title_links_inside) >= 2:
            # поднялись бы уже в соседнюю карточку — останавливаемся
            return best
        best = parent
        if "Предмет тендера" in parent.get_text(" ", strip=True):
            return parent
        node = parent
    return best


def find_cards(soup: BeautifulSoup) -> list[Tag]:
    """
    Возвращает список DOM-элементов-карточек.

    Стратегия 1 — известные контейнеры (проверь селекторы на живом сайте и
    при необходимости поправь список ниже).
    Стратегия 2 (запасная, работает без знания классов) — сгруппировать по
    главным ссылкам-заголовкам: у них есть атрибут title="Тендер на ...".
    """
    candidate_selectors = [
        "article.tender-row",
        "div.tender-row",
        "div.tender-listing-item",
        "div.tender__item",
        "article",
    ]
    for sel in candidate_selectors:
        cards = [el for el in soup.select(sel) if _looks_like_card(el)]
        if len(cards) >= 3:
            return cards

    # --- запасная стратегия ---
    title_links = [
        a for a in soup.find_all("a", href=DETAIL_HREF_RE)
        if a.get("title", "").startswith("Тендер")
        and not a.get("title", "").startswith("Тендеры")
        and not _is_iso_text(a.get_text())
    ]
    seen_ids, cards = set(), []
    for link in title_links:
        tid = extract_tender_id(link.get("href", ""))
        if not tid or tid in seen_ids:
            continue
        seen_ids.add(tid)
        cards.append(_card_scope(link))
    return cards


# --------------------------------------------------------------------------- #
#  Разбор одной карточки
# --------------------------------------------------------------------------- #

def parse_card(card: Tag) -> Optional[Tender]:
    text = card.get_text(" ", strip=True)
    text = _clean(text)

    # Главная ссылка-заголовок.
    # Важно: у карточки есть служебная ссылка с ISO-строкой на тот же URL —
    # её нельзя брать как заголовок. Отсекаем такие по тексту.
    candidates = card.find_all("a", href=DETAIL_HREF_RE)
    title_link = None
    # 1) ссылка с title="Тендер ..." и НЕ служебным (ISO) текстом
    for a in candidates:
        ttl = a.get("title", "")
        if ttl.startswith("Тендер") and not ttl.startswith("Тендеры") and not _is_iso_text(a.get_text()):
            title_link = a
            break
    # 2) запас: первая ссылка на карточку с непустым НЕ-ISO текстом
    if title_link is None:
        for a in candidates:
            if a.get_text(strip=True) and not _is_iso_text(a.get_text()):
                title_link = a
                break
    # 3) совсем запас
    if title_link is None:
        title_link = candidates[0] if candidates else None
    if title_link is None:
        return None

    href = title_link.get("href", "")
    tender_id = extract_tender_id(href)
    if not tender_id:
        return None

    t = Tender(tender_id=tender_id)
    t.url = href if href.startswith("http") else BASE_URL + href
    t.title = _clean(title_link.get_text())

    # Номер и дата
    if (m := RE_NUMBER.search(text)):
        t.number = m.group(1)

    # ISO-строка: даты, место, регион (самый надёжный источник)
    if (m := RE_ISO_LINE.search(text)):
        t.published_at = _to_iso(m.group(1), "%Y-%m-%d %H:%M:%S")
        t.deadline = _to_iso(m.group(2), "%Y-%m-%d %H:%M:%S")
        t.location = _clean(m.group(4))
        t.region = _clean(m.group(5))

    # Запасной вариант дедлайна из человекочитаемой строки
    if not t.deadline and (m := RE_DEADLINE_HU.search(text)):
        t.deadline = _to_iso(f"{m.group(1)} {m.group(2)}", "%d.%m.%Y %H:%M")

    # Предмет + цена в рублях
    if (m := RE_SUBJECT_PRICE.search(text)):
        t.subject = _clean(m.group(1))
        t.price_rub = int(m.group(2)) or None
    if not t.subject:
        t.subject = t.title

    # Страховка: если в заголовок всё же попала служебная ISO-строка — берём предмет
    if _is_iso_text(t.title or ""):
        t.title = t.subject or t.title

    # Отображаемая цена (как на сайте)
    if (m := RE_PRICE_DISPLAY.search(text)):
        t.price_display = _clean(m.group(1))
        if t.price_rub is None:
            t.price_rub = _price_display_to_number(t.price_display)

    # Заказчик (может быть скрыт заглушкой -> тогда None)
    if (m := RE_CUSTOMER.search(text)):
        t.customer = _clean(m.group(1)) or None

    # Обеспечение
    if (m := RE_CONTRACT_SEC.search(text)):
        t.contract_security_pct = float(m.group(1))
    if (m := RE_BID_SEC.search(text)):
        t.bid_security_pct = float(m.group(1))

    # Категория: ссылка вида /tendery-...  (не /region/...)
    for a in card.find_all("a", href=True):
        if re.search(r"/tendery-[\w\-]+", a["href"]):
            t.category = _clean(a.get_text())
            break

    # Регион запасным способом — из ссылки /region/...
    if not t.region:
        for a in card.find_all("a", href=True):
            if "/region/" in a["href"] and a.get_text(strip=True):
                t.region = _clean(a.get_text())
                break

    # Сколько дней до дедлайна
    if t.deadline:
        try:
            t.days_left = (datetime.fromisoformat(t.deadline) - datetime.now()).days
        except ValueError:
            pass

    return t


def parse_listing(html: str) -> list[Tender]:
    soup = BeautifulSoup(html, "lxml")
    cards = find_cards(soup)
    out: list[Tender] = []
    for card in cards:
        try:
            tender = parse_card(card)
            if tender:
                out.append(tender)
        except Exception as exc:  # одна битая карточка не должна ронять весь парсинг
            print(f"  [warn] карточка не разобрана: {exc}")
    return out


# --------------------------------------------------------------------------- #
#  Сеть
# --------------------------------------------------------------------------- #

_socks_hint_shown = False


def fetch_page(url: str, session: requests.Session) -> Optional[str]:
    global _socks_hint_shown
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT_SEC)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        except requests.RequestException as exc:
            if "SOCKS" in str(exc) and not _socks_hint_shown:
                _socks_hint_shown = True
                print("  [подсказка] Включён VPN/прокси через SOCKS. Чтобы сбор работал "
                      "с VPN, установите пакет: pip install pysocks")
            print(f"  [retry {attempt}/{MAX_RETRIES}] {url} -> {exc}")
            time.sleep(REQUEST_DELAY_SEC * attempt)
    return None


def scrape(max_pages: int = 30, known_ids: set | None = None) -> list[Tender]:
    """
    Собирает тендеры по категориям из CATEGORY_PATHS. Дедуп по номеру (общий на все
    категории). max_pages — ПРЕДОХРАНИТЕЛЬ от бесконечного цикла, а не цель.

    Инкрементальный режим: если передан known_ids (набор уже известных id из базы),
    сбор идёт, пока на странице есть НОВЫЕ тендеры, и останавливает категорию, как
    только целая страница состоит только из уже известных (список отсортирован
    «сначала новые», значит дальше — только старые). Так добавляются ровно новые.
    """
    session = requests.Session()
    session.headers.update(HEADERS)
    incremental = known_ids is not None
    known_ids = known_ids or set()

    all_tenders: dict[str, Tender] = {}
    for path in CATEGORY_PATHS:
        print(f"=== Категория: {path} ===")
        for page in range(1, max_pages + 1):
            sep = "&" if "?" in path else "?"
            url = f"{BASE_URL}{path}" + (f"{sep}page={page}" if page > 1 else "")
            print(f"[page {page}] {url}")
            html = fetch_page(url, session)
            if not html:
                print("  страница не загрузилась, перехожу к следующей категории.")
                break

            tenders = parse_listing(html)
            print(f"  найдено карточек: {len(tenders)}")
            if not tenders:
                print("  тендеров не найдено — вероятно, страницы категории закончились.")
                break

            # сколько на этой странице по-настоящему новых (нет ни в базе, ни в этом прогоне)
            new_here = [t for t in tenders
                        if t.tender_id not in known_ids and t.tender_id not in all_tenders]
            for t in tenders:
                all_tenders[t.tender_id] = t     # дедуп по id (в т.ч. между категориями)

            if incremental:
                print(f"  из них новых: {len(new_here)}")
                if not new_here:
                    print("  вся страница уже в базе — дальше только старые, перехожу к следующей категории.")
                    break

            time.sleep(REQUEST_DELAY_SEC)

    return list(all_tenders.values())


def save_json(tenders: list[Tender], path: str = "tenders.json") -> None:
    data = [asdict(t) for t in tenders]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Сохранено {len(data)} тендеров -> {path}")


if __name__ == "__main__":
    result = scrape(max_pages=3)
    save_json(result)