# -*- coding: utf-8 -*-
"""Проверка годовой выручки компании-заказчика.

Для выгрузок Контур.Закупок проверка выполняется по ИНН через открытый ресурс
бухгалтерской отчётности ФНС (ГИР БО). Показатель ``gainSum`` соответствует
годовой выручке и публикуется сервисом в тысячах рублей. Ответы кэшируются в
SQLite, поэтому один ИНН не запрашивается при каждой повторной загрузке файла.

Справочник ниже оставлен как резерв для старых источников и для известных
крупных организаций (например, банков), которые не публикуют стандартную БФО.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
import os
from pathlib import Path
import re
from threading import Lock

import requests

MIN_REVENUE = 10_000_000_000          # 10 млрд ₽
# Как поступать, если компанию-заказчика определить нельзя (нет в справочнике
# или площадка её скрыла):
#   True  (по умолчанию) — пропускаем на ручную оценку (иначе источники, где
#          заказчик почти всегда скрыт заглушкой, не даёт вообще ничего);
#   False — строгий режим: рассматриваем ТОЛЬКО подтверждённо крупные компании
#          (имеет смысл на источниках, где заказчик виден: порталы компаний, ЕИС).
# Совместимость старых источников без ИНН: их старый фильтр по названию не
# меняем. Импорт Контур.Закупок использует строгий RevenueCheck и неизвестные
# компании в любом случае удаляет до сохранения.
INCLUDE_UNKNOWN = True

FNS_GIR_BO_SEARCH_URL = "https://bo.nalog.gov.ru/advanced-search/organizations/search"
ENV_PATH = Path(__file__).with_name(".env")
DATA_DIR = Path(__file__).with_name("data")
DEFAULT_NAME_REGISTRY_PATH = DATA_DIR / "russian_companies_mega_registry_with_it.txt"
DEFAULT_VERIFIED_REGISTRY_PATH = DATA_DIR / "verified_large_companies.csv"
DEFAULT_CACHE_DAYS = 30
DEFAULT_TIMEOUT_SECONDS = 12.0
_VERIFIED_REGISTRY_LOCK = Lock()
_TAG_RE = re.compile(r"<[^>]+>")
_REGISTRY_LINE_RE = re.compile(
    r"^\s*\d+\.\s+\[[^]]+]\s+(.+?)(?:\s+\|\s+Головная структура:|$)"
)


class RevenueServiceError(RuntimeError):
    """Проверка выручки не завершена: импорт безопасно прерывается."""


@dataclass(frozen=True)
class RevenueCheck:
    inn: str
    revenue_rub: int | None
    report_year: int | None
    company_name: str | None
    source: str
    status: str
    from_cache: bool = False

    @property
    def is_confirmed(self) -> bool:
        return ((self.status == "found" and self.revenue_rub is not None) or
                self.status == "registry_found")

    @property
    def passes(self) -> bool:
        if self.status == "registry_found":
            return True
        return (self.status == "found" and self.revenue_rub is not None and
                self.revenue_rub >= MIN_REVENUE)


def _load_local_env(path: Path = ENV_PATH) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if ((value.startswith('"') and value.endswith('"')) or
                (value.startswith("'") and value.endswith("'"))):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _setting(name: str, default: str = "") -> str:
    return os.environ.get(name, _load_local_env().get(name, default)).strip()


def normalize_inn(value: str | None) -> str:
    """Оставляет только корректный 10- или 12-значный ИНН."""
    inn = re.sub(r"\D", "", value or "")
    if len(inn) == 10:
        weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
        checksum = sum(int(digit) * weight
                       for digit, weight in zip(inn[:9], weights)) % 11 % 10
        return inn if checksum == int(inn[9]) else ""
    if len(inn) == 12:
        weights_11 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        weights_12 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        checksum_11 = sum(int(digit) * weight
                          for digit, weight in zip(inn[:10], weights_11)) % 11 % 10
        checksum_12 = sum(int(digit) * weight
                          for digit, weight in zip(inn[:11], weights_12)) % 11 % 10
        return inn if (checksum_11 == int(inn[10]) and
                       checksum_12 == int(inn[11])) else ""
    return ""


def _positive_int(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def _report_year(value) -> int | None:
    year = _positive_int(value)
    return year if year and 1990 <= year <= datetime.now().year + 1 else None


_MATCH_LEGAL_FORM_RE = re.compile(
    r"\b(ооо|оао|пао|ао|зао|нко|гуп|фгуп|муп|гбу|фгбу|ано|гку|гау|фку|фау|"
    r"мкпао|публичное акционерное общество|акционерное общество|"
    r"общество с ограниченной ответственностью)\b",
    re.I,
)


def company_name_key(value: str | None) -> str:
    """Строгий ключ названия: убирает форму юрлица, но сохраняет брендовые слова."""
    text = (value or "").lower().replace("ё", "е")
    text = text.replace("«", " ").replace("»", " ").replace('"', " ")
    text = _MATCH_LEGAL_FORM_RE.sub(" ", text)
    text = re.sub(r"[^\w\s-]", " ", text)
    return re.sub(r"[\s-]+", " ", text).strip()


class FnsGirBoRevenueClient:
    """Клиент открытого поиска ГИР БО ФНС по точному ИНН."""

    def __init__(self, timeout: float | None = None,
                 session: requests.Session | None = None):
        if timeout is None:
            try:
                timeout = float(_setting("FNS_GIR_BO_TIMEOUT_SECONDS",
                                         str(DEFAULT_TIMEOUT_SECONDS)))
            except ValueError:
                timeout = DEFAULT_TIMEOUT_SECONDS
        self.timeout = min(max(timeout, 1.0), 60.0)
        self.session = session or requests.Session()
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://bo.nalog.gov.ru/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/140 Safari/537.36"
            ),
        })

    def _search(self, query: str) -> list[dict]:
        try:
            response = self.session.get(
                FNS_GIR_BO_SEARCH_URL,
                params={"query": query, "page": 0, "size": 20},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise RevenueServiceError(
                "ГИР БО ФНС временно недоступен; данные не изменены, "
                "повторите загрузку позже"
            ) from exc

        if response.status_code in (403, 429):
            raise RevenueServiceError(
                "ГИР БО ФНС временно ограничил частоту запросов; данные не изменены"
            )
        if not response.ok:
            raise RevenueServiceError(
                f"ГИР БО ФНС вернул HTTP {response.status_code}; данные не изменены"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RevenueServiceError(
                "ГИР БО ФНС вернул некорректный ответ; данные не изменены"
            ) from exc
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, list):
            raise RevenueServiceError(
                "в ответе ГИР БО ФНС отсутствует список организаций; данные не изменены"
            )
        return [item for item in content if isinstance(item, dict)]

    @staticmethod
    def _from_org(org: dict, fallback_name: str | None, source: str) -> RevenueCheck:
        response_inn = _TAG_RE.sub("", str(org.get("inn") or ""))
        response_inn = normalize_inn(response_inn)
        bfo = org.get("bfo") if isinstance(org.get("bfo"), dict) else {}
        gain_thousands = _positive_int(bfo.get("gainSum"))
        revenue = gain_thousands * 1000 if gain_thousands is not None else None
        year = _report_year(bfo.get("period"))
        return RevenueCheck(
            response_inn, revenue, year, org.get("shortName") or fallback_name,
            source, "found" if revenue is not None else "no_data",
        )

    def lookup(self, inn: str, company_name: str | None = None) -> RevenueCheck:
        inn = normalize_inn(inn)
        if not inn:
            return RevenueCheck("", None, None, company_name, "fns-gir-bo", "invalid_inn")
        content = self._search(inn)
        exact = None
        for item in content:
            response_inn = _TAG_RE.sub("", str(item.get("inn") or ""))
            if normalize_inn(response_inn) == inn:
                exact = item
                break
        if exact is None:
            return RevenueCheck(inn, None, None, company_name, "fns-gir-bo", "not_found")
        result = self._from_org(exact, company_name, "fns-gir-bo")
        return RevenueCheck(
            inn, result.revenue_rub, result.report_year, result.company_name,
            result.source, result.status,
        )

    def lookup_by_name(self, company_name: str | None) -> RevenueCheck:
        """Ищет компанию по названию и принимает только близкое совпадение."""
        query_name = (company_name or "").strip()
        query_norm = company_name_key(query_name)
        if not query_norm:
            return RevenueCheck("", None, None, company_name,
                                "fns-gir-bo-name", "invalid_name")
        candidates = self._search(query_name)
        ranked: list[tuple[float, dict]] = []
        query_tokens = set(query_norm.split())
        for item in candidates:
            candidate_name = item.get("shortName") or item.get("fullName") or ""
            candidate_norm = company_name_key(_TAG_RE.sub("", str(candidate_name)))
            if not candidate_norm:
                continue
            if candidate_norm == query_norm:
                score = 1.0
            else:
                candidate_tokens = set(candidate_norm.split())
                common = len(query_tokens & candidate_tokens)
                score = (2 * common / (len(query_tokens) + len(candidate_tokens))
                         if query_tokens and candidate_tokens else 0.0)
                if (query_norm in candidate_norm or candidate_norm in query_norm):
                    score = max(score, 0.85)
            ranked.append((score, item))
        if not ranked:
            return RevenueCheck("", None, None, company_name,
                                "fns-gir-bo-name", "not_found")
        score, best = max(ranked, key=lambda pair: pair[0])
        if score < 0.75:
            return RevenueCheck("", None, None, company_name,
                                "fns-gir-bo-name", "name_mismatch")
        return self._from_org(best, company_name, "fns-gir-bo-name")


def _cache_days() -> int:
    try:
        return min(max(int(_setting("FNS_GIR_BO_CACHE_DAYS",
                                    str(DEFAULT_CACHE_DAYS))), 1), 365)
    except ValueError:
        return DEFAULT_CACHE_DAYS


def _cached_revenue(conn, inn: str, now: datetime) -> RevenueCheck | None:
    row = conn.execute(
        "SELECT inn, company_name, revenue_rub, report_year, source, status, checked_at "
        "FROM company_revenue_cache WHERE inn = ?", (inn,),
    ).fetchone()
    if row is None:
        return None
    try:
        checked_at = datetime.fromisoformat(row["checked_at"])
    except (TypeError, ValueError):
        return None
    if checked_at < now - timedelta(days=_cache_days()):
        return None
    return RevenueCheck(
        row["inn"], row["revenue_rub"], row["report_year"], row["company_name"],
        row["source"], row["status"], from_cache=True,
    )


def _save_cached_revenue(conn, check: RevenueCheck, now: datetime) -> None:
    conn.execute(
        "INSERT INTO company_revenue_cache "
        "(inn, company_name, revenue_rub, report_year, source, status, checked_at) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(inn) DO UPDATE SET company_name=excluded.company_name, "
        "revenue_rub=excluded.revenue_rub, report_year=excluded.report_year, "
        "source=excluded.source, status=excluded.status, checked_at=excluded.checked_at",
        (check.inn, check.company_name, check.revenue_rub, check.report_year,
         check.source, check.status, now.isoformat(timespec="seconds")),
    )


def _registry_path(value: str | Path | None, default: Path,
                   setting_name: str | None = None) -> Path:
    configured = _setting(setting_name) if setting_name else ""
    selected = value or configured
    return Path(selected).expanduser() if selected else default


@lru_cache(maxsize=8)
def _load_verified_registry(path_text: str) -> tuple[dict[str, RevenueCheck],
                                                      dict[str, RevenueCheck]]:
    """Загружает только записи с ИНН и фактическим оборотом не ниже порога."""
    path = Path(path_text)
    by_inn: dict[str, RevenueCheck] = {}
    by_name: dict[str, RevenueCheck] = {}
    if not path.exists():
        return by_inn, by_name
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            inn = normalize_inn(row.get("inn"))
            revenue = _positive_int(row.get("revenue"))
            if not inn or revenue is None or revenue < MIN_REVENUE:
                continue
            name = (row.get("name") or "").strip() or None
            check = RevenueCheck(
                inn, revenue, _report_year(row.get("year")), name,
                "verified-local-registry", "found", from_cache=True,
            )
            by_inn[inn] = check
            normalized_name = company_name_key(name)
            if normalized_name:
                by_name[normalized_name] = check
    return by_inn, by_name


@lru_cache(maxsize=8)
def _load_name_registry(path_text: str) -> dict[str, str]:
    """Извлекает точные названия из пользовательского текстового реестра."""
    path = Path(path_text)
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        match = _REGISTRY_LINE_RE.match(raw_line)
        if not match:
            continue
        original = match.group(1).strip()
        variants = {original, re.sub(r"\s*\([^)]*\)", "", original).strip()}
        for variant in variants:
            normalized = company_name_key(variant)
            if normalized:
                result.setdefault(normalized, original)
    return result


def _lookup_local_registry(inn: str, company_name: str | None,
                           verified_registry_path: str | Path | None,
                           name_registry_path: str | Path | None) -> RevenueCheck | None:
    verified_path = _registry_path(
        verified_registry_path, DEFAULT_VERIFIED_REGISTRY_PATH,
        "COMPANY_VERIFIED_REGISTRY_PATH",
    )
    by_inn, by_name = _load_verified_registry(str(verified_path.resolve()))
    if inn and inn in by_inn:
        return by_inn[inn]
    normalized_name = company_name_key(company_name)
    if normalized_name and normalized_name in by_name:
        return by_name[normalized_name]

    names_path = _registry_path(
        name_registry_path, DEFAULT_NAME_REGISTRY_PATH,
        "COMPANY_NAME_REGISTRY_PATH",
    )
    names = _load_name_registry(str(names_path.resolve()))
    if normalized_name and normalized_name in names:
        return RevenueCheck(
            inn, None, None, names[normalized_name],
            "local-name-registry", "registry_found", from_cache=True,
        )
    return None


def _remember_verified_company(check: RevenueCheck,
                               path_value: str | Path | None) -> None:
    if not check.passes or check.revenue_rub is None:
        return
    inn = normalize_inn(check.inn)
    if not inn:
        return
    path = _registry_path(
        path_value, DEFAULT_VERIFIED_REGISTRY_PATH,
        "COMPANY_VERIFIED_REGISTRY_PATH",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with _VERIFIED_REGISTRY_LOCK:
        existing: dict[str, tuple[int, int | None, str]] = {}
        if path.exists():
            with path.open("r", encoding="utf-8-sig", newline="") as source:
                for row in csv.DictReader(source):
                    row_inn = normalize_inn(row.get("inn"))
                    row_revenue = _positive_int(row.get("revenue"))
                    if row_inn and row_revenue is not None:
                        existing[row_inn] = (
                            row_revenue, _report_year(row.get("year")),
                            (row.get("name") or "").strip(),
                        )
        existing[inn] = (
            int(check.revenue_rub), check.report_year,
            (check.company_name or "").strip(),
        )
        temp_path = path.with_suffix(path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8", newline="") as target:
            writer = csv.writer(target)
            writer.writerow(["inn", "revenue", "year", "name"])
            for row_inn, (revenue, year, name) in sorted(existing.items()):
                writer.writerow([row_inn, revenue, year or "", name])
        temp_path.replace(path)
        _load_verified_registry.cache_clear()


def check_revenue_by_inn(conn, inn: str | None, company_name: str | None = None,
                         client: FnsGirBoRevenueClient | None = None,
                         now: datetime | None = None,
                         name_registry_path: str | Path | None = None,
                         verified_registry_path: str | Path | None = None) -> RevenueCheck:
    """Проверяет справочники, ФНС по ИНН и запасной поиск ФНС по названию."""
    normalized = normalize_inn(inn)
    now = now or datetime.now()
    local = _lookup_local_registry(
        normalized, company_name, verified_registry_path, name_registry_path,
    )
    if (local is not None and local.source == "verified-local-registry" and
            (not normalized or local.inn == normalized)):
        return local

    service = client or FnsGirBoRevenueClient()
    exact_result: RevenueCheck | None = None
    if normalized:
        cached = _cached_revenue(conn, normalized, now)
        if cached is not None and cached.is_confirmed:
            return cached
        try:
            exact_result = service.lookup(normalized, company_name)
        except RevenueServiceError:
            if local is not None:
                return local
            raise
        if exact_result.is_confirmed:
            _save_cached_revenue(conn, exact_result, now)
            _remember_verified_company(exact_result, verified_registry_path)
            return exact_result

    try:
        name_result = service.lookup_by_name(company_name)
    except RevenueServiceError:
        if local is not None:
            return local
        raise
    name_result_inn = normalize_inn(name_result.inn)
    name_inn_mismatch = bool(
        normalized and name_result_inn and name_result_inn != normalized
    )
    if name_result.is_confirmed and not name_inn_mismatch:
        result = RevenueCheck(
            normalized or name_result.inn,
            name_result.revenue_rub,
            name_result.report_year,
            name_result.company_name or company_name,
            name_result.source,
            name_result.status,
        )
        if result.inn:
            _save_cached_revenue(conn, result, now)
        _remember_verified_company(result, verified_registry_path)
        return result

    if local is not None:
        local_inn = normalize_inn(local.inn)
        if normalized and local_inn and local_inn != normalized:
            local = None
    if local is not None:
        if normalized and local.inn != normalized:
            local = RevenueCheck(
                normalized, local.revenue_rub, local.report_year,
                local.company_name or company_name, local.source, local.status,
                from_cache=True,
            )
        if normalized:
            _save_cached_revenue(conn, local, now)
        return local

    if name_inn_mismatch:
        status = "inn_mismatch"
    else:
        status = (name_result.status if name_result.status not in {"invalid_name"}
                  else (exact_result.status if exact_result else "invalid_inn"))
    result = RevenueCheck(
        normalized, None, None, company_name, "unverified", status,
    )
    if normalized:
        _save_cached_revenue(conn, result, now)
    return result

_B = 1_000_000_000

# Курируемый список крупных заказчиков (выручка приблизительная, все >= 10 млрд).
# Ключ — нормализованное имя (нижний регистр, без организационно-правовой формы).
_KNOWN = {
    "сбербанк": 4000 * _B, "сбер": 4000 * _B, "сбердевайсы": 30 * _B,
    "втб": 2000 * _B, "газпром": 10000 * _B, "газпром нефть": 3900 * _B,
    "роснефть": 9000 * _B, "лукойл": 8000 * _B, "татнефть": 1500 * _B,
    "сургутнефтегаз": 2300 * _B, "транснефть": 1300 * _B, "новатэк": 1400 * _B,
    "сибур": 1000 * _B, "ржд": 2700 * _B, "российские железные дороги": 2700 * _B,
    "ростелеком": 700 * _B, "ростех": 2800 * _B, "росатом": 2500 * _B,
    "норникель": 1200 * _B, "нлмк": 900 * _B, "северсталь": 800 * _B,
    "ммк": 760 * _B, "магнитогорский металлургический": 760 * _B,
    "русал": 1100 * _B, "евраз": 1200 * _B, "металлоинвест": 700 * _B,
    "тмк": 540 * _B, "омк": 400 * _B, "полюс": 500 * _B,
    "фосагро": 500 * _B, "еврохим": 700 * _B, "акрон": 180 * _B,
    "уралкалий": 300 * _B, "x5": 3100 * _B, "икс 5": 3100 * _B,
    "магнит": 2500 * _B, "лента": 600 * _B, "о'кей": 200 * _B,
    "вкусвилл": 240 * _B, "wildberries": 2500 * _B, "вайлдберриз": 2500 * _B,
    "ozon": 400 * _B, "озон": 400 * _B, "яндекс": 800 * _B,
    "vk": 150 * _B, "мтс": 600 * _B, "мегафон": 350 * _B,
    "вымпелком": 300 * _B, "билайн": 300 * _B, "т-банк": 500 * _B,
    "тинькофф": 500 * _B, "альфа-банк": 900 * _B, "газпромбанк": 800 * _B,
    "россельхозбанк": 600 * _B, "совкомбанк": 400 * _B, "открытие": 400 * _B,
    "райффайзен": 200 * _B, "росбанк": 200 * _B, "аэрофлот": 600 * _B,
    "почта россии": 220 * _B, "интер рао": 1300 * _B, "русгидро": 500 * _B,
    "россети": 1100 * _B, "мосэнерго": 250 * _B, "т плюс": 500 * _B,
    "камаз": 370 * _B, "автоваз": 500 * _B, "газпром энергохолдинг": 600 * _B,
    "пик": 550 * _B, "самолёт": 300 * _B, "самолет": 300 * _B,
    "лср": 240 * _B, "эталон": 130 * _B, "м.видео": 450 * _B,
    "мвидео": 450 * _B, "днс": 700 * _B, "детский мир": 170 * _B,
    "спортмастер": 150 * _B, "леруа мерлен": 500 * _B, "лемана про": 500 * _B,
    "мегафон ритейл": 100 * _B, "ситилинк": 200 * _B, "вб": 2500 * _B,
    "аэропорт шереметьево": 60 * _B, "мосводоканал": 60 * _B,
    "московский метрополитен": 250 * _B, "мосгортранс": 100 * _B,
    "аэрофлот техникс": 20 * _B, "россельхозцентр": 15 * _B,
}

_STRIP = re.compile(
    r"\b(пао|оао|ао|зао|ооо|нко|гуп|фгуп|муп|гбу|фгбу|гк|группа компаний|группа|"
    r"банк|публичное акционерное общество|акционерное общество|"
    r"общество с ограниченной ответственностью|компания|корпорация|холдинг)\b",
    re.I)


def _norm(name: str) -> str:
    name = (name or "").lower().replace("«", " ").replace("»", " ").replace('"', " ")
    name = _STRIP.sub(" ", name)
    name = re.sub(r"[^\w\s\-]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def annual_revenue_rub(company: str):
    """Годовая выручка компании в рублях или None, если неизвестна.

    TODO: здесь можно подключить реальный источник (СПАРК/Rusprofile/датасет).
    Сейчас — поиск по курируемому справочнику крупных компаний.
    """
    n = _norm(company)
    if not n:
        return None
    if n in _KNOWN:
        return _KNOWN[n]
    # подстрочное совпадение в обе стороны (напр. «сбербанк россии» -> «сбербанк»)
    for known, rev in _KNOWN.items():
        if known in n or n in known:
            return rev
    return None


def passes_revenue(company: str) -> bool:
    """Проходит ли компания порог 10 млрд ₽ (или неизвестна и INCLUDE_UNKNOWN=True)."""
    rev = annual_revenue_rub(company)
    if rev is None:
        return INCLUDE_UNKNOWN
    return rev >= MIN_REVENUE


def revenue_human(company: str) -> str:
    """Человекочитаемая выручка: '≈ 4 000 млрд ₽' или '—' если неизвестна."""
    rev = annual_revenue_rub(company)
    if rev is None:
        return "—"
    return "≈ " + revenue_human_value(rev)


def revenue_human_value(rev) -> str:
    """Форматирует сумму в рублях: '10 млрд ₽' / '850 млн ₽'."""
    if not rev:
        return "—"
    if rev >= _B:
        v = rev / _B
        return (f"{v:.0f} млрд ₽" if abs(v - round(v)) < 0.05 else f"{v:.1f} млрд ₽")
    return f"{rev / 1_000_000:.0f} млн ₽"
