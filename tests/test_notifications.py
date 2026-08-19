from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import notification_service
import storage


def tender(tender_id: str, title: str, score: int) -> dict:
    return {
        "tender_id": tender_id,
        "number": tender_id,
        "title": title,
        "subject": title,
        "customer": "ПАО «Тест»",
        "deadline": "2099-08-24T17:30:00",
        "source": "kontur-excel",
        "score": score,
        "verdict": "take",
        "reasons": [],
        "labels": [],
    }


class FakeSMTP:
    sent_messages = []

    def __init__(self, host, port, timeout, context=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def ehlo(self):
        return None

    def starttls(self, context):
        return None

    def login(self, username, password):
        self.username = username

    def send_message(self, message):
        self.sent_messages.append(message)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = storage.connect(str(Path(self.tmp.name) / "test.db"))
        notification_service.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        FakeSMTP.sent_messages.clear()

    def test_creates_site_events_and_one_email_digest_without_duplicates(self):
        rows = [
            tender("kontur:software", "Разработка информационной системы", 75),
            tender("kontur:license", "Поставка лицензий Postgres Pro", 65),
            tender(
                "kontur:low-bi",
                "Выбор поставщика на поставку BI системы для нужд аптечной сети",
                40,
            ),
            tender("kontur:other", "Поставка офисных кресел", 95),
        ]
        saved = storage.save_scored(self.conn, rows)
        first = notification_service.create_new_tender_notifications(
            self.conn,
            saved["new"],
            site_enabled=True,
            email_enabled=True,
            recipient="test@example.com",
            base_url="http://127.0.0.1:5000",
            top_min=70,
        )
        second = notification_service.create_new_tender_notifications(
            self.conn,
            saved["new"],
            site_enabled=True,
            email_enabled=True,
            recipient="test@example.com",
            base_url="http://127.0.0.1:5000",
            top_min=70,
        )

        self.assertEqual(first["relevant"], 2)
        self.assertEqual(first["site_created"], 2)
        self.assertEqual(first["email_candidates"], 2)
        self.assertEqual(first["outbox_created"], 1)
        self.assertEqual(second["site_created"], 0)
        self.assertEqual(second["outbox_created"], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM site_notifications").fetchone()[0], 2
        )
        outbox = self.conn.execute("SELECT * FROM email_outbox").fetchone()
        self.assertEqual(outbox["status"], "pending")
        self.assertIn("Поставка лицензий Postgres Pro", outbox["text_body"])
        self.assertNotIn("аптечной сети", outbox["text_body"])
        self.assertNotIn("офисных кресел", outbox["text_body"])

    def test_prunes_old_notification_for_low_score_tender(self):
        storage.save_scored(
            self.conn,
            [tender(
                "kontur:low-bi",
                "Выбор поставщика на поставку BI системы для нужд аптечной сети",
                40,
            )],
        )
        self.conn.execute(
            "INSERT INTO site_notifications "
            "(event_key, kind, tender_id, tender_title, message, created_at) "
            "VALUES (?, 'new_tender', ?, ?, ?, ?)",
            ("legacy:low-bi", "kontur:low-bi", "BI система", "Новый подходящий тендер",
             "2026-08-19T12:00:00"),
        )
        notification_id = self.conn.execute(
            "SELECT id FROM site_notifications WHERE event_key='legacy:low-bi'"
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO site_notif_seen (user_id, notification_id) VALUES (?, ?)",
            (1, notification_id),
        )
        self.conn.commit()

        removed = notification_service.prune_ineligible_site_notifications(
            self.conn, relevant_min=60
        )

        self.assertEqual(removed, 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM site_notifications").fetchone()[0], 0
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM site_notif_seen").fetchone()[0], 0
        )

    def test_dispatch_marks_email_sent(self):
        storage.save_scored(
            self.conn,
            [tender("kontur:software", "Разработка информационной системы", 75)],
        )
        queued = notification_service.create_new_tender_notifications(
            self.conn,
            ["kontur:software"],
            site_enabled=False,
            email_enabled=True,
            recipient="test@example.com",
            base_url="http://127.0.0.1:5000",
            top_min=70,
        )
        config = {
            "host": "smtp.yandex.ru", "port": 465,
            "use_ssl": True, "use_tls": False,
            "username": "sender", "password": "app-password",
            "from_email": "sender@yandex.ru", "from_name": "DAR",
            "recipient": "test@example.com", "base_url": "http://127.0.0.1:5000",
        }
        with patch("notification_service.smtplib.SMTP_SSL", FakeSMTP):
            result = notification_service.dispatch_email_outbox(
                self.conn, only_id=queued["outbox_id"], config=config
            )

        self.assertEqual(result, {"sent": 1, "failed": 0, "errors": []})
        row = self.conn.execute("SELECT status, attempts FROM email_outbox").fetchone()
        self.assertEqual((row["status"], row["attempts"]), ("sent", 1))
        self.assertEqual(len(FakeSMTP.sent_messages), 1)

    def test_missing_app_password_keeps_email_pending(self):
        result = notification_service.dispatch_email_outbox(
            self.conn,
            config={
                "host": "smtp.yandex.ru", "port": 465,
                "use_ssl": True, "use_tls": False,
                "username": "sender", "password": "",
                "from_email": "sender@yandex.ru", "from_name": "DAR",
                "recipient": "test@example.com", "base_url": "http://127.0.0.1:5000",
            },
        )
        self.assertEqual(result["sent"], 0)
        self.assertIn("config_error", result)


if __name__ == "__main__":
    unittest.main()
