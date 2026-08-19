# -*- coding: utf-8 -*-
"""Уведомления о новых тендерах: сайт и надёжная SMTP email-очередь."""
from __future__ import annotations

import hashlib
import html
import os
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote

import directions
import storage


ENV_PATH = Path(__file__).with_name(".env")


def load_local_env(path: Path = ENV_PATH) -> dict[str, str]:
    """Читает простой KEY=VALUE файл; системное окружение применится отдельно."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if ((value.startswith('"') and value.endswith('"')) or
                (value.startswith("'") and value.endswith("'"))):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def smtp_config() -> dict:
    """Возвращает публичные SMTP-настройки и секрет из окружения."""
    local = load_local_env()

    def value(key: str, default: str = "") -> str:
        return os.environ.get(key, local.get(key, default))

    try:
        port = int(value("SMTP_PORT", "587"))
    except ValueError:
        port = 587
    return {
        "host": value("SMTP_HOST").strip(),
        "port": port,
        "use_ssl": value("SMTP_USE_SSL", "0").strip().lower() not in
                   {"0", "false", "no", "off"},
        "use_tls": value("SMTP_USE_TLS", "1").strip().lower() not in
                   {"0", "false", "no", "off"},
        "username": value("SMTP_USERNAME").strip(),
        "password": value("SMTP_APP_PASSWORD").strip(),
        "from_email": value("EMAIL_FROM").strip(),
        "from_name": value("EMAIL_FROM_NAME", "DAR Tender Assistant").strip(),
        "recipient": value("EMAIL_TO").strip(),
        "base_url": value("APP_BASE_URL", "http://127.0.0.1:5000").rstrip("/"),
    }


def smtp_ready(config: dict | None = None) -> bool:
    config = config or smtp_config()
    return bool(config["host"] and config["port"] and config["username"] and
                config["password"] and (config["from_email"] or config["username"]))


def ensure_schema(conn) -> None:
    """Создаёт таблицы broadcast-уведомлений и отказоустойчивой email-очереди."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS site_notifications ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_key TEXT UNIQUE NOT NULL, "
        "kind TEXT NOT NULL, "
        "tender_id TEXT, "
        "tender_title TEXT, "
        "message TEXT, "
        "created_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS site_notif_seen ("
        "user_id INTEGER NOT NULL, "
        "notification_id INTEGER NOT NULL, "
        "PRIMARY KEY (user_id, notification_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS email_outbox ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_key TEXT UNIQUE NOT NULL, "
        "kind TEXT NOT NULL, "
        "recipient TEXT NOT NULL, "
        "subject TEXT NOT NULL, "
        "text_body TEXT NOT NULL, "
        "html_body TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'pending', "
        "attempts INTEGER NOT NULL DEFAULT 0, "
        "last_error TEXT, "
        "created_at TEXT NOT NULL, "
        "sent_at TEXT)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_outbox_status "
        "ON email_outbox(status, attempts, id)"
    )
    conn.commit()


def _tender_link(base_url: str, tender_id: str) -> str:
    return f"{base_url.rstrip('/')}/tender/{quote(tender_id, safe='')}"


def _deadline_text(tender: dict) -> str:
    deadline = tender.get("deadline")
    if not deadline:
        return "срок не указан"
    try:
        return datetime.fromisoformat(deadline).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return str(deadline)


