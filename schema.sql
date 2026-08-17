-- ============================================================================
--  DAR — структура базы данных (SQLite)
-- ============================================================================
--  Этот файл нужен ТОЛЬКО как справка и для ручного создания пустой базы.
--  При обычном запуске приложения база создаётся автоматически:
--    * таблицу "tenders" создаёт модуль storage.py;
--    * остальные таблицы создаёт app.py при старте (функции _ensure_*).
--  Поэтому саму базу (tenders.db) в репозиторий класть не нужно —
--  на чистой копии проекта она появится сама при первом запуске app.py.
--
--  Если всё же хотите создать пустую базу вручную из этого файла:
--    sqlite3 tenders.db < schema.sql
-- ============================================================================


-- ----------------------------------------------------------------------------
--  Тендеры. Структуру создаёт storage.py автоматически — здесь приведено
--  для справки (набор колонок; в storage.py могут быть уточнённые типы).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenders (
    tender_id             TEXT PRIMARY KEY,   -- внутренний id закупки
    number                TEXT,               -- номер тендера
    title                 TEXT,               -- заголовок
    url                   TEXT,               -- ссылка на площадку
    subject               TEXT,               -- предмет / описание
    customer              TEXT,               -- заказчик (на rostender часто скрыт)
    region                TEXT,
    location              TEXT,
    category              TEXT,
    price_rub             REAL,               -- начальная цена, ₽
    price_display         TEXT,               -- цена «как на площадке»
    published_at          TEXT,               -- дата публикации
    deadline              TEXT,               -- срок окончания подачи заявок (ISO)
    days_left             INTEGER,            -- дней до дедлайна (кэш)
    contract_security_pct TEXT,               -- обеспечение контракта
    bid_security_pct      TEXT,               -- обеспечение заявки
    source                TEXT,               -- источник (rostender.info и т.д.)
    parsed_at             TEXT,               -- когда собрано парсером
    score                 INTEGER,            -- оценка соответствия (0..100)
    verdict               TEXT,               -- вердикт скоринга (take/reject/...)
    reasons               TEXT,               -- факторы оценки (JSON-строка)
    labels                TEXT,               -- метки скоринга (JSON-строка)
    documents             TEXT,               -- документы после обогащения (JSON)
    details               TEXT,               -- доп. данные: ЕИС, аванс, СМП (JSON)
    status                TEXT,               -- служебный статус записи
    favorite              INTEGER DEFAULT 0,  -- устаревшее поле (не используется)
    enriched_at           TEXT,               -- когда обогащено
    first_seen            TEXT,               -- когда впервые увидено
    last_seen             TEXT,               -- когда последний раз встречено
    times_seen            INTEGER DEFAULT 1   -- сколько раз встречено
);


-- ----------------------------------------------------------------------------
--  Пользователи (многопользовательский режим). Создаётся app.py.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,              -- хэш пароля (werkzeug), не сам пароль
    created_at    TEXT
);


-- ----------------------------------------------------------------------------
--  Избранное / «взято в работу»: кто какой тендер сохранил. Создаётся app.py.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_favorites (
    user_id    INTEGER NOT NULL,
    tender_id  TEXT NOT NULL,
    created_at TEXT,
    PRIMARY KEY (user_id, tender_id)
);


-- ----------------------------------------------------------------------------
--  События для колокольчика («сотрудник взял тендер в работу»). Создаётся app.py.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fav_notifications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id     INTEGER NOT NULL,            -- кто совершил действие
    actor_name   TEXT,                        -- имя (для показа)
    tender_id    TEXT NOT NULL,
    tender_title TEXT,
    created_at   TEXT
);


-- ----------------------------------------------------------------------------
--  Отметки «прочитано» для уведомлений (по пользователю). Создаётся app.py.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notif_seen (
    user_id  INTEGER NOT NULL,
    notif_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, notif_id)
);


-- ----------------------------------------------------------------------------
--  Ручные пометки по тендеру: релевантность, «не пошли», этап. Создаётся app.py.
--    relevance   : 'relevant' / 'irrelevant' / NULL (авто-классификация)
--    not_pursued : 1 — компания решила не участвовать
--    stage       : 'passed_dar' / 'rejected_dar' / 'passed_other' / NULL
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tender_meta (
    tender_id   TEXT PRIMARY KEY,
    relevance   TEXT,
    not_pursued INTEGER DEFAULT 0,
    stage       TEXT
);


-- ----------------------------------------------------------------------------
--  Приоритетные компании (их тендеры подсвечиваются). Создаётся app.py и
--  предзаполняется списком крупных компаний при первом запуске.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS priority_companies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    inn        TEXT,
    created_at TEXT
);


-- ----------------------------------------------------------------------------
--  Настройки приложения (уведомления, расписание, e-mail). Создаётся app.py.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);


-- ----------------------------------------------------------------------------
--  Задачи (устаревший раздел, в текущем интерфейсе не используется).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    tender_id    TEXT,
    tender_title TEXT,
    due_date     TEXT,
    priority     TEXT DEFAULT 'normal',
    done         INTEGER DEFAULT 0,
    created_at   TEXT
);