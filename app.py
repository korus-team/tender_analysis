# -*- coding: utf-8 -*-
"""
Ассистент для анализа тендеров — веб-интерфейс в дизайне DAR.

Работает поверх существующего бэкенда проекта (storage, scoring, main,
enrich, icp_config) — эти файлы менять не нужно.

Запуск в PyCharm: правой кнопкой -> Run 'app', затем http://127.0.0.1:5000
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from functools import wraps

from flask import (Flask, flash, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import storage
import directions
import company_size
from main import run_ingest, rescore_all
from enrich import enrich_pending
from scoring import theme_score
from icp_config import load_icp, save_icp

app = Flask(__name__)
app.secret_key = "dar-tender-assistant-local"

# --- пороги и настройки отображения -----------------------------------------
RELEVANT_MIN = 60      # «подходит нашей компании»
TOP_MIN = 85           # «наиболее подходящее»
NOTIFY_MIN_SCORE = 60  # порог: уведомления только для тендеров с баллом не ниже
INGEST_MAX_PAGES = 50  # сколько страниц собирать
ENRICH_MIN_SCORE = 50  # какие тендеры обогащать
PER_PAGE = 20          # тендеров на страницу списка

_BLOCK_RE = re.compile(r"^(заказчик|организатор)\s*:?\s*", re.I)
_PLACEHOLDER_RE = re.compile(r"[\u2580-\u259F]+")  # символы «Block Elements»: ░ ▒ ▓ █ …


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
    c = _PLACEHOLDER_RE.sub("", c)        # убрать плашки ░▒▓█ (rostender прячет имя)
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
        days, dfmt = None, "—"
        dl = t.get("deadline")
        if dl:
            try:
                d = datetime.fromisoformat(dl)
                days = (d - now).days
                dfmt = d.strftime("%d.%m.%Y")
            except (ValueError, TypeError):
                pass
        t["days_left"] = days
        t["deadline_fmt"] = dfmt
        t["days_fmt"] = _days_fmt(days)
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
        if det and det.get("eis_number"):
            t["ptype"], t["ptype_label"] = "gov", "Госзакупка (ФЗ)"
        elif t.get("enriched_at"):
            t["ptype"], t["ptype_label"] = "com", "Коммерческая"
        else:
            t["ptype"], t["ptype_label"] = None, None
        # уровень соответствия
        _apply_level(t)
        # цена / заказчик / риски
        t["price_fmt"] = fmt_price(t.get("price_rub"))
        t["customer"] = _clean_customer(t.get("customer"))
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
        "created_at TEXT)")
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


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return {"id": uid, "username": session.get("username", "")}


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
                "passed_other": "Передано др. деп."}


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
    return directions.is_relevant(t)


# --- приоритетные компании (заменяют «профиль компании») ---
_PRIORITY_SEED = [
    ("ПАО Сбербанк", "7707083893"), ("ПАО Газпром", "7736050003"),
    ("ПАО НК Роснефть", "7706107510"), ("ПАО ЛУКОЙЛ", "7708004767"),
    ("Банк ВТБ (ПАО)", "7702070139"), ("ОАО РЖД", "7708503727"),
    ("ПАО Ростелеком", "7707049388"), ("ПАО ГМК Норильский никель", "8401005730"),
    ("ПАО НЛМК", "4823006703"), ("ПАО Северсталь", "3528000597"),
    ("ПАО ММК", "7414003633"), ("ПАО Татнефть", "1644003838"),
    ("ПАО Сургутнефтегаз", "8602060555"), ("ПАО Транснефть", "7706061801"),
    ("ПАО НОВАТЭК", "6316031581"), ("АО Тандер (Магнит)", "2309085638"),
    ("X5 Retail Group", "7728632689"), ("ПАО МТС", "7740000076"),
    ("ПАО МегаФон", "7812014560"), ("ПАО ВымпелКом", "7713076301"),
    ("ПАО Аэрофлот", "7712040126"), ("Газпром нефть", "5504036333"),
    ("ПАО СИБУР Холдинг", "7727547261"), ("ГК Ростех", "7704274402"),
    ("ГК Росатом", "7706413348"), ("ПАО Интер РАО", "2320109650"),
    ("ПАО РусГидро", "2460066195"), ("ПАО Россети", "7728662669"),
    ("АО Альфа-Банк", "7728168971"), ("Банк ГПБ (АО)", "7744001497"),
]


def _ensure_priorities_table():
    conn = storage.connect()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS priority_companies ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
        "inn TEXT, created_at TEXT)")
    n = conn.execute("SELECT COUNT(*) FROM priority_companies").fetchone()[0]
    if n == 0:  # предзаполняем крупными компаниями (можно почистить позже)
        now = datetime.now().isoformat(timespec="seconds")
        conn.executemany(
            "INSERT INTO priority_companies (name, inn, created_at) VALUES (?,?,?)",
            [(nm, inn, now) for nm, inn in _PRIORITY_SEED])
    conn.commit()
    conn.close()


def _norm_name(s):
    import re as _re
    s = (s or "").lower().replace("«", " ").replace("»", " ").replace('"', " ")
    s = _re.sub(r"\b(пао|оао|ао|зао|ооо|гк|банк|публичное акционерное общество)\b", " ", s)
    return _re.sub(r"[^\w]+", "", s)


def _priority_index(conn):
    """Возвращает (set нормализованных имён, set ИНН) приоритетных компаний."""
    names, inns = set(), set()
    for r in conn.execute("SELECT name, inn FROM priority_companies"):
        if r["name"]:
            names.add(_norm_name(r["name"]))
        if r["inn"]:
            inns.add(r["inn"].strip())
    return names, inns


def _is_priority(customer, pindex):
    names, _inns = pindex
    if not customer:
        return False
    n = _norm_name(customer)
    return any(pn and (pn in n or n in pn) for pn in names)


def _ensure_settings_table():
    conn = storage.connect()
    conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()


def _law_tag(t):
    """Тег закона по тексту закупки (лёгкая эвристика, где закон упомянут)."""
    txt = ((t.get("title") or "") + " " + (t.get("subject") or "")).lower()
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
_ensure_meta_table()
_ensure_priorities_table()
_ensure_settings_table()


@app.before_request
def _require_login():
    """Пускаем на страницы только авторизованных (кроме входа/регистрации/статики)."""
    if request.endpoint in ("login", "register", "static") or request.endpoint is None:
        return None
    if not session.get("user_id"):
        return redirect(url_for("login"))
    return None


@app.context_processor
def inject_globals():
    return {"current_user": current_user()}


@app.context_processor
def inject_notifications():
    """Уведомления: события «сотрудник взял тендер в работу» (свои не показываем)."""
    try:
        uid = session.get("user_id")
        if not uid:
            return {"notifications": [], "notif_count": 0,
                    "my_fav_ids": set(), "taken_by": {}}
        here = request.path
        conn = storage.connect()
        events = conn.execute(
            "SELECT n.id, n.actor_name, n.tender_id, n.tender_title "
            "FROM fav_notifications n "
            "JOIN user_favorites f ON f.tender_id = n.tender_id AND f.user_id = n.actor_id "
            "WHERE n.actor_id != ? ORDER BY n.id DESC LIMIT 40", (uid,)).fetchall()
        seen = {r[0] for r in conn.execute(
            "SELECT notif_id FROM notif_seen WHERE user_id = ?", (uid,))}
        my_favs = _my_fav_ids(conn, uid)
        taken_by = {r["tender_id"]: r["username"] for r in conn.execute(
            "SELECT f.tender_id, u.username FROM user_favorites f "
            "JOIN users u ON u.id = f.user_id")}
        conn.close()
        notes = []
        for e in events:
            nid = e["id"]
            target = url_for("tender", tender_id=e["tender_id"], ret=here)
            notes.append({
                "id": nid,
                "title": e["tender_title"] or "Тендер",
                "meta": f"{e['actor_name'] or 'Сотрудник'} взял(а) в работу",
                "url": url_for("notif_read", id=nid, to=target),
                "read": nid in seen,
            })
        return {"notifications": notes,
                "notif_count": sum(1 for n in notes if not n["read"]),
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
    sort = request.args.get("sort") or "ai"
    show = request.args.get("show") or "all"
    show_filters = request.args.get("filters") == "1"
    page = request.args.get("page", type=int) or 1

    rel_map, not_pursued = _load_meta(conn)
    pindex = _priority_index(conn)
    prio_companies = conn.execute("SELECT COUNT(*) FROM priority_companies").fetchone()[0]
    rows = _annotate(storage.query_tenders(conn, limit=None))
    rows = [t for t in rows
            if company_size.passes_revenue(t.get("customer")) and _not_expired(t)
            and _is_relevant_eff(t, rel_map) and t["tender_id"] not in not_pursued]
    conn.close()

    for t in rows:
        t["is_priority"] = _is_priority(t.get("customer"), pindex)
        t["law"] = _law_tag(t)
        t["warn"] = None
        dl = t.get("deadline")
        if dl:
            try:
                secs = (datetime.fromisoformat(dl) - datetime.now()).total_seconds()
                if 0 <= secs < 24 * 3600 and not t.get("is_license"):
                    h = int(secs // 3600)
                    t["warn"] = "Меньше часа" if h < 1 else f"Меньше {h + 1} часов"
            except (ValueError, TypeError):
                pass

    # счётчики (по всей отобранной выборке, до фильтров)
    stat_total = len(rows)
    stat_top = sum(1 for t in rows if (t.get("score") or 0) >= TOP_MIN)
    stat_lic = sum(1 for t in rows if t.get("is_license"))

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
        "home.html", active="home", now=datetime.now().strftime("%d.%m %H:%M"),
        stat_total=stat_total, stat_top=stat_top, stat_lic=stat_lic,
        prio_companies=prio_companies, tenders=page_rows,
        direction=direction, ptype=ptype, q=q, sort=sort, show=show, show_filters=show_filters,
        type_keys=directions.all_keys(include_other=False), dir_name=directions.name_of,
        total_found=total_found, page=page, total_pages=total_pages)


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
    rows = _annotate(storage.query_tenders(conn, limit=None))
    rows = [t for t in rows
            if company_size.passes_revenue(t.get("customer")) and _not_expired(t)
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
    have = {(_norm_name(c["name"])) for c in companies}
    suggestions = [{"name": nm, "inn": inn} for nm, inn in _PRIORITY_SEED
                   if _norm_name(nm) not in have][:6]
    if q:
        ql = q.lower()
        companies = [c for c in companies
                     if ql in (c["name"] or "").lower() or ql in (c["inn"] or "")]
    return render_template("priorities.html", active="priorities", companies=companies,
                           q=q, suggestions=suggestions)


@app.route("/priorities/add", methods=["POST"])
def priorities_add():
    name = (request.form.get("name") or "").strip()
    inn = (request.form.get("inn") or "").strip()
    if name or inn:
        conn = storage.connect()
        conn.execute("INSERT INTO priority_companies (name, inn, created_at) VALUES (?,?,?)",
                     (name or ("ИНН " + inn), inn or None,
                      datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        conn.close()
        flash("Компания добавлена в приоритетные")
    return redirect(url_for("priorities"))


@app.route("/priorities/<int:cid>/delete", methods=["POST"])
def priorities_delete(cid):
    conn = storage.connect()
    conn.execute("DELETE FROM priority_companies WHERE id = ?", (cid,))
    conn.commit()
    conn.close()
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
    """Импорт из Excel (.xlsx): колонки «Название» и «ИНН» (или наоборот)."""
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Файл не выбран", "err")
        return redirect(url_for("priorities"))
    try:
        import openpyxl
        wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
        ws = wb.active
        conn = storage.connect()
        now = datetime.now().isoformat(timespec="seconds")
        added = 0
        for row in ws.iter_rows(values_only=True):
            if not row:
                continue
            cells = [str(c).strip() for c in row if c not in (None, "")]
            if not cells:
                continue
            # ИНН — это набор из 10/12 цифр; остальное — название
            inn = next((c for c in cells if c.isdigit() and len(c) in (10, 12)), None)
            name = next((c for c in cells if c != inn), None) or (("ИНН " + inn) if inn else None)
            if (name and name.lower() in ("название", "наименование", "компания")):
                continue  # заголовок
            if name or inn:
                conn.execute("INSERT INTO priority_companies (name, inn, created_at) VALUES (?,?,?)",
                             (name, inn, now))
                added += 1
        conn.commit()
        conn.close()
        flash(f"Импортировано компаний: {added}")
    except ImportError:
        flash("Для импорта из Excel нужна библиотека openpyxl (pip install openpyxl)", "err")
    except Exception as e:  # noqa: BLE001
        flash(f"Не удалось разобрать файл: {e}", "err")
    return redirect(url_for("priorities"))


@app.route("/irrelevant")
def irrelevant():
    """Вкладка QA: тендеры, которые классификатор счёл нерелевантными (для проверки/фидбэка)."""
    conn = storage.connect()
    q = (request.args.get("q") or "").strip()
    page = request.args.get("page", type=int) or 1
    rel_map, not_pursued = _load_meta(conn)
    rows = _annotate(storage.query_tenders(conn, limit=None))
    rows = [t for t in rows
            if company_size.passes_revenue(t.get("customer")) and _not_expired(t)
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
    conn.close()
    t = _annotate([t])[0]
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
                           not_pursued=not_pursued)


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
    conn.close()
    rows = _annotate(raw)
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
    all_rows = _annotate(storage.query_tenders(conn, limit=None))
    conn.close()

    pool = [r for r in all_rows
            if company_size.passes_revenue(r.get("customer")) and _not_expired(r)]
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
            n = rescore_all(icp)
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
    "timezone": "Московское время UTC +3",
    "check_freq": "1", "check_win1": "В 9 – 10", "check_win2": "В 12 – 13", "check_win3": "В 16 – 17",
    "notify_freq": "1", "notify_win": "В 8 – 9",
    "email": "",
}


def _load_settings():
    conn = storage.connect()
    saved = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM app_settings")}
    conn.close()
    s = dict(SETTINGS_DEFAULTS)
    s.update(saved)
    return s


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        toggles = ["n_new_email", "n_new_site", "n_fav_email", "n_fav_site",
                   "n_remind_email", "n_remind_site"]
        vals = {k: ("1" if request.form.get(k) else "0") for k in toggles}
        for k in ("timezone", "check_freq", "check_win1", "check_win2", "check_win3",
                  "notify_freq", "notify_win", "email"):
            vals[k] = request.form.get(k, SETTINGS_DEFAULTS[k])
        conn = storage.connect()
        for k, v in vals.items():
            conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (k, v))
        conn.commit()
        conn.close()
        flash("Настройки сохранены")
        return redirect(url_for("settings"))
    return render_template("settings.html", active="settings", s=_load_settings(),
                           windows=["В 8 – 9", "В 9 – 10", "В 12 – 13", "В 16 – 17", "В 18 – 19"])


# ============================================================================
#  Действия (POST)
# ============================================================================
@app.route("/tender/<tender_id>/delete", methods=["POST"])
def delete_tender_route(tender_id):
    conn = storage.connect()
    ok = storage.delete_tender(conn, tender_id)
    conn.close()
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


@app.route("/ingest", methods=["POST"])
def ingest():
    """Одна кнопка: собрать из всех источников (с фильтром 10 млрд ₽) и обогатить."""
    try:
        s = run_ingest(max_pages=INGEST_MAX_PAGES, use_llm=False, write_json=True)
        if isinstance(s, dict):
            parts = [f"новых {s.get('new', 0)}"]
            if s.get("relevant") is not None:
                parts.append(f"профильных {s['relevant']}")
            if s.get("expired_removed"):
                parts.append(f"удалено просроченных {s['expired_removed']}")
            if s.get("enriched") is not None:
                parts.append(f"обогащено {s['enriched']}")
            flash("Сбор и обогащение завершены: " + ", ".join(parts))
        else:
            flash("Сбор завершён")
    except Exception as e:  # noqa: BLE001
        flash(f"Ошибка сбора: {e}", "err")
    return redirect(request.referrer or url_for("home"))


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
        conn.execute("INSERT OR IGNORE INTO notif_seen (user_id, notif_id) VALUES (?, ?)",
                     (uid, nid))
        conn.commit()
        conn.close()
    if to.startswith("/") and not to.startswith("//") and "\\" not in to:
        return redirect(to)
    return redirect(url_for("home"))


if __name__ == "__main__":
    app.run(debug=True, port=5000)