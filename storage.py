# -*- coding: utf-8 -*-
"""
Хранилище тендеров с дедупликацией и накоплением между запусками (ТЗ 3.2, 11.1).

Зачем:
  JSON-файлы перезаписываются каждый прогон — истории нет. База накапливает
  тендеры во времени: новый прогон не затирает старое, а обновляет уже известные
  и добавляет новые. Дедупликация — по номеру тендера (PRIMARY KEY).

Почему SQLite:
  Для прототипа/MVP этого достаточно, сервер не нужен, всё в одном файле tenders.db.
  По ТЗ (11.1) на боевом этапе меняется на PostgreSQL — интерфейс тех же функций
  сохранится, поменяется только подключение и SQL-диалект.

Что отслеживаем по каждому тендеру:
  first_seen  — когда впервые увидели,
  last_seen   — когда видели в последний раз,
  times_seen  — сколько раз попадался,
  status      — статус в работе отдела (новый/в работе/выиграли/...), живёт отдельно
                от парсинга и НЕ сбрасывается при повторном сборе (см. save_scored),
  плюс замечаем изменения (например, продление дедлайна — ТЗ 3.2).
"""

from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timedelta

DB_PATH = "tenders.db"

# --- Статусы тендера в работе отдела (ядро "рабочего места" из ТЗ 3.8) ---
# Ключи латиницей и стабильные (для кода и БД), подписи — для человека.
STATUS_NEW = "new"
STATUS_REVIEW = "review"
STATUS_IN_PROGRESS = "in_progress"
STATUS_WON = "won"
STATUS_LOST = "lost"
STATUS_REJECTED = "rejected"
DEFAULT_STATUS = STATUS_NEW

STATUS_LABELS = {
    STATUS_NEW: "Новый",
    STATUS_REVIEW: "На рассмотрении",
    STATUS_IN_PROGRESS: "В работе",
    STATUS_WON: "Выиграли",
    STATUS_LOST: "Проиграли",
    STATUS_REJECTED: "Отклонён",
}
# Порядок отображения доски.
STATUS_ORDER = [
    STATUS_NEW, STATUS_REVIEW, STATUS_IN_PROGRESS,
    STATUS_WON, STATUS_LOST, STATUS_REJECTED,
]

# Поля, которые могут меняться у уже известного тендера и которые обновляем.
MUTABLE = [
    "title", "subject", "customer", "region", "location", "category",
    "price_rub", "price_display", "published_at", "deadline",
    "contract_security_pct", "bid_security_pct",
    "score", "verdict", "reasons", "labels",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenders (
    tender_id             TEXT PRIMARY KEY,
    number                TEXT,
    title                 TEXT,
    url                   TEXT,
    subject               TEXT,
    customer              TEXT,
    region                TEXT,
    location              TEXT,
    category              TEXT,
    price_rub             INTEGER,
    price_display         TEXT,
    published_at          TEXT,
    deadline              TEXT,
    contract_security_pct REAL,
    bid_security_pct      REAL,
    source                TEXT,
    score                 INTEGER,
    verdict               TEXT,
    reasons               TEXT,   -- JSON-массив
    labels                TEXT,   -- JSON-массив
    first_seen            TEXT,
    last_seen             TEXT,
    times_seen            INTEGER DEFAULT 1,
    status                TEXT DEFAULT 'new',   -- статус в работе отдела
    status_updated_at     TEXT,                 -- когда меняли статус
    note                  TEXT,                 -- комментарий к тендеру
    documents             TEXT,                 -- JSON-список документов (обогащение)
    advance_pct           REAL,                 -- аванс, % (обогащение)
    enriched_at           TEXT,                 -- когда обогащали с детальной страницы
    details               TEXT                  -- JSON: номер ЕИС, срок контракта, СМП, способ
);
"""

# Колонки, которые могли отсутствовать в старой базе — добавим при подключении
# (безопасная миграция для уже созданного tenders.db).
_ADDED_COLUMNS = {
    "status": "TEXT DEFAULT 'new'",
    "status_updated_at": "TEXT",
    "note": "TEXT",
    "documents": "TEXT",
    "advance_pct": "REAL",
    "enriched_at": "TEXT",
    "details": "TEXT",
}

# Журнал смены статусов — для аналитики воронки (кто, когда, с чего на что).
HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS status_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tender_id   TEXT,
    old_status  TEXT,
    new_status  TEXT,
    changed_at  TEXT,
    note        TEXT
);
"""

REVENUE_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS company_revenue_cache (
    inn          TEXT PRIMARY KEY,
    company_name TEXT,
    revenue_rub  INTEGER,
    report_year  INTEGER,
    source       TEXT NOT NULL,
    status       TEXT NOT NULL,
    checked_at   TEXT NOT NULL
);
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    conn.execute(HISTORY_SCHEMA)
    conn.execute(REVENUE_CACHE_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Добавляет недостающие колонки в уже существующую базу (безопасно для старых tenders.db)."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(tenders)")}
    for col, ddl in _ADDED_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE tenders ADD COLUMN {col} {ddl}")
    conn.commit()


def _encode(record: dict, key: str):
    """Списки (reasons/labels) храним как JSON-строку."""
    val = record.get(key)
    if isinstance(val, (list, dict)):
        return json.dumps(val, ensure_ascii=False)
    return val


def save_scored(conn: sqlite3.Connection, scored: list[dict]) -> dict:
    """
    Сохраняет список оценённых тендеров с дедупликацией.
    Возвращает сводку: что новое, что обновлено, где изменился дедлайн/цена.
    """
    now = datetime.now().isoformat(timespec="seconds")
    new_ids: list[str] = []
    updated_ids: list[str] = []
    deadline_changes: list[tuple] = []
    price_changes: list[tuple] = []

    for r in scored:
        tid = r.get("tender_id")
        if not tid:
            continue

        existing = conn.execute(
            "SELECT deadline, price_rub FROM tenders WHERE tender_id = ?", (tid,)
        ).fetchone()

        if existing is None:
            # --- новый тендер: ставим статус "Новый" ---
            cols = (["tender_id", "number", "url", "source"] + MUTABLE
                    + ["first_seen", "last_seen", "times_seen", "status", "status_updated_at"])
            values = (
                [tid, r.get("number"), r.get("url"), r.get("source")]
                + [_encode(r, k) for k in MUTABLE]
                + [now, now, 1, DEFAULT_STATUS, now]
            )
            placeholders = ", ".join(["?"] * len(cols))
            conn.execute(
                f"INSERT INTO tenders ({', '.join(cols)}) VALUES ({placeholders})",
                values,
            )
            new_ids.append(tid)
        else:
            # --- уже известный: замечаем изменения и обновляем.
            #     ВАЖНО: статус НЕ трогаем — он живёт в работе отдела и не должен
            #     сбрасываться при повторном парсинге.
            if existing["deadline"] != r.get("deadline"):
                deadline_changes.append((tid, existing["deadline"], r.get("deadline")))
            if existing["price_rub"] != r.get("price_rub"):
                price_changes.append((tid, existing["price_rub"], r.get("price_rub")))

            set_clause = ", ".join(f"{k} = ?" for k in MUTABLE)
            values = [_encode(r, k) for k in MUTABLE] + [now, tid]
            conn.execute(
                f"UPDATE tenders SET {set_clause}, last_seen = ?, "
                f"times_seen = times_seen + 1 WHERE tender_id = ?",
                values,
            )
            updated_ids.append(tid)

    conn.commit()
    return {
        "new": new_ids,
        "updated": updated_ids,
        "deadline_changes": deadline_changes,
        "price_changes": price_changes,
    }


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Строка БД -> словарь, с распаковкой JSON-полей reasons/labels."""
    d = dict(row)
    for k in ("reasons", "labels"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (json.JSONDecodeError, TypeError):
                pass
    return d


def count_all(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]


def existing_ids(conn: sqlite3.Connection) -> set:
    """Все номера тендеров, уже лежащие в базе (для инкрементального сбора)."""
    return {row[0] for row in conn.execute("SELECT tender_id FROM tenders")}


def status_history(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    """Последние записи журнала смены статусов (с названием тендера)."""
    rows = conn.execute(
        "SELECT h.changed_at, h.tender_id, h.old_status, h.new_status, h.note, t.title "
        "FROM status_history h LEFT JOIN tenders t ON t.tender_id = h.tender_id "
        "ORDER BY h.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def funnel_stats(conn: sqlite3.Connection) -> dict:
    """
    Аналитика воронки: сколько тендеров сейчас в каждом статусе (по текущему статусу)
    плюс сколько всего смен и доля выигранных среди завершённых.
    """
    current = pipeline_counts(conn)
    total_changes = conn.execute("SELECT COUNT(*) FROM status_history").fetchone()[0]
    # сколько тендеров КОГДА-ЛИБО доходили до статуса (по журналу)
    ever = {}
    for st in (STATUS_IN_PROGRESS, STATUS_WON, STATUS_LOST):
        ever[st] = conn.execute(
            "SELECT COUNT(DISTINCT tender_id) FROM status_history WHERE new_status = ?",
            (st,),
        ).fetchone()[0]
    won, lost = ever[STATUS_WON], ever[STATUS_LOST]
    win_rate = round(100 * won / (won + lost)) if (won + lost) else None
    return {
        "current": current,
        "total_changes": total_changes,
        "ever": ever,
        "won": won,
        "lost": lost,
        "win_rate": win_rate,
    }


def get_tender(conn: sqlite3.Connection, tender_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM tenders WHERE tender_id = ?", (tender_id,)).fetchone()
    return _row_to_dict(row) if row else None


def set_status(conn: sqlite3.Connection, tender_id: str, status: str,
               note: str | None = None) -> dict:
    """
    Меняет статус тендера. Возвращает {'ok': True, 'old': ..., 'new': ...}
    либо {'ok': False, 'error': ...}.
    """
    if status not in STATUS_LABELS:
        return {"ok": False, "error": f"Неизвестный статус '{status}'. "
                                      f"Допустимо: {', '.join(STATUS_LABELS)}"}
    row = conn.execute("SELECT status FROM tenders WHERE tender_id = ?", (tender_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": f"Тендер {tender_id} не найден"}

    now = datetime.now().isoformat(timespec="seconds")
    if note is None:
        conn.execute(
            "UPDATE tenders SET status = ?, status_updated_at = ? WHERE tender_id = ?",
            (status, now, tender_id),
        )
    else:
        conn.execute(
            "UPDATE tenders SET status = ?, status_updated_at = ?, note = ? WHERE tender_id = ?",
            (status, now, note, tender_id),
        )
    # пишем в журнал только реальные переходы (старый статус != новый)
    if row["status"] != status:
        conn.execute(
            "INSERT INTO status_history (tender_id, old_status, new_status, changed_at, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (tender_id, row["status"], status, now, note),
        )
    conn.commit()
    return {"ok": True, "old": row["status"], "new": status}


def list_by_status(conn: sqlite3.Connection, status: str, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM tenders WHERE status = ? ORDER BY score DESC, last_seen DESC LIMIT ?",
        (status, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def pipeline_counts(conn: sqlite3.Connection) -> dict:
    """Сводка по доске: {статус: количество} в порядке STATUS_ORDER."""
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM tenders GROUP BY status").fetchall()
    counts = {(row["status"] or DEFAULT_STATUS): row["n"] for row in rows}
    return {s: counts.get(s, 0) for s in STATUS_ORDER}


def top_by_score(conn: sqlite3.Connection, limit: int = 10, min_score: int = 0) -> list[dict]:
    """Топ накопленных тендеров по баллу (например, для дайджеста уведомлений)."""
    rows = conn.execute(
        "SELECT * FROM tenders WHERE score >= ? ORDER BY score DESC, last_seen DESC LIMIT ?",
        (min_score, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def query_tenders(conn: sqlite3.Connection, status: str | None = None,
                  min_score: int | None = None, category: str | None = None,
                  region: str | None = None, search: str | None = None,
                  limit: int | None = 300) -> list[dict]:
    """Гибкая выборка под фильтры интерфейса. Пустые фильтры игнорируются.
    limit=None — вернуть все подходящие (для пагинации в приложении)."""
    sql = "SELECT * FROM tenders WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status = ?"; params.append(status)
    if min_score is not None:
        sql += " AND score >= ?"; params.append(min_score)
    if category:
        sql += " AND category = ?"; params.append(category)
    if region:
        sql += " AND region = ?"; params.append(region)
    if search:
        like = f"%{search.lower()}%"
        sql += " AND (LOWER(title) LIKE ? OR LOWER(customer) LIKE ? OR LOWER(subject) LIKE ?)"
        params += [like, like, like]
    sql += " ORDER BY score DESC, last_seen DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def delete_tenders(conn: sqlite3.Connection, tender_ids, commit: bool = True) -> int:
    """Удаляет тендеры и связанные служебные записи одним согласованным действием."""
    ids = list(dict.fromkeys(tid for tid in tender_ids if tid))
    removed = 0
    for tender_id in ids:
        if _table_exists(conn, "fav_notifications"):
            notification_ids = [row[0] for row in conn.execute(
                "SELECT id FROM fav_notifications WHERE tender_id = ?", (tender_id,)
            )]
            if notification_ids and _table_exists(conn, "notif_seen"):
                conn.executemany(
                    "DELETE FROM notif_seen WHERE notif_id = ?",
                    [(notification_id,) for notification_id in notification_ids],
                )
            conn.execute("DELETE FROM fav_notifications WHERE tender_id = ?", (tender_id,))
        if _table_exists(conn, "site_notifications"):
            site_notification_ids = [row[0] for row in conn.execute(
                "SELECT id FROM site_notifications WHERE tender_id = ?", (tender_id,)
            )]
            if site_notification_ids and _table_exists(conn, "site_notif_seen"):
                conn.executemany(
                    "DELETE FROM site_notif_seen WHERE notification_id = ?",
                    [(notification_id,) for notification_id in site_notification_ids],
                )
            conn.execute("DELETE FROM site_notifications WHERE tender_id = ?", (tender_id,))
        for table in ("user_favorites", "tender_meta", "status_history", "app_tasks"):
            if _table_exists(conn, table):
                conn.execute(f"DELETE FROM {table} WHERE tender_id = ?", (tender_id,))
        cur = conn.execute("DELETE FROM tenders WHERE tender_id = ?", (tender_id,))
        removed += cur.rowcount
    if commit:
        conn.commit()
    return removed


def delete_tender(conn: sqlite3.Connection, tender_id: str) -> bool:
    return delete_tenders(conn, [tender_id]) > 0


def update_score(conn: sqlite3.Connection, tender_id: str, score: int,
                 verdict: str, reasons: list, labels: list) -> None:
    """Обновляет только оценку (для пересчёта при смене профиля). Не трогает
    last_seen/times_seen/статус. Коммит — снаружи, после цикла."""
    conn.execute(
        "UPDATE tenders SET score = ?, verdict = ?, reasons = ?, labels = ? WHERE tender_id = ?",
        (score, verdict, json.dumps(reasons, ensure_ascii=False),
         json.dumps(labels, ensure_ascii=False), tender_id),
    )


def distinct_values(conn: sqlite3.Connection, column: str) -> list[str]:
    """Уникальные значения колонки для выпадающих фильтров (только разрешённые колонки)."""
    if column not in {"category", "region", "status"}:
        return []
    rows = conn.execute(
        f"SELECT DISTINCT {column} FROM tenders "
        f"WHERE {column} IS NOT NULL AND {column} != '' ORDER BY {column}"
    ).fetchall()
    return [r[0] for r in rows]


def tenders_to_enrich(conn: sqlite3.Connection, limit: int = 20,
                      min_score: int = 0) -> list[dict]:
    """Тендеры, которые ещё не обогащались (по убыванию балла — сначала важные)."""
    rows = conn.execute(
        "SELECT tender_id, url FROM tenders "
        "WHERE enriched_at IS NULL AND url IS NOT NULL AND score >= ? "
        "ORDER BY score DESC, last_seen DESC LIMIT ?",
        (min_score, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def count_to_enrich(conn: sqlite3.Connection, min_score: int = 0) -> int:
    """Сколько релевантных тендеров ещё не обогащено (для кнопки в интерфейсе)."""
    return conn.execute(
        "SELECT COUNT(*) FROM tenders "
        "WHERE enriched_at IS NULL AND url IS NOT NULL AND score >= ?",
        (min_score,),
    ).fetchone()[0]


def upcoming_deadlines(conn: sqlite3.Connection, within_days: int | None = None) -> list[dict]:
    """
    Тендеры с приближающимся дедлайном подачи, от самого срочного.
    Показываем только те, что ещё в работе (новый / на рассмотрении / в работе):
    просроченные и завершённые (выиграли/проиграли/отклонён) не нужны.
    within_days — окно в днях (None = все будущие). Сравнение по ISO-строкам
    работает как хронологическое (одинаковый формат дат).
    """
    now = datetime.now()
    params: list = [now.isoformat(timespec="seconds")]
    sql = ("SELECT * FROM tenders WHERE deadline IS NOT NULL AND deadline >= ? "
           "AND (status IS NULL OR status NOT IN ('won','lost','rejected'))")
    if within_days is not None:
        params.append((now + timedelta(days=within_days)).isoformat(timespec="seconds"))
        sql += " AND deadline <= ?"
    sql += " ORDER BY deadline ASC LIMIT 500"
    return [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def save_enrichment(conn: sqlite3.Connection, tender_id: str, data: dict) -> None:
    """Сохраняет данные с детальной страницы: документы, аванс, детали, цену (если её не было)."""
    now = datetime.now().isoformat(timespec="seconds")
    docs_json = json.dumps(data.get("documents") or [], ensure_ascii=False)
    details_json = json.dumps(data.get("details") or {}, ensure_ascii=False)
    # цену заполняем только если её ещё не было (не затираем цену из списка)
    if data.get("price_rub"):
        conn.execute(
            "UPDATE tenders SET price_rub = COALESCE(price_rub, ?) WHERE tender_id = ?",
            (data["price_rub"], tender_id),
        )
    conn.execute(
        "UPDATE tenders SET documents = ?, advance_pct = ?, details = ?, enriched_at = ? "
        "WHERE tender_id = ?",
        (docs_json, data.get("advance_pct"), details_json, now, tender_id),
    )
    conn.commit()
