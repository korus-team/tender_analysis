# -*- coding: utf-8 -*-
"""
Ассистент для анализа тендеров — веб-интерфейс в дизайне DAR.

Работает поверх существующего бэкенда проекта (storage, scoring, main,
enrich, icp_config) — эти файлы менять не нужно.

Запуск в PyCharm: правой кнопкой -> Run 'app', затем http://127.0.0.1:5000
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import wraps
from threading import Lock

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import storage
import directions
import company_size
from services import priority_companies
from services.rescoring import rescore_all
from scoring import theme_score
from icp_config import load_icp, save_icp
from integrations.kontur_excel import KonturExcelError, import_kontur_xlsx
import notification_service
from document_analysis import (DocumentAnalysisError, analyze as analyze_documents,
                               document_upload_dir, persist_uploads, read_uploads)
from services.document_requests import build_document_request
from observability.logging_config import configure_logging

configure_logging()
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = "dar-tender-assistant-local"
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

# --- пороги и настройки отображения -----------------------------------------
RELEVANT_MIN = 60      # «подходит нашей компании»
TOP_MIN = 70           # «наиболее подходящее»; числа вторичны, важнее теги и объяснение
NOTIFY_MIN_SCORE = 60  # порог: уведомления только для тендеров с баллом не ниже
USE_LLM_SCORING = str(os.getenv("USE_LLM_SCORING", "0")) == "1"
PER_PAGE = 20          # тендеров на страницу списка

_BLOCK_RE = re.compile(r"^(заказчик|организатор)\s*:?\s*", re.I)
_PLACEHOLDER_RE = re.compile(r"[\u2580-\u259F]+")  # символы «Block Elements»: ░ ▒ ▓ █ …
_kontur_import_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kontur-import")
_kontur_import_jobs: dict[str, dict] = {}
_kontur_import_lock = Lock()


# ============================================================================
#  Вспомогательные функции
# ============================================================================
def _to_int(v, default=None):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _plural_days(n: int) -> str:
    n = abs(n)
    if 11 <= n % 100 <= 14:
        return "дней"
    d = n % 10
    if d == 1:
        return "день"
    if 2 <= d <= 4:
        return "дня"
    return "дней"


def _plural_hours(n: int) -> str:
    n = abs(n)
    if 11 <= n % 100 <= 14:
        return "часов"
    d = n % 10
    if d == 1:
        return "час"
    if 2 <= d <= 4:
        return "часа"
    return "часов"


def _plural_minutes(n: int) -> str:
    n = abs(n)
    if 11 <= n % 100 <= 14:
        return "минут"
    d = n % 10
    if d == 1:
        return "минута"
    if 2 <= d <= 4:
        return "минуты"
    return "минут"


def _time_left_fmt(deadline: datetime | None, now: datetime) -> str:
    """Человеческий остаток времени для списков тендеров."""
    if deadline is None:
        return "Срок не указан"
    seconds = (deadline - now).total_seconds()
    if seconds < 0:
        return "Срок истёк"
    if seconds < 3600:
        minutes = max(1, math.ceil(seconds / 60))
        verb = "Осталась" if minutes % 10 == 1 and minutes % 100 != 11 else "Осталось"
        return f"{verb} {minutes} {_plural_minutes(minutes)}"
    if seconds < 24 * 3600:
        hours = max(1, math.ceil(seconds / 3600))
        verb = "Остался" if hours % 10 == 1 and hours % 100 != 11 else "Осталось"
        return f"{verb} {hours} {_plural_hours(hours)}"
    days = max(1, int(seconds // (24 * 3600)))
    verb = "Остался" if days % 10 == 1 and days % 100 != 11 else "Осталось"
    return f"{verb} {days} {_plural_days(days)}"


def _days_fmt(days) -> str:
    if days is None:
        return "—"
    if days < 0:
        return "просрочен"
    if days == 0:
        return "сегодня"
    return f"{days} {_plural_days(days)}"


def _urgency(days) -> str:
    if days is None:
        return "ok"
    if days <= 3:
        return "crit"
    if days <= 7:
        return "soon"
    if days <= 14:
        return "warn"
    return "ok"


def level_of(score):
    s = score or 0
    if s >= TOP_MIN:
        return "high", "Наиболее подходящее"
    if s >= RELEVANT_MIN:
        return "mid", "Подходящее"
    return "low", "Наименее подходящее"


def _apply_level(t: dict) -> None:
    lvl, lbl = level_of(t.get("score"))
    t["level"] = lvl
    t["level_label"] = lbl


def fmt_price(p) -> str:
    if not p:
        return "—"
    if p >= 1_000_000:
        v = p / 1_000_000
        return f"{v:.0f} млн" if abs(v - round(v)) < 0.05 else f"{v:.1f} млн"
    if p >= 1000:
        return f"{round(p / 1000)} тыс"
    return f"{p} ₽"


def _clean_customer(c):
    if not c:
        return None
    c = _BLOCK_RE.sub("", c)              # убрать префикс «Заказчик:»
    c = _PLACEHOLDER_RE.sub("", c)        # убрать служебные блочные заглушки
    c = re.sub(r"\s+", " ", c).strip()
    return c or None


_RISK_MAP = {
    "дедлайн близко": "Скоро дедлайн — мало времени на подготовку",
    "цена не указана": "Начальная цена не указана — проверить вручную",
    "не профиль: поставка/лицензии": "Похоже на поставку/перепродажу, а не разработку",
    "тема не наша": "Нет совпадений по профильным темам",
    "бюджет мал": "Бюджет ниже рабочего минимума",
    "бюджет велик": "Бюджет выше рабочего максимума",
    "стоп-слово": "Есть стоп-слово из профиля",
    "регион исключён": "Регион в списке исключённых",
    "дедлайн прошёл/близко": "Дедлайн слишком близко или уже прошёл",
}


def _risks_from_labels(labels, days):
    risks = []
    for lab in (labels or []):
        txt = _RISK_MAP.get(lab)
        if txt and txt not in risks:
            risks.append(txt)
    if days is not None and days < 0 and "Дедлайн уже прошёл" not in risks:
        risks.append("Дедлайн уже прошёл")
    return risks


def icon_for(cat) -> str:
    c = (cat or "").lower()
    if "информацион" in c:
        return "i-db"
    if "программир" in c or "услуг" in c:
        return "i-bars"
    if "разработ" in c or "по" in c:
        return "i-ai"
    return "i-grid"


def _is_recent(iso, days=1) -> bool:
    if not iso:
        return False
    try:
        return (datetime.now() - datetime.fromisoformat(iso)).days < days
    except (TypeError, ValueError):
        return False


def _not_expired(t) -> bool:
    """True, если дедлайн не прошёл (или его нет)."""
    d = t.get("days_left")
    return d is None or d >= 0


def _annotate(tenders):
    """Готовит записи из БД к показу: даты, баллы-уровни, цена, риски, JSON-поля."""
    now = datetime.now()
    out = []
    for src in tenders:
        t = dict(src)
        # documents / details: строки JSON -> python
        for k in ("documents", "details"):
            v = t.get(k)
            if isinstance(v, str) and v.strip():
                try:
                    import json
                    t[k] = json.loads(v)
                except (ValueError, TypeError):
                    t[k] = None
            elif not v:
                t[k] = None
        # reasons / labels -> всегда список
        for k in ("reasons", "labels"):
            if not isinstance(t.get(k), list):
                t[k] = t.get(k) or []
        # дедлайн
        days, dfmt, deadline_dt = None, "—", None
        deadline_date_fmt, deadline_time_fmt = "—", ""
        dl = t.get("deadline")
        if dl:
            try:
                d = datetime.fromisoformat(dl)
                deadline_dt = d
                days = (d - now).days
                dfmt = d.strftime("%d.%m.%Y · %H:%M")
                deadline_date_fmt = d.strftime("%d.%m.%Y")
                deadline_time_fmt = d.strftime("%H:%M МСК")
            except (ValueError, TypeError):
                pass
        t["days_left"] = days
        t["deadline_fmt"] = dfmt
        t["deadline_date_fmt"] = deadline_date_fmt
        t["deadline_time_fmt"] = deadline_time_fmt
        t["days_fmt"] = _days_fmt(days)
        t["time_left_fmt"] = _time_left_fmt(deadline_dt, now)
        seconds_left = ((deadline_dt - now).total_seconds()
                        if deadline_dt is not None else None)
        t["deadline_reminder"] = (seconds_left is not None
                                  and 0 <= seconds_left <= 3 * 24 * 3600)
        t["urgency"] = _urgency(days)
        # тип закупки (предметный тег)
        t["direction"] = directions.classify(t)
        t["direction_name"] = directions.name_of(t["direction"])
        t["is_license"] = t["direction"] == "license"
        # красным подсвечиваем срок ≤ 3 дней, КРОМЕ лицензий (там ещё можно успеть)
        t["deadline_soon"] = (days is not None and 0 <= days <= 3
                              and t["direction"] != "license")
        # коммерческая / государственная (по наличию номера в ЕИС)
        det = t.get("details") if isinstance(t.get("details"), dict) else {}
        trade_type = str(det.get("trade_type") or "").lower().replace(" ", "")
        is_government = bool(
            det.get("eis_number") or det.get("eis_url") or
            any(marker in trade_type for marker in
                ("44-фз", "223-фз", "615ппрф", "615-пп"))
        )
        if is_government:
            t["ptype"], t["ptype_label"] = "gov", "Госзакупка (ФЗ)"
        elif t.get("enriched_at"):
            t["ptype"], t["ptype_label"] = "com", "Коммерческая"
        else:
            t["ptype"], t["ptype_label"] = None, None
        t["law"] = _law_tag(t)
        # уровень соответствия
        _apply_level(t)
        # цена / заказчик / риски
        t["price_fmt"] = fmt_price(t.get("price_rub"))
        t["customer"] = _clean_customer(t.get("customer"))
        exact_revenue = (det.get("customer_turnover_rub") or
                         det.get("customer_revenue_rub"))
        turnover_status = det.get("customer_turnover_status")
        t["turnover_unverified"] = turnover_status in {
            "no_data", "not_found", "invalid_inn"
        }
        if exact_revenue is not None:
            t["revenue_h"] = company_size.revenue_human_value(exact_revenue)
        elif t["turnover_unverified"]:
            t["revenue_h"] = "Не подтверждён"
        else:
            t["revenue_h"] = company_size.revenue_human(t.get("customer"))
        t["risks"] = _risks_from_labels(t.get("labels"), days)
        out.append(t)
    return out


# --- разбор форм профиля ICP -------------------------------------------------
def _lines(text):
    if not text:
        return []
    return [p.strip() for p in re.split(r"[\n,]", text) if p.strip()]


def _pairs(text):
    out = {}
    if not text:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        iv = _to_int(v)
        if iv is not None:
            out[k.strip()] = iv
    return out


def _lines_text(lst):
    return "\n".join(lst or [])


def _pairs_text(d):
    return "\n".join(f"{k}: {v}" for k, v in (d or {}).items())


def _ensure_favorite_column():
    """Добавляет колонку favorite в таблицу tenders (storage.py не трогаем)."""
    conn = storage.connect()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tenders)")}
    if "favorite" not in cols:
        conn.execute("ALTER TABLE tenders ADD COLUMN favorite INTEGER DEFAULT 0")
        conn.commit()
    conn.close()


def _ensure_tasks_table():
    """Отдельная таблица задач для вкладки «Задачи» (storage.py не трогаем)."""
    conn = storage.connect()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS app_tasks ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "title TEXT NOT NULL, "
        "tender_id TEXT, "
        "tender_title TEXT, "
        "due_date TEXT, "
        "priority TEXT DEFAULT 'normal', "
        "done INTEGER DEFAULT 0, "
        "created_at TEXT)")
    conn.commit()
    conn.close()


def _ensure_auth_tables():
    """Таблицы многопользовательского режима (в той же SQLite-базе)."""
    conn = storage.connect()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "username TEXT UNIQUE NOT NULL, "
        "password_hash TEXT NOT NULL, "
        "created_at TEXT, "
        "avatar BLOB, "
        "avatar_mime TEXT)")
    user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "avatar" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN avatar BLOB")
    if "avatar_mime" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN avatar_mime TEXT")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS user_favorites ("
        "user_id INTEGER NOT NULL, "
        "tender_id TEXT NOT NULL, "
        "created_at TEXT, "
        "PRIMARY KEY (user_id, tender_id))")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fav_notifications ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "actor_id INTEGER NOT NULL, "
        "actor_name TEXT, "
        "tender_id TEXT NOT NULL, "
        "tender_title TEXT, "
        "created_at TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS notif_seen ("
        "user_id INTEGER NOT NULL, "
        "notif_id INTEGER NOT NULL, "
        "PRIMARY KEY (user_id, notif_id))")
    conn.commit()
    conn.close()


def _ensure_upload_history_table():
    """Журнал успешно обработанных Excel-выгрузок Контур.Закупок."""
    conn = storage.connect()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS upload_history ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "user_id INTEGER, "
        "username TEXT NOT NULL, "
        "uploaded_at TEXT NOT NULL, "
        "filename TEXT NOT NULL, "
        "sheet_name TEXT)"
    )
    conn.commit()
    conn.close()


def _prune_upload_history(conn, now: datetime | None = None) -> int:
    """Удаляет записи журнала старше 30 дней и возвращает их количество."""
    cutoff = (now or datetime.now()) - timedelta(days=30)
    cursor = conn.execute(
        "DELETE FROM upload_history WHERE uploaded_at < ?",
        (cutoff.isoformat(timespec="seconds"),),
    )
    conn.commit()
    return cursor.rowcount


def _upload_history_cutoff(now: datetime | None = None) -> str:
    """Нижняя граница видимого месячного журнала без записи в БД."""
    return ((now or datetime.now()) - timedelta(days=30)).isoformat(timespec="seconds")


def _record_upload_history(
    user_id: int | None,
    username: str,
    uploaded_at: str,
    filename: str,
    sheet_name: str | None,
) -> None:
    """Записывает аудит загрузки, не меняя результат уже завершённого импорта."""
    conn = None
    try:
        conn = storage.connect()
        _prune_upload_history(conn)
        conn.execute(
            "INSERT INTO upload_history "
            "(user_id, username, uploaded_at, filename, sheet_name) VALUES (?,?,?,?,?)",
            (user_id, username, uploaded_at, filename, sheet_name),
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        if conn is not None:
            conn.rollback()
        logger.exception(
            "upload_history_record_failed user_id=%s filename=%s", user_id, filename
        )
    finally:
        if conn is not None:
            conn.close()


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    if "has_avatar" not in session:
        conn = storage.connect()
        row = conn.execute(
            "SELECT username, avatar_mime FROM users WHERE id = ?", (uid,)
        ).fetchone()
        conn.close()
        if not row:
            session.clear()
            return None
        session["username"] = row["username"]
        session["has_avatar"] = bool(row["avatar_mime"])
    has_avatar = bool(session.get("has_avatar"))
    return {
        "id": uid,
        "username": session.get("username", ""),
        "has_avatar": has_avatar,
        "avatar_url": url_for("user_avatar", user_id=uid) if has_avatar else None,
    }


def _image_mime(data: bytes) -> str | None:
    """Определяет поддерживаемый тип изображения по содержимому, а не расширению."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _claimed_ids(conn):
    """tender_id всех тендеров, которые кто-либо уже забрал в избранное (заняты)."""
    return {r[0] for r in conn.execute("SELECT DISTINCT tender_id FROM user_favorites")}


def _my_fav_ids(conn, uid):
    return {r[0] for r in conn.execute(
        "SELECT tender_id FROM user_favorites WHERE user_id = ?", (uid,))}


def _ensure_meta_table():
    """Ручная корректировка релевантности, статус «не пошли», этап (без изменения storage.py)."""
    conn = storage.connect()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tender_meta ("
        "tender_id TEXT PRIMARY KEY, "
        "relevance TEXT, "          # 'relevant' / 'irrelevant' / NULL (авто)
        "not_pursued INTEGER DEFAULT 0)")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tender_meta)")]
    if "stage" not in cols:        # этап: passed_dar / rejected_dar / passed_other
        conn.execute("ALTER TABLE tender_meta ADD COLUMN stage TEXT")
    conn.commit()
    conn.close()


def _load_stages(conn):
    return {r[0]: r[1] for r in conn.execute(
        "SELECT tender_id, stage FROM tender_meta WHERE stage IS NOT NULL")}


STAGE_LABELS = {"passed_dar": "Передано ДАР",
                "rejected_dar": "Отклонено ДАР",
                "passed_other": "Передано др. деп.",
                "not_pursued": "Не пошли"}


def _load_meta(conn):
    """Возвращает (rel_map, not_pursued_set)."""
    rel_map, not_pursued = {}, set()
    for r in conn.execute("SELECT tender_id, relevance, not_pursued FROM tender_meta"):
        if r["relevance"]:
            rel_map[r["tender_id"]] = r["relevance"]
        if r["not_pursued"]:
            not_pursued.add(r["tender_id"])
    return rel_map, not_pursued