def _build_digest(tenders: list[dict], base_url: str) -> tuple[str, str, str]:
    count = len(tenders)
    subject = f"DAR: новые подходящие тендеры — {count}"
    text_lines = [
        f"В DAR появилось новых подходящих тендеров: {count}.",
        "",
    ]
    html_items = []
    for tender in tenders:
        title = tender.get("title") or "Тендер без названия"
        customer = tender.get("customer") or "Заказчик не указан"
        score = int(tender.get("score") or 0)
        direction = directions.name_of(directions.classify(tender))
        deadline = _deadline_text(tender)
        link = _tender_link(base_url, tender["tender_id"])
        text_lines.extend([
            title,
            f"Заказчик: {customer}",
            f"Направление: {direction}; оценка: {score}; подача до: {deadline}",
            link,
            "",
        ])
        html_items.append(
            "<li style=\"margin:0 0 18px\">"
            f"<a href=\"{html.escape(link, quote=True)}\" "
            "style=\"font-weight:700;color:#5b43c9;text-decoration:none\">"
            f"{html.escape(title)}</a><br>"
            f"<span style=\"color:#5f6670\">{html.escape(customer)}</span><br>"
            f"{html.escape(direction)} · оценка {score} · подача до {html.escape(deadline)}"
            "</li>"
        )
    html_body = (
        "<!doctype html><html><body style=\"font-family:Arial,sans-serif;color:#15171a\">"
        f"<h2 style=\"margin-bottom:8px\">Новые подходящие тендеры: {count}</h2>"
        "<p style=\"color:#5f6670\">Они уже загружены и отсортированы в DAR.</p>"
        f"<ol style=\"padding-left:22px\">{''.join(html_items)}</ol>"
        "</body></html>"
    )
    return subject, "\n".join(text_lines), html_body


def is_notification_candidate(tender: dict, relevant_min: int = 60) -> bool:
    """Совпадает ли предмет с профилем и прошёл ли тендер итоговый порог."""
    return (directions.is_relevant(tender) and
            int(tender.get("score") or 0) >= relevant_min)


def prune_ineligible_site_notifications(conn, relevant_min: int = 60) -> int:
    """Удаляет старые уведомления, созданные прежним слишком широким правилом."""
    ensure_schema(conn)
    invalid_ids = []
    for row in conn.execute("SELECT id, tender_id FROM site_notifications").fetchall():
        tender = storage.get_tender(conn, row["tender_id"]) if row["tender_id"] else None
        if not tender or not is_notification_candidate(tender, relevant_min):
            invalid_ids.append(row["id"])
    if invalid_ids:
        conn.executemany(
            "DELETE FROM site_notif_seen WHERE notification_id = ?",
            [(notification_id,) for notification_id in invalid_ids],
        )
        conn.executemany(
            "DELETE FROM site_notifications WHERE id = ?",
            [(notification_id,) for notification_id in invalid_ids],
        )
        conn.commit()
    return len(invalid_ids)


def create_new_tender_notifications(conn, tender_ids, *, site_enabled: bool,
                                    email_enabled: bool, recipient: str,
                                    base_url: str, relevant_min: int = 60,
                                    top_min: int = 70,
                                    now: datetime | None = None) -> dict:
    """Создаёт события только для действительно новых профильных тендеров.

    Направление само по себе недостаточно: уведомляем только о тендерах, которые
    также прошли итоговый порог релевантности. В email из них попадают наиболее
    подходящие и лицензии.
    """
    ensure_schema(conn)
    unique_ids = list(dict.fromkeys(tid for tid in tender_ids if tid))
    tenders = []
    for tender_id in unique_ids:
        tender = storage.get_tender(conn, tender_id)
        if tender and is_notification_candidate(tender, relevant_min):
            tenders.append(tender)
    tenders.sort(key=lambda item: int(item.get("score") or 0), reverse=True)

    created_at = (now or datetime.now()).isoformat(timespec="seconds")
    site_created = 0
    if site_enabled:
        for tender in tenders:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO site_notifications "
                "(event_key, kind, tender_id, tender_title, message, created_at) "
                "VALUES (?, 'new_tender', ?, ?, ?, ?)",
                (f"new_tender:{tender['tender_id']}", tender["tender_id"],
                 tender.get("title"), "Новый подходящий тендер", created_at),
            )
            site_created += cursor.rowcount

    email_tenders = [
        tender for tender in tenders
        if int(tender.get("score") or 0) >= top_min or
        directions.classify(tender) == "license"
    ]
    outbox_created = 0
    outbox_id = None
    recipient = (recipient or "").strip()
    if email_enabled and email_tenders and recipient:
        ids_key = "\n".join(sorted(tender["tender_id"] for tender in email_tenders))
        digest_hash = hashlib.sha256(f"{recipient}\n{ids_key}".encode("utf-8")).hexdigest()
        subject, text_body, html_body = _build_digest(email_tenders, base_url)
        cursor = conn.execute(
            "INSERT OR IGNORE INTO email_outbox "
            "(event_key, kind, recipient, subject, text_body, html_body, created_at) "
            "VALUES (?, 'new_tenders', ?, ?, ?, ?, ?)",
            (f"new_tenders:{digest_hash}", recipient, subject, text_body, html_body, created_at),
        )
        outbox_created = cursor.rowcount
        if outbox_created:
            outbox_id = cursor.lastrowid
        else:
            row = conn.execute(
                "SELECT id FROM email_outbox WHERE event_key = ?",
                (f"new_tenders:{digest_hash}",),
            ).fetchone()
            outbox_id = row[0] if row else None
    conn.commit()
    return {
        "relevant": len(tenders),
        "site_created": site_created,
        "email_candidates": len(email_tenders),
        "outbox_created": outbox_created,
        "outbox_id": outbox_id,
        "email_missing_recipient": bool(email_enabled and email_tenders and not recipient),
    }


