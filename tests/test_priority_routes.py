from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import storage


class PriorityRoutesTests(unittest.TestCase):
    def test_search_button_filters_and_delete_is_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = str(Path(tmp) / "test.db")
            original_connect = storage.connect

            def temp_connect(path=database):
                return original_connect(database)

            sys.modules.pop("app", None)
            with patch("storage.connect", side_effect=temp_connect):
                webapp = importlib.import_module("app")
                conn = storage.connect()
                conn.execute(
                    "INSERT INTO priority_companies (name, inn) VALUES (?, ?)",
                    ("Альфа", "7707083893"),
                )
                conn.execute(
                    "INSERT INTO priority_companies (name, inn) VALUES (?, ?)",
                    ("Бета", "7736050003"),
                )
                conn.commit()
                company_id = conn.execute(
                    "SELECT id FROM priority_companies WHERE inn = ?", ("7707083893",)
                ).fetchone()[0]
                conn.close()

                webapp.app.config["TESTING"] = True
                client = webapp.app.test_client()
                with client.session_transaction() as session:
                    session["user_id"] = 1
                    session["username"] = "test"
                    session["has_avatar"] = False

                response = client.get("/priorities?q=7707083893")
                page = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn("Альфа", page)
                self.assertNotIn("Бета", page)
                self.assertIn(">Найти</button>", page)
                self.assertNotIn("/priorities/add", page)

                response = client.post(f"/priorities/{company_id}/delete")
                self.assertEqual(response.status_code, 302)
                self.assertTrue(
                    response.headers.get("Location", "").endswith("/priorities"),
                    response.headers.get("Location"),
                )
                conn = storage.connect()
                try:
                    remaining = conn.execute(
                        "SELECT COUNT(*) FROM priority_companies WHERE id = ?",
                        (company_id,),
                    ).fetchone()[0]
                finally:
                    conn.close()
                self.assertEqual(remaining, 0)

                conn = storage.connect()
                storage.save_scored(conn, [{
                    "tender_id": "kontur:request-documents",
                    "number": "42",
                    "title": "Поставка аналитической платформы",
                    "url": "https://example.test/tender/42",
                    "subject": "Поставка аналитической платформы",
                    "customer": "ООО Тест",
                    "deadline": "2099-08-24T17:30:00",
                    "source": "kontur-excel",
                    "score": 75,
                    "verdict": "take",
                    "reasons": ["Тестовая причина"],
                    "labels": [],
                }])
                conn.close()
                response = client.get("/tender/kontur:request-documents")
                page = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn("Добрый день, коллеги!", page)
                self.assertIn("Копировать", page)
                self.assertNotIn("Здравствуйте!", page)
                self.assertNotIn(">Отправить</button>", page)
                self.assertNotIn("placeholder=\"tenders@company.ru\"", page)


if __name__ == "__main__":
    unittest.main()