def _is_relevant_eff(t, rel_map):
    """Эффективная релевантность: ручная пометка важнее авто-классификации."""
    ov = rel_map.get(t["tender_id"])
    if ov == "relevant":
        return True
    if ov == "irrelevant":
        return False
    return (directions.is_relevant(t) and
            int(t.get("score") or 0) >= RELEVANT_MIN)


# --- приоритетные компании (заменяют «профиль компании») ---
def _ensure_priorities_table():
    conn = storage.connect()
    priority_companies.ensure_schema(conn)
    conn.close()


def _priority_index(conn):
    """Возвращает набор ИНН приоритетных компаний."""
    return priority_companies.priority_inns(conn)


def _is_priority(tender, pindex):
    """Приоритетность определяется только точным совпадением нормализованного ИНН."""
    return priority_companies.is_priority_tender(tender, pindex)


def _mark_priority(rows, pindex):
    for tender in rows:
        tender["is_priority"] = _is_priority(tender, pindex)
    return rows


def _passes_company_filter(tender):
    """Госзакупки и приоритетные компании не ограничиваются порогом 10 млрд."""
    return (tender.get("ptype") == "gov" or tender.get("is_priority") or
            company_size.passes_revenue(tender.get("customer")))


def _ensure_settings_table():
    conn = storage.connect()
    conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()


def _ensure_document_analysis_table():
    """Результаты ИИ-анализа: без хранения самих загруженных файлов."""
    conn = storage.connect()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tender_document_analyses ("
        "tender_id TEXT PRIMARY KEY, documents TEXT NOT NULL, risks TEXT NOT NULL, "
        "pitfalls TEXT NOT NULL, recommendations TEXT NOT NULL, openness TEXT, "
        "summary TEXT, analyzer TEXT NOT NULL, analyzed_at TEXT NOT NULL)")
    conn.commit()
    conn.close()


def _document_analysis(conn, tender_id):
    row = conn.execute(
        "SELECT * FROM tender_document_analyses WHERE tender_id = ?", (tender_id,)
    ).fetchone()
    if not row:
        return None
    result = dict(row)
    for key in ("documents", "risks", "pitfalls", "recommendations"):
        try:
            result[key] = json.loads(result[key] or "[]")
        except (TypeError, ValueError):
            result[key] = []
    return result