def _send_message(recipient: str, subject: str, text_body: str, html_body: str,
                  config: dict | None = None) -> None:
    config = config or smtp_config()
    if not smtp_ready(config):
        raise RuntimeError(
            f"не задан SMTP_APP_PASSWORD в локальном файле {ENV_PATH}"
        )

    message = EmailMessage()
    sender = config["from_email"] or config["username"]
    message["From"] = f"{config['from_name']} <{sender}>" if config["from_name"] else sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    ssl_context = ssl.create_default_context()
    if config.get("use_ssl"):
        smtp_connection = smtplib.SMTP_SSL(
            config["host"], config["port"], timeout=20, context=ssl_context
        )
    else:
        smtp_connection = smtplib.SMTP(config["host"], config["port"], timeout=20)

    with smtp_connection as smtp:
        smtp.ehlo()
        if not config.get("use_ssl") and config.get("use_tls"):
            smtp.starttls(context=ssl_context)
            smtp.ehlo()
        smtp.login(config["username"], config["password"])
        smtp.send_message(message)


def dispatch_email_outbox(conn=None, *, only_id: int | None = None,
                          limit: int = 10, config: dict | None = None) -> dict:
    """Пытается отправить очередь; ошибки сохраняются и не ломают импорт."""
    config = config or smtp_config()
    if not smtp_ready(config):
        return {
            "sent": 0, "failed": 0,
            "config_error": "Заполните SMTP-настройки и SMTP_APP_PASSWORD в локальном .env.",
        }

    own_connection = conn is None
    conn = conn or storage.connect()
    try:
        ensure_schema(conn)
        sql = (
            "SELECT * FROM email_outbox WHERE status IN ('pending', 'failed') "
            "AND attempts < 5"
        )
        params = []
        if only_id is not None:
            sql += " AND id = ?"
            params.append(only_id)
        sql += " ORDER BY id LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        sent = failed = 0
        errors = []
        for row in rows:
            try:
                _send_message(row["recipient"], row["subject"], row["text_body"],
                              row["html_body"], config=config)
                conn.execute(
                    "UPDATE email_outbox SET status='sent', attempts=attempts+1, "
                    "last_error=NULL, sent_at=? WHERE id=?",
                    (datetime.now().isoformat(timespec="seconds"), row["id"]),
                )
                sent += 1
            except Exception as exc:  # noqa: BLE001 - ошибка должна остаться в outbox
                error = str(exc)[:500]
                conn.execute(
                    "UPDATE email_outbox SET status='failed', attempts=attempts+1, "
                    "last_error=? WHERE id=?", (error, row["id"]),
                )
                failed += 1
                errors.append(error)
        conn.commit()
        return {"sent": sent, "failed": failed, "errors": errors}
    finally:
        if own_connection:
            conn.close()


def send_test_email(recipient: str, config: dict | None = None) -> None:
    recipient = (recipient or "").strip()
    if not recipient or "@" not in recipient:
        raise ValueError("укажите корректный адрес электронной почты")
    _send_message(
        recipient,
        "DAR: проверка почтовых уведомлений",
        "Почтовые уведомления DAR настроены правильно.",
        "<p><strong>Почтовые уведомления DAR настроены правильно.</strong></p>",
        config=config,
    )