def _save_document_analysis(conn, tender_id, documents, result):
    conn.execute(
        "INSERT INTO tender_document_analyses "
        "(tender_id, documents, risks, pitfalls, recommendations, openness, summary, analyzer, analyzed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(tender_id) DO UPDATE SET documents=excluded.documents, risks=excluded.risks, "
        "pitfalls=excluded.pitfalls, recommendations=excluded.recommendations, openness=excluded.openness, "
        "summary=excluded.summary, analyzer=excluded.analyzer, analyzed_at=excluded.analyzed_at",
        (tender_id, json.dumps(documents, ensure_ascii=False),
         json.dumps(result["risks"], ensure_ascii=False),
         json.dumps(result["pitfalls"], ensure_ascii=False),
         json.dumps(result["recommendations"], ensure_ascii=False),
         result["openness"], result["summary"], result["analyzer"],
         datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def _law_tag(t):
    """Тег закона по тексту закупки (лёгкая эвристика, где закон упомянут)."""
    details = t.get("details") if isinstance(t.get("details"), dict) else {}
    txt = " ".join(str(part) for part in (
        t.get("title"), t.get("subject"), t.get("category"),
        details.get("trade_type"), details.get("method"), details.get("tag"),
    ) if part).lower()
    if "223" in txt:
        return "223-ФЗ"
    if "44-фз" in txt or "44 фз" in txt or "44фз" in txt:
        return "44-ФЗ"
    if "615" in txt:
        return "615 ПП РФ"
    return None


def _deadline_active(dl):
    """Дедлайн не прошёл (или его нет)."""
    if not dl:
        return True
    try:
        return datetime.fromisoformat(dl) >= datetime.now()
    except (ValueError, TypeError):
        return True


_PRIORITY_LABELS = {"high": "Высокий", "normal": "Обычный", "low": "Низкий"}


def _annotate_tasks(rows):
    today = datetime.now().date()
    out = []
    for src in rows:
        t = dict(src)
        due = t.get("due_date")
        t["due_fmt"], t["due_urg"], t["due_days"] = "—", "ok", None
        if due:
            try:
                d = datetime.fromisoformat(due).date()
                t["due_days"] = (d - today).days
                t["due_fmt"] = d.strftime("%d.%m.%Y")
                t["due_urg"] = _urgency(t["due_days"])
            except (ValueError, TypeError):
                pass
        t["prio_label"] = _PRIORITY_LABELS.get(t.get("priority", "normal"), "Обычный")
        out.append(t)
    return out


_ensure_favorite_column()
_ensure_tasks_table()
_ensure_auth_tables()
_ensure_upload_history_table()
_upload_history_conn = storage.connect()
_prune_upload_history(_upload_history_conn)
_upload_history_conn.close()
_ensure_meta_table()
_ensure_priorities_table()
_ensure_settings_table()
_ensure_document_analysis_table()
_notification_conn = storage.connect()
notification_service.ensure_schema(_notification_conn)
notification_service.prune_ineligible_site_notifications(
    _notification_conn, relevant_min=RELEVANT_MIN
)
_notification_conn.close()
logger.info(
    "application_initialized llm_scoring=%s database=%s",
    USE_LLM_SCORING, storage.DB_PATH,
)


@app.before_request
def _require_login():
    """Пускаем на страницы только авторизованных (кроме входа/регистрации/статики)."""
    if request.endpoint in ("login", "register", "static") or request.endpoint is None:
        return None
    if not session.get("user_id"):
        return redirect(url_for("login"))
    return None


@app.before_request
def _start_request_timer():
    g.request_started_at = time.perf_counter()


@app.after_request
def _log_request(response):
    if request.endpoint != "static":
        started = getattr(g, "request_started_at", None)
        elapsed_ms = ((time.perf_counter() - started) * 1000 if started else 0)
        logger.info(
            "http_request method=%s path=%s status=%s duration_ms=%.1f",
            request.method, request.path, response.status_code, elapsed_ms,
        )
    return response


@app.teardown_request
def _log_request_error(error):
    if error is not None:
        logger.error(
            "http_request_failed method=%s path=%s error=%s",
            request.method, request.path, type(error).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )


@app.context_processor
def inject_globals():
    return {"current_user": current_user()}


@app.context_processor
def inject_notifications():
    """Общий колокольчик: новые тендеры и действия сотрудников без дублей."""
    try:
        uid = session.get("user_id")
        if not uid:
            return {"notifications": [], "notif_count": 0,
                    "my_fav_ids": set(), "taken_by": {}}
        here = request.path
        conn = storage.connect()
        favorite_events = conn.execute(
            "SELECT n.id, n.actor_name, n.tender_id, n.tender_title, n.created_at "
            "FROM fav_notifications n "
            "JOIN user_favorites f ON f.tender_id = n.tender_id AND f.user_id = n.actor_id "
            "WHERE n.actor_id != ? AND NOT EXISTS ("
            "SELECT 1 FROM notif_seen s WHERE s.user_id = ? AND s.notif_id = n.id) "
            "ORDER BY n.id DESC LIMIT 40", (uid, uid)).fetchall()
        site_events = conn.execute(
            "SELECT id, kind, tender_id, tender_title, message, created_at "
            "FROM site_notifications n WHERE NOT EXISTS ("
            "SELECT 1 FROM site_notif_seen s "
            "WHERE s.user_id = ? AND s.notification_id = n.id) "
            "ORDER BY id DESC LIMIT 40", (uid,)
        ).fetchall()
        my_favs = _my_fav_ids(conn, uid)
        taken_by = {r["tender_id"]: r["username"] for r in conn.execute(
            "SELECT f.tender_id, u.username FROM user_favorites f "
            "JOIN users u ON u.id = f.user_id")}
        conn.close()
        notes = []
        for e in favorite_events:
            nid = e["id"]
            target = url_for("tender", tender_id=e["tender_id"], ret=here)
            notes.append({
                "id": nid, "created_at": e["created_at"] or "", "icon": "i-heart",
                "title": e["tender_title"] or "Тендер",
                "meta": f"{e['actor_name'] or 'Сотрудник'} взял(а) в работу",
                "url": url_for("notif_read", id=nid, source="favorite", to=target),
            })
        for e in site_events:
            nid = e["id"]
            target = url_for("tender", tender_id=e["tender_id"], ret=here)
            notes.append({
                "id": nid, "created_at": e["created_at"] or "", "icon": "i-search",
                "title": e["tender_title"] or "Новый тендер",
                "meta": e["message"] or "Новый подходящий тендер",
                "url": url_for("notif_read", id=nid, source="site", to=target),
            })
        notes.sort(key=lambda note: note["created_at"], reverse=True)
        notes = notes[:40]
        return {"notifications": notes,
                "notif_count": len(notes),
                "my_fav_ids": my_favs, "taken_by": taken_by}
    except Exception:  # noqa: BLE001
        return {"notifications": [], "notif_count": 0,
                "my_fav_ids": set(), "taken_by": {}}


# ============================================================================
#  Авторизация
# ============================================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("home"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        conn = storage.connect()
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()
        if row and check_password_hash(row["password_hash"], password):
            session["user_id"] = row["id"]
            session["username"] = row["username"]
            session["has_avatar"] = bool(row["avatar_mime"])
            return redirect(url_for("home"))
        flash("Неверный логин или пароль", "err")
        return redirect(url_for("login"))
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("home"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if len(username) < 2 or len(password) < 4:
            flash("Логин — от 2 символов, пароль — от 4", "err")
            return redirect(url_for("register"))
        conn = storage.connect()
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            conn.close()
            flash("Такой логин уже занят", "err")
            return redirect(url_for("register"))
        conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                     (username, generate_password_hash(password),
                      datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()
        session["user_id"] = row["id"]
        session["username"] = row["username"]
        session["has_avatar"] = bool(row["avatar_mime"])
        return redirect(url_for("home"))
    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ============================================================================
#  Страницы
# ============================================================================
@app.route("/")
def home():
    conn = storage.connect()
    direction = request.args.get("direction") or None
    ptype = request.args.get("ptype") or None
    q = (request.args.get("q") or "").strip()
    sort = request.args.get("sort") or "deadline"
    show = request.args.get("show") or "all"
    show_filters = request.args.get("filters") == "1"
    page = request.args.get("page", type=int) or 1

    rel_map, not_pursued = _load_meta(conn)
    pindex = _priority_index(conn)
    rows = _mark_priority(_annotate(storage.query_tenders(conn, limit=None)), pindex)
    rows = [t for t in rows
            if _passes_company_filter(t) and _not_expired(t)
            and _is_relevant_eff(t, rel_map) and t["tender_id"] not in not_pursued]
    last_upload_row = conn.execute(
        "SELECT uploaded_at FROM upload_history WHERE uploaded_at >= ? "
        "ORDER BY uploaded_at DESC, id DESC LIMIT 1",
        (_upload_history_cutoff(),),
    ).fetchone()
    conn.close()

    last_upload = "—"
    if last_upload_row:
        try:
            last_upload = datetime.fromisoformat(last_upload_row["uploaded_at"]).strftime(
                "%d.%m.%Y %H:%M"
            )
        except (TypeError, ValueError):
            last_upload = last_upload_row["uploaded_at"]

    for t in rows:
        t["law"] = _law_tag(t)

    # счётчики (по всей отобранной выборке, до фильтров)
    stat_total = len(rows)
    stat_top = sum(1 for t in rows if (t.get("score") or 0) >= TOP_MIN)
    stat_lic = sum(1 for t in rows if t.get("is_license"))
    stat_priority = sum(1 for t in rows if t.get("is_priority"))

    # фильтры
    view = rows
    if direction:
        view = [t for t in view if t.get("direction") == direction]
    if ptype in ("gov", "com"):
        view = [t for t in view if t.get("ptype") == ptype]
    if q:
        ql = q.lower()
        view = [t for t in view if ql in ((t.get("title") or "") + " "
                + (t.get("subject") or "") + " " + (t.get("customer") or "")).lower()]
    if show == "priority":
        view = [t for t in view if t.get("is_priority")]
    elif show == "license":
        view = [t for t in view if t.get("is_license")]
    elif show == "gov":
        view = [t for t in view if t.get("ptype") == "gov"]
    elif show == "com":
        view = [t for t in view if t.get("ptype") == "com"]

    # сортировка (ai = рекомендации ИИ: пока по баллу; deadline; price; company)
    if sort == "deadline":
        view.sort(key=lambda x: (x.get("days_left") if x.get("days_left") is not None else 10 ** 9))
    elif sort == "price":
        view.sort(key=lambda x: (x.get("price_rub") or 0), reverse=True)
    elif sort == "company":
        view.sort(key=lambda x: (x.get("customer") or "\uffff").lower())
    else:  # ai
        view.sort(key=lambda x: (x.get("is_priority"), x.get("score") or 0), reverse=True)

    total_found = len(view)
    total_pages = max(1, (total_found + PER_PAGE - 1) // PER_PAGE)
    page = min(max(1, page), total_pages)
    page_rows = view[(page - 1) * PER_PAGE:(page - 1) * PER_PAGE + PER_PAGE]

    return render_template(
        "home.html", active="home", last_upload=last_upload,
        stat_total=stat_total, stat_top=stat_top, stat_lic=stat_lic,
        stat_priority=stat_priority, tenders=page_rows,
        direction=direction, ptype=ptype, q=q, sort=sort, show=show, show_filters=show_filters,
        type_keys=directions.all_keys(include_other=False), dir_name=directions.name_of,
        total_found=total_found, page=page, total_pages=total_pages)


@app.route("/upload-history")
def upload_history():
    conn = storage.connect()
    rows = [dict(row) for row in conn.execute(
        "SELECT username, uploaded_at, filename, sheet_name "
        "FROM upload_history WHERE uploaded_at >= ? ORDER BY uploaded_at DESC, id DESC",
        (_upload_history_cutoff(),),
    ).fetchall()]
    conn.close()
    for row in rows:
        try:
            row["uploaded_at_fmt"] = datetime.fromisoformat(row["uploaded_at"]).strftime(
                "%d.%m.%Y %H:%M"
            )
        except (TypeError, ValueError):
            row["uploaded_at_fmt"] = row["uploaded_at"]
    return render_template("upload_history.html", active="home", uploads=rows)


def _group_by_direction(annotated_rows):
    """Группирует размеченные тендеры по направлениям DAR (в порядке направлений)."""
    buckets = {}
    for t in annotated_rows:
        k = t.get("direction") or "other"
        b = buckets.setdefault(k, {"key": k, "name": directions.name_of(k),
                                   "icon": directions.icon_of(k), "total": 0, "top": 0})
        b["total"] += 1
        if (t.get("score") or 0) >= TOP_MIN:
            b["top"] += 1
    return [buckets[k] for k in directions.all_keys() if k in buckets]


@app.route("/tenders")
def tenders():
    conn = storage.connect()
    direction = request.args.get("direction") or None
    ptype = request.args.get("ptype") or None
    match = request.args.get("match") or None
    q = (request.args.get("q") or "").strip()
    sort = request.args.get("sort") or "deadline"   # по умолчанию — по сроку подачи
    page = request.args.get("page", type=int) or 1

    rel_map, not_pursued = _load_meta(conn)
    pindex = _priority_index(conn)
    rows = _mark_priority(_annotate(storage.query_tenders(conn, limit=None)), pindex)
    rows = [t for t in rows
            if _passes_company_filter(t) and _not_expired(t)
            and _is_relevant_eff(t, rel_map) and t["tender_id"] not in not_pursued]
    conn.close()

    if direction:
        rows = [t for t in rows if t.get("direction") == direction]
    if ptype in ("gov", "com"):
        rows = [t for t in rows if t.get("ptype") == ptype]
    if match in ("high", "mid", "low"):
        rows = [t for t in rows if t.get("level") == match]
    # поиск по названию, описанию И КОМПАНИИ-заказчику
    if q:
        ql = q.lower()
        rows = [t for t in rows if ql in ((t.get("title") or "") + " "
                + (t.get("subject") or "") + " " + (t.get("customer") or "")).lower()]

    if sort == "price":
        rows.sort(key=lambda x: (x.get("price_rub") or 0), reverse=True)
    elif sort == "score":
        rows.sort(key=lambda x: (x.get("score") or 0), reverse=True)
    elif sort == "company":
        rows.sort(key=lambda x: (x.get("customer") or "\uffff").lower())
    else:  # deadline (по умолчанию) — ближайшие сроки сверху
        rows.sort(key=lambda x: (x.get("days_left") if x.get("days_left") is not None else 10 ** 9))

    total_found = len(rows)
    total_pages = max(1, (total_found + PER_PAGE - 1) // PER_PAGE)
    page = min(max(1, page), total_pages)
    start = (page - 1) * PER_PAGE
    page_rows = rows[start:start + PER_PAGE]

    return render_template("tenders.html", active="tenders", tenders=page_rows,
                           title=(directions.name_of(direction) if direction else "Подходящие тендеры"),
                           direction=direction, ptype=ptype, match=match, q=q, sort=sort,
                           type_keys=directions.all_keys(include_other=False),
                           dir_name=directions.name_of,
                           total_found=total_found, page=page, total_pages=total_pages)


@app.route("/priorities")
def priorities():
    """Приоритетные компании — их тендеры подсвечиваются в поиске."""
    conn = storage.connect()
    rows = conn.execute("SELECT id, name, inn FROM priority_companies ORDER BY id").fetchall()
    conn.close()
    q = (request.args.get("q") or "").strip()
    companies = [dict(r) for r in rows]
    if q:
        ql = q.lower()
        companies = [c for c in companies
                     if ql in (c["name"] or "").lower() or ql in (c["inn"] or "")]
    return render_template("priorities.html", active="priorities", companies=companies,
                           q=q)


@app.route("/priorities/<int:cid>/delete", methods=["POST"])
def priorities_delete(cid):
    conn = storage.connect()
    deleted = conn.execute("DELETE FROM priority_companies WHERE id = ?", (cid,)).rowcount
    conn.commit()
    conn.close()
    logger.info("priority_company_deleted id=%s deleted=%s", cid, bool(deleted))
    flash("Компания удалена из приоритетных" if deleted else "Компания уже удалена")
    return redirect(url_for("priorities"))


@app.route("/priorities/clear", methods=["POST"])
def priorities_clear():
    conn = storage.connect()
    conn.execute("DELETE FROM priority_companies")
    conn.commit()
    conn.close()
    flash("Список приоритетных компаний очищен")
    return redirect(url_for("priorities"))


@app.route("/priorities/import", methods=["POST"])
def priorities_import():
    """Импорт из Excel: автоматически находит колонки названия и ИНН."""
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Файл не выбран", "err")
        return redirect(url_for("priorities"))
    if not f.filename.lower().endswith(".xlsx"):
        flash("Поддерживаются только Excel-файлы в формате .xlsx", "err")
        return redirect(url_for("priorities"))
    conn = None
    try:
        conn = storage.connect()
        summary = priority_companies.import_xlsx(f, conn)
        logger.info(
            "priority_companies_imported sheet=%s added=%s duplicates=%s invalid=%s",
            summary["sheet"], summary["added"], summary["duplicates"], summary["invalid"],
        )
        message = f"Добавлено компаний: {summary['added']}"
        if summary["duplicates"]:
            message += f"; уже были в списке: {summary['duplicates']}"
        if summary["invalid"]:
            message += f"; пропущено строк без корректного названия или ИНН: {summary['invalid']}"
        flash(message)
    except priority_companies.PriorityCompanyImportError as exc:
        logger.warning("priority_companies_import_rejected error=%s", exc)
        flash(str(exc), "err")
    except Exception as exc:  # noqa: BLE001
        logger.exception("priority_companies_import_failed")
        flash(f"Не удалось импортировать компании: {exc}", "err")
    finally:
        if conn is not None:
            conn.close()
    return redirect(url_for("priorities"))


@app.route("/irrelevant")
def irrelevant():
    """Вкладка QA: тендеры, которые классификатор счёл нерелевантными (для проверки/фидбэка)."""
    conn = storage.connect()
    q = (request.args.get("q") or "").strip()
    page = request.args.get("page", type=int) or 1
    rel_map, not_pursued = _load_meta(conn)
    pindex = _priority_index(conn)
    rows = _mark_priority(_annotate(storage.query_tenders(conn, limit=None)), pindex)
    rows = [t for t in rows
            if _passes_company_filter(t) and _not_expired(t)
            and (not _is_relevant_eff(t, rel_map) or t["tender_id"] in not_pursued)]
    conn.close()
    for t in rows:
        t["np"] = t["tender_id"] in not_pursued
    if q:
        ql = q.lower()
        rows = [t for t in rows if ql in ((t.get("title") or "") + " "
                + (t.get("subject") or "") + " " + (t.get("customer") or "")).lower()]
    rows.sort(key=lambda x: (x.get("days_left") if x.get("days_left") is not None else 10 ** 9))
    total_found = len(rows)
    total_pages = max(1, (total_found + PER_PAGE - 1) // PER_PAGE)
    page = min(max(1, page), total_pages)
    page_rows = rows[(page - 1) * PER_PAGE:(page - 1) * PER_PAGE + PER_PAGE]
    return render_template("irrelevant.html", active="irrelevant", tenders=page_rows,
                           q=q, total_found=total_found, page=page, total_pages=total_pages)


@app.route("/tender/<tender_id>")
def tender(tender_id):
    conn = storage.connect()
    t = storage.get_tender(conn, tender_id)
    if not t:
        conn.close()
        flash("Тендер не найден", "err")
        return redirect(url_for("tenders"))
    row = conn.execute("SELECT relevance, not_pursued FROM tender_meta WHERE tender_id = ?",
                       (tender_id,)).fetchone()
    doc_analysis = _document_analysis(conn, tender_id)
    pindex = _priority_index(conn)
    conn.close()
    t = _annotate([t])[0]
    t["is_priority"] = _is_priority(t, pindex)
    t["document_analysis"] = doc_analysis
    override = row["relevance"] if row else None
    not_pursued = bool(row["not_pursued"]) if row else False
    auto_rel = directions.is_relevant(t)
    eff_rel = (override == "relevant") or (override is None and auto_rel)
    ret = (request.args.get("ret") or "").strip()
    if ret.startswith("/") and not ret.startswith("//") and "\\" not in ret:
        back_url = ret
    else:
        back_url = (url_for("tenders", direction=t.get("direction"))
                    if t.get("direction") else url_for("tenders"))
    norm = ret.split("?", 1)[0]
    back_label = {
        "/": "Назад на главную",
        "/favorites": "Назад в избранное",
        "/employees": "Назад к сотрудникам",
        "/tasks": "Назад к задачам",
        "/irrelevant": "Назад к нерелевантным",
    }.get(norm, "Назад к списку")
    return render_template("tender.html", active="tenders", t=t, back_url=back_url,
                           back_label=back_label, override=override, eff_rel=eff_rel,
                           not_pursued=not_pursued,
                           document_request_text=build_document_request(t))


@app.route("/tender/<tender_id>/analyze-documents", methods=["POST"])
def tender_analyze_documents(tender_id):
    """Принимает набор текстовых документов, анализирует и сохраняет только результат."""
    conn = storage.connect()
    exists = conn.execute("SELECT 1 FROM tenders WHERE tender_id = ?", (tender_id,)).fetchone()
    if not exists:
        conn.close()
        abort(404)
    try:
        documents, text = read_uploads(request.files.getlist("documents"))
        result = analyze_documents(
            text, request.form.get("include_recommendations") == "1",
            [document["name"] for document in documents],
        )
        documents = persist_uploads(documents, tender_id)
        _save_document_analysis(conn, tender_id, documents, result)
    except DocumentAnalysisError as exc:
        logger.warning("document_analysis_rejected tender_id=%s error=%s", tender_id, exc)
        flash(str(exc), "err")
    finally:
        conn.close()
    if 'result' in locals():
        logger.info(
            "document_analysis_completed tender_id=%s analyzer=%s documents=%s",
            tender_id, result["analyzer"], len(documents),
        )
        source = "OpenAI" if result["analyzer"] == "openai" else "локальный экспресс-анализ"
        flash(f"Документы проанализированы: {source}.")
    ret = (request.form.get("ret") or "").strip()
    if ret.startswith("/") and not ret.startswith("//") and "\\" not in ret:
        return redirect(url_for("tender", tender_id=tender_id, ret=ret))
    return redirect(url_for("tender", tender_id=tender_id))


@app.route("/tender/<tender_id>/analysis-document/<stored_name>")
def tender_analysis_document(tender_id, stored_name):
    """Открывает только файл, принадлежащий сохранённому анализу тендера."""
    conn = storage.connect()
    record = _document_analysis(conn, tender_id)
    conn.close()
    documents = record.get("documents", []) if record else []
    document = next((item for item in documents if item.get("stored_name") == stored_name), None)
    if not document:
        abort(404)
    return send_from_directory(
        document_upload_dir(tender_id), stored_name,
        as_attachment=False, download_name=document.get("name") or stored_name,
    )


@app.route("/favorites")
def favorites():
    """Избранное — общий список сохранённых тендеров команды (метка = кто, этап = статус)."""
    metka = request.args.get("metka") or None
    stage_f = request.args.get("stage") or None
    conn = storage.connect()
    stages = _load_stages(conn)
    saves = {}
    for r in conn.execute("SELECT f.tender_id, u.username FROM user_favorites f "
                          "JOIN users u ON u.id = f.user_id ORDER BY f.created_at"):
        saves.setdefault(r["tender_id"], []).append(r["username"])
    employees = sorted({u for names in saves.values() for u in names})
    raw = []
    for tid in saves:
        tt = storage.get_tender(conn, tid)
        if tt:
            raw.append(tt)
    pindex = _priority_index(conn)
    conn.close()
    rows = _mark_priority(_annotate(raw), pindex)
    for t in rows:
        t["savers"] = saves.get(t["tender_id"], [])
        t["stage"] = stages.get(t["tender_id"])
        t["stage_label"] = STAGE_LABELS.get(t["stage"])
        t["expired"] = t.get("days_left") is not None and t["days_left"] < 0
    if metka:
        rows = [t for t in rows if metka in t["savers"]]
    if stage_f:
        rows = [t for t in rows if t.get("stage") == stage_f]
    rows.sort(key=lambda x: (x.get("days_left") if x.get("days_left") is not None else 10 ** 9))
    return render_template("favorites.html", active="favorites", tenders=rows,
                           employees=employees, metka=metka, stage_f=stage_f,
                           stage_labels=STAGE_LABELS)


@app.route("/analytics")
def analytics():
    conn = storage.connect()
    rel_map, not_pursued = _load_meta(conn)
    stages = _load_stages(conn)
    pindex = _priority_index(conn)
    all_rows = _mark_priority(_annotate(storage.query_tenders(conn, limit=None)), pindex)
    conn.close()

    pool = [r for r in all_rows
            if _passes_company_filter(r) and _not_expired(r)]
    relevant_rows = [r for r in pool if _is_relevant_eff(r, rel_map)]
    irrelevant_rows = [r for r in pool if not _is_relevant_eff(r, rel_map)]
    relevant = len(relevant_rows)
    passed = sum(1 for r in relevant_rows
                 if stages.get(r["tender_id"]) in ("passed_dar", "passed_other"))
    rejected = sum(1 for r in relevant_rows if stages.get(r["tender_id"]) == "rejected_dar")
    not_pursued_n = sum(1 for r in relevant_rows if r["tender_id"] in not_pursued)

    cards = [
        {"label": "Найдено подходящих тендеров", "value": relevant, "cls": "p1"},
        {"label": "Было передано дальше", "value": passed, "cls": "p2"},
        {"label": "Отклонено после внутренней квалификации", "value": rejected, "cls": "p3"},
        {"label": "Компания не пошла", "value": not_pursued_n, "cls": "p4"},
    ]

    # распределение по 4 типам: комм / комм.лиц / гос.лиц / гос
    B = {"com": 0, "com_lic": 0, "gov_lic": 0, "gov": 0}
    for r in relevant_rows:
        lic, pt = r.get("is_license"), r.get("ptype")
        if pt == "com":
            B["com_lic" if lic else "com"] += 1
        elif pt == "gov":
            B["gov_lic" if lic else "gov"] += 1
    legend = [("com", "Коммерческие", "#C3B4EF"), ("com_lic", "Коммерческие лицензионные", "#EFA9C6"),
              ("gov_lic", "Государственные лицензионные", "#9BD9A5"), ("gov", "Государственные", "#A9C7EF")]
    typed_total = sum(B.values())
    circ = 2 * math.pi * 80
    segs, cum = [], 0.0
    for key, name, color in legend:
        cnt = B[key]
        if typed_total and cnt:
            seg = (cnt / typed_total) * circ
            segs.append({"color": color, "dash": f"{seg:.1f} {circ - seg:.1f}", "offset": f"{-cum:.1f}"})
            cum += seg
    legend_rows = [{"name": name, "color": color, "count": B[key]} for key, name, color in legend]

    irr_total = len(irrelevant_rows)
    irr_passed = sum(1 for r in irrelevant_rows
                     if stages.get(r["tender_id"]) in ("passed_dar", "passed_other"))

    return render_template("analytics.html", active="analytics", cards=cards,
                           donut_total=typed_total, segs=segs, legend=legend_rows,
                           irr_total=irr_total, irr_passed=irr_passed)


@app.route("/profile", methods=["GET", "POST"])
def profile():
    if request.method == "POST":
        f = request.form
        icp = load_icp()
        icp["name"] = f.get("name", "").strip() or icp.get("name")
        icp["competencies"] = f.get("competencies", "").strip()
        icp["keywords_any"] = _lines(f.get("keywords_any"))
        icp["stop_words"] = _lines(f.get("stop_words"))
        save_icp(icp)
        try:
            n = rescore_all(icp, use_llm=USE_LLM_SCORING)
            flash(f"Профиль сохранён, баллы пересчитаны: {n} тендеров")
        except Exception as e:  # noqa: BLE001
            flash(f"Профиль сохранён, но пересчёт не удался: {e}", "err")
        return redirect(url_for("profile"))

    icp = load_icp()
    return render_template(
        "profile.html", active="profile", subtab="profile",
        name=icp.get("name", ""),
        competencies=icp.get("competencies", ""),
        min_revenue_h=company_size.revenue_human_value(company_size.MIN_REVENUE),
        keywords_any=_lines_text(icp.get("keywords_any")),
        stop_words=_lines_text(icp.get("stop_words")),
        types=[(k, directions.name_of(k)) for k in directions.all_keys(include_other=False)])


@app.route("/employees")
def employees():
    """Сотрудники компании и тендеры, которые они забрали в избранное."""
    conn = storage.connect()
    users = conn.execute("SELECT id, username FROM users ORDER BY username").fetchall()
    data = []
    for u in users:
        rows = conn.execute(
            "SELECT t.* FROM tenders t JOIN user_favorites f ON f.tender_id = t.tender_id "
            "WHERE f.user_id = ? ORDER BY t.score DESC, f.created_at DESC", (u["id"],)).fetchall()
        favs = _annotate([storage._row_to_dict(r) for r in rows])
        data.append({"id": u["id"], "username": u["username"],
                     "is_me": u["id"] == session.get("user_id"), "favs": favs})
    conn.close()
    return render_template("employees.html", active="profile", subtab="employees",
                           employees=data)


SETTINGS_DEFAULTS = {
    "n_new_email": "1", "n_new_site": "1",
    "n_fav_email": "0", "n_fav_site": "1",
    "n_remind_email": "1", "n_remind_site": "0",
    "n_license_email": "1", "n_license_site": "1",
    "n_priority_email": "1", "n_priority_site": "1",
    "n_weekly_email": "1", "n_weekly_site": "0",
    "timezone": "Московское время UTC +3",
    "check_freq": "1", "check_win1": "В 9 – 10", "check_win2": "В 12 – 13", "check_win3": "В 16 – 17",
    "notify_freq": "1", "notify_win": "В 8 – 9",
    "email": "",
    "extra_contacts": "",
}


def _load_settings():
    conn = storage.connect()
    saved = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM app_settings")}
    conn.close()
    s = dict(SETTINGS_DEFAULTS)
    s.update(saved)
    if not (s.get("email") or "").strip():
        s["email"] = notification_service.smtp_config()["recipient"]
    return s


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        toggles = ["n_new_email", "n_new_site", "n_fav_email", "n_fav_site",
                   "n_remind_email", "n_remind_site",
                   "n_license_email", "n_license_site",
                   "n_priority_email", "n_priority_site",
                   "n_weekly_email", "n_weekly_site"]
        vals = {k: ("1" if request.form.get(k) else "0") for k in toggles}
        for k in ("timezone", "check_freq", "check_win1", "check_win2", "check_win3",
                  "notify_freq", "notify_win", "email", "extra_contacts"):
            vals[k] = request.form.get(k, SETTINGS_DEFAULTS[k])
        conn = storage.connect()
        for k, v in vals.items():
            conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (k, v))
        conn.commit()
        conn.close()
        flash("Настройки сохранены")
        return redirect(url_for("settings"))
    return render_template("settings.html", active="settings", s=_load_settings(),
                           smtp_ready=notification_service.smtp_ready(),
                           smtp_env_path=str(notification_service.ENV_PATH),
                           windows=["В 8 – 9", "В 9 – 10", "В 12 – 13", "В 16 – 17", "В 18 – 19"])


@app.route("/settings/contacts", methods=["POST"])
def settings_contacts():
    value = (request.form.get("extra_contacts") or "").strip()
    conn = storage.connect()
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
        ("extra_contacts", value),
    )
    conn.commit()
    conn.close()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True)
    return redirect(url_for("settings") + "#profile")


@app.route("/settings/account", methods=["POST"])
def settings_account():
    """Обновляет фото, логин и пароль текущего пользователя."""
    uid = session["user_id"]
    username = (request.form.get("username") or "").strip()
    current_password = request.form.get("current_password") or ""
    new_password = request.form.get("new_password") or ""
    password_confirm = request.form.get("password_confirm") or ""
    remove_avatar = request.form.get("remove_avatar") == "1"
    upload = request.files.get("avatar")

    conn = storage.connect()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not user:
        conn.close()
        session.clear()
        return redirect(url_for("login"))

    username_changed = username != user["username"]
    password_changed = bool(new_password or password_confirm)
    if len(username) < 2:
        conn.close()
        flash("Логин должен содержать не менее 2 символов", "err")
        return redirect(url_for("settings") + "#profile")
    if username_changed or password_changed:
        if not current_password or not check_password_hash(user["password_hash"], current_password):
            conn.close()
            flash("Чтобы изменить логин или пароль, укажите текущий пароль", "err")
            return redirect(url_for("settings") + "#profile")
    if password_changed:
        if len(new_password) < 4:
            conn.close()
            flash("Новый пароль должен содержать не менее 4 символов", "err")
            return redirect(url_for("settings") + "#profile")
        if new_password != password_confirm:
            conn.close()
            flash("Новый пароль и подтверждение не совпадают", "err")
            return redirect(url_for("settings") + "#profile")
    if username_changed and conn.execute(
            "SELECT 1 FROM users WHERE username = ? AND id <> ?", (username, uid)
    ).fetchone():
        conn.close()
        flash("Такой логин уже занят", "err")
        return redirect(url_for("settings") + "#profile")

    avatar_data = None
    avatar_mime = None
    avatar_changed = False
    if upload and upload.filename:
        avatar_data = upload.read(3 * 1024 * 1024 + 1)
        if len(avatar_data) > 3 * 1024 * 1024:
            conn.close()
            flash("Фото профиля должно быть не больше 3 МБ", "err")
            return redirect(url_for("settings") + "#profile")
        avatar_mime = _image_mime(avatar_data)
        if not avatar_mime:
            conn.close()
            flash("Поддерживаются фотографии PNG, JPG, GIF и WEBP", "err")
            return redirect(url_for("settings") + "#profile")
        avatar_changed = True

    updates = ["username = ?"]
    values = [username]
    if password_changed:
        updates.append("password_hash = ?")
        values.append(generate_password_hash(new_password))
    if remove_avatar:
        updates.extend(["avatar = NULL", "avatar_mime = NULL"])
    elif avatar_changed:
        updates.extend(["avatar = ?", "avatar_mime = ?"])
        values.extend([avatar_data, avatar_mime])
    values.append(uid)
    conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", values)
    conn.commit()
    saved = conn.execute("SELECT username, avatar_mime FROM users WHERE id = ?", (uid,)).fetchone()
    conn.close()
    session["username"] = saved["username"]
    session["has_avatar"] = bool(saved["avatar_mime"])
    flash("Данные учётной записи сохранены")
    return redirect(url_for("settings") + "#profile")


@app.route("/settings/avatar", methods=["POST"])
def settings_avatar():
    """Сохраняет выбранное фото сразу, отдельно от логина и пароля."""
    upload = request.files.get("avatar")
    if not upload or not upload.filename:
        return jsonify(ok=False, error="Выберите фотографию"), 400
    avatar_data = upload.read(3 * 1024 * 1024 + 1)
    if len(avatar_data) > 3 * 1024 * 1024:
        return jsonify(ok=False, error="Фото профиля должно быть не больше 3 МБ"), 400
    avatar_mime = _image_mime(avatar_data)
    if not avatar_mime:
        return jsonify(ok=False, error="Поддерживаются фотографии PNG, JPG, GIF и WEBP"), 400

    uid = session["user_id"]
    conn = storage.connect()
    exists = conn.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone()
    if not exists:
        conn.close()
        session.clear()
        return jsonify(ok=False, error="Пользователь не найден"), 404
    conn.execute(
        "UPDATE users SET avatar = ?, avatar_mime = ? WHERE id = ?",
        (avatar_data, avatar_mime, uid),
    )
    conn.commit()
    conn.close()
    session["has_avatar"] = True
    return jsonify(ok=True, avatar_url=url_for("user_avatar", user_id=uid))


@app.route("/user/<int:user_id>/avatar")
def user_avatar(user_id):
    conn = storage.connect()
    row = conn.execute("SELECT avatar, avatar_mime FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if not row or not row["avatar"] or not row["avatar_mime"]:
        abort(404)
    response = Response(bytes(row["avatar"]), mimetype=row["avatar_mime"])
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/settings/test-email", methods=["POST"])
def settings_test_email():
    """Проверяет локальные SMTP-настройки отдельным безопасным письмом."""
    recipient = (request.form.get("email") or _load_settings().get("email") or "").strip()
    try:
        notification_service.send_test_email(recipient)
        flash(f"Тестовое письмо отправлено на {recipient}")
    except Exception as exc:  # noqa: BLE001
        flash(f"Не удалось отправить тестовое письмо: {exc}", "err")
    return redirect(url_for("settings"))


# ============================================================================
#  Действия (POST)
# ============================================================================
@app.route("/tender/<tender_id>/delete", methods=["POST"])
def delete_tender_route(tender_id):
    conn = storage.connect()
    ok = storage.delete_tender(conn, tender_id)
    conn.close()
    logger.info("tender_deleted tender_id=%s deleted=%s", tender_id, ok)
    flash("Тендер удалён" if ok else "Тендер не найден", "ok" if ok else "err")
    return redirect(url_for("tenders"))


@app.route("/tender/<tender_id>/favorite", methods=["POST"])
def toggle_favorite(tender_id):
    uid = session["user_id"]
    conn = storage.connect()
    mine = conn.execute(
        "SELECT 1 FROM user_favorites WHERE user_id = ? AND tender_id = ?",
        (uid, tender_id)).fetchone()
    if mine:
        conn.execute("DELETE FROM user_favorites WHERE user_id = ? AND tender_id = ?",
                     (uid, tender_id))
        # снимаем и уведомление «взял в работу», чтобы оно не устаревало
        conn.execute("DELETE FROM fav_notifications WHERE tender_id = ? AND actor_id = ?",
                     (tender_id, uid))
    else:
        taken = conn.execute(
            "SELECT user_id FROM user_favorites WHERE tender_id = ?", (tender_id,)).fetchone()
        if taken and taken["user_id"] != uid:
            conn.close()
            if request.accept_mimetypes.best == "application/json":
                return jsonify(ok=False, error="Этот тендер уже забрал другой сотрудник"), 409
            flash("Этот тендер уже забрал другой сотрудник", "err")
            return redirect(request.referrer or url_for("tenders"))
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("INSERT INTO user_favorites (user_id, tender_id, created_at) VALUES (?,?,?)",
                     (uid, tender_id, now))
        t = storage.get_tender(conn, tender_id)
        conn.execute(
            "INSERT INTO fav_notifications "
            "(actor_id, actor_name, tender_id, tender_title, created_at) VALUES (?,?,?,?,?)",
            (uid, session.get("username"), tender_id, (t.get("title") if t else None), now))
    conn.commit()
    conn.close()
    if request.accept_mimetypes.best == "application/json":
        return jsonify(ok=True, favorite=not bool(mine))
    return redirect(request.referrer or url_for("tenders"))


@app.route("/tender/<tender_id>/relevance", methods=["POST"])
def set_relevance(tender_id):
    """Ручная корректировка релевантности (обратная связь по подбору)."""
    value = request.form.get("value")
    conn = storage.connect()
    conn.execute("INSERT OR IGNORE INTO tender_meta (tender_id) VALUES (?)", (tender_id,))
    if value in ("relevant", "irrelevant"):
        conn.execute("UPDATE tender_meta SET relevance = ? WHERE tender_id = ?",
                     (value, tender_id))
        flash("Спасибо за отметку — это поможет улучшить подбор")
    else:  # авто
        conn.execute("UPDATE tender_meta SET relevance = NULL WHERE tender_id = ?", (tender_id,))
        flash("Возвращена автоматическая оценка")
    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for("tenders"))


@app.route("/tender/<tender_id>/stage", methods=["POST"])
def set_stage(tender_id):
    """Этап тендера в избранном (передано/отклонено/передано др.деп.)."""
    value = request.form.get("value") or ""
    conn = storage.connect()
    conn.execute("INSERT OR IGNORE INTO tender_meta (tender_id) VALUES (?)", (tender_id,))
    if value in STAGE_LABELS:
        conn.execute("UPDATE tender_meta SET stage = ? WHERE tender_id = ?", (value, tender_id))
    else:
        conn.execute("UPDATE tender_meta SET stage = NULL WHERE tender_id = ?", (tender_id,))
    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for("favorites"))


@app.route("/tender/<tender_id>/not_pursued", methods=["POST"])
def toggle_not_pursued(tender_id):
    """Отметка «не пошли» — компания решила не участвовать (для воронки)."""
    conn = storage.connect()
    row = conn.execute("SELECT not_pursued FROM tender_meta WHERE tender_id = ?",
                       (tender_id,)).fetchone()
    newval = 0 if (row and row["not_pursued"]) else 1
    conn.execute("INSERT OR IGNORE INTO tender_meta (tender_id) VALUES (?)", (tender_id,))
    conn.execute("UPDATE tender_meta SET not_pursued = ? WHERE tender_id = ?",
                 (newval, tender_id))
    conn.commit()
    conn.close()
    flash("Отмечено «не пошли»" if newval else "Возвращено в работу")
    return redirect(url_for("tenders") if newval else (request.referrer or url_for("tenders")))


@app.route("/import/kontur", methods=["POST"])
def import_kontur():
    """Загрузка Excel-выгрузки Контур.Закупок через общий конвейер скоринга."""
    upload = request.files.get("kontur_file")
    if not upload or not upload.filename:
        return jsonify({"error": "Выберите Excel-файл Контур.Закупок."}), 400
    if not upload.filename.lower().endswith(".xlsx"):
        return jsonify({"error": "Поддерживаются только выгрузки Контур.Закупок в формате .xlsx."}), 400

    filename = upload.filename.replace("\\", "/").rsplit("/", 1)[-1]
    uploaded_at = datetime.now().isoformat(timespec="seconds")
    uploader_id = session.get("user_id")
    uploader_name = session.get("username") or "Неизвестный пользователь"

    with _kontur_import_lock:
        if any(job["status"] in {"queued", "running"} for job in _kontur_import_jobs.values()):
            return jsonify({"error": "Загрузка Excel уже выполняется."}), 409
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
        temp_path = temp.name
        temp.close()
        upload.save(temp_path)
        job_id = uuid.uuid4().hex
        _kontur_import_jobs[job_id] = {
            "status": "queued", "found": 0, "processed": 0,
            "llm_scored": 0, "remaining": 0,
        }
    logger.info("kontur_import_queued job_id=%s filename=%s", job_id, upload.filename)

    def update_progress(progress: dict[str, int]) -> None:
        with _kontur_import_lock:
            _kontur_import_jobs[job_id].update(progress)

    def run_job() -> None:
        with _kontur_import_lock:
            _kontur_import_jobs[job_id]["status"] = "running"
        try:
            with open(temp_path, "rb") as source:
                summary = import_kontur_xlsx(
                    source, use_llm=USE_LLM_SCORING, progress_callback=update_progress,
                )
            current_settings = _load_settings()
            smtp = notification_service.smtp_config()
            notification_conn = storage.connect()
            try:
                notification_service.create_new_tender_notifications(
                    notification_conn, summary.get("new_ids", []),
                    site_enabled=current_settings["n_new_site"] == "1",
                    email_enabled=current_settings["n_new_email"] == "1",
                    recipient=current_settings.get("email") or smtp["recipient"],
                    base_url=smtp["base_url"], relevant_min=RELEVANT_MIN, top_min=TOP_MIN,
                )
            finally:
                notification_conn.close()
            if current_settings["n_new_email"] == "1":
                notification_service.dispatch_email_outbox()
            _record_upload_history(
                uploader_id, uploader_name, uploaded_at, filename, summary.get("sheet")
            )
            with _kontur_import_lock:
                _kontur_import_jobs[job_id].update(status="complete", summary=summary)
            logger.info(
                "kontur_import_completed job_id=%s parsed=%s kept=%s new=%s updated=%s",
                job_id, summary.get("parsed"), summary.get("kept"),
                summary.get("new"), summary.get("updated"),
            )
        except Exception as exc:  # noqa: BLE001
            with _kontur_import_lock:
                _kontur_import_jobs[job_id].update(status="error", error=str(exc))
            logger.exception("kontur_import_failed job_id=%s", job_id)
        finally:
            os.unlink(temp_path)

    _kontur_import_executor.submit(run_job)
    return jsonify({"job_id": job_id}), 202


@app.route("/import/kontur/<job_id>/status")
def import_kontur_status(job_id):
    with _kontur_import_lock:
        job = _kontur_import_jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Задача импорта не найдена."}), 404
        return jsonify(job)


@app.route("/rescore", methods=["POST"])
def rescore():
    try:
        n = rescore_all(load_icp())
        flash(f"Баллы пересчитаны: {n} тендеров")
    except Exception as e:  # noqa: BLE001
        flash(f"Ошибка пересчёта: {e}", "err")
    return redirect(request.referrer or url_for("settings"))


# ============================================================================
#  Задачи (таск-менеджер)
# ============================================================================
@app.route("/tasks")
def tasks():
    conn = storage.connect()
    rows = conn.execute(
        "SELECT * FROM app_tasks ORDER BY done ASC, "
        "CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, "
        "(due_date IS NULL), due_date ASC, id DESC").fetchall()
    all_tasks = _annotate_tasks([dict(r) for r in rows])
    tender_opts = storage.top_by_score(conn, limit=60, min_score=1)
    prefill_id = request.args.get("tender")
    prefill_title = ""
    if prefill_id:
        pt = storage.get_tender(conn, prefill_id)
        if pt:
            prefill_title = f"Подготовить заявку: {(pt.get('title') or '')[:60]}"
            if prefill_id not in [o.get("tender_id") for o in tender_opts]:
                tender_opts = [pt] + tender_opts
        else:
            prefill_id = None
    conn.close()
    return render_template("tasks.html", active="tasks",
                           active_tasks=[t for t in all_tasks if not t["done"]],
                           done_tasks=[t for t in all_tasks if t["done"]],
                           tender_opts=tender_opts,
                           prefill_id=prefill_id, prefill_title=prefill_title)


@app.route("/tasks/add", methods=["POST"])
def task_add():
    title = (request.form.get("title") or "").strip()
    if not title:
        flash("Введите название задачи", "err")
        return redirect(url_for("tasks"))
    tender_id = request.form.get("tender_id") or None
    due = request.form.get("due_date") or None
    priority = request.form.get("priority") or "normal"
    if priority not in _PRIORITY_LABELS:
        priority = "normal"
    tender_title = None
    conn = storage.connect()
    if tender_id:
        pt = storage.get_tender(conn, tender_id)
        tender_title = pt.get("title") if pt else None
        if pt is None:
            tender_id = None
    conn.execute(
        "INSERT INTO app_tasks (title, tender_id, tender_title, due_date, priority, done, created_at) "
        "VALUES (?,?,?,?,?,0,?)",
        (title, tender_id, tender_title, due, priority,
         datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()
    flash("Задача добавлена")
    return redirect(url_for("tasks"))


@app.route("/tasks/<int:task_id>/toggle", methods=["POST"])
def task_toggle(task_id):
    conn = storage.connect()
    conn.execute("UPDATE app_tasks SET done = CASE WHEN done=1 THEN 0 ELSE 1 END WHERE id = ?",
                 (task_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("tasks"))


@app.route("/tasks/<int:task_id>/delete", methods=["POST"])
def task_delete(task_id):
    conn = storage.connect()
    conn.execute("DELETE FROM app_tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("tasks"))


@app.route("/notifications/read")
def notif_read():
    """Отмечает уведомление прочитанным для текущего пользователя и ведёт на его цель."""
    nid = request.args.get("id", type=int)
    to = (request.args.get("to") or "").strip()
    uid = session.get("user_id")
    if nid and uid:
        conn = storage.connect()
        if request.args.get("source") == "site":
            conn.execute(
                "INSERT OR IGNORE INTO site_notif_seen (user_id, notification_id) VALUES (?, ?)",
                (uid, nid),
            )
        else:
            conn.execute("INSERT OR IGNORE INTO notif_seen (user_id, notif_id) VALUES (?, ?)",
                         (uid, nid))
        conn.commit()
        conn.close()
    if to.startswith("/") and not to.startswith("//") and "\\" not in to:
        return redirect(to)
    return redirect(url_for("home"))


if __name__ == "__main__":
    app.run(debug=True, port=5000)
