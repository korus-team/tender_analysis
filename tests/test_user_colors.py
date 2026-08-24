from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import storage


class UserColorTests(unittest.TestCase):
    def test_color_is_saved_and_used_for_taken_tenders(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = str(Path(tmp) / "test.db")
            original_connect = storage.connect

            def temp_connect(path=database):
                return original_connect(database)

            sys.modules.pop("app", None)
            with patch("storage.connect", side_effect=temp_connect):
                webapp = importlib.import_module("app")
                conn = storage.connect()
                user_id = conn.execute(
                    "INSERT INTO users "
                    "(username, password_hash, created_at, label_color) VALUES (?, ?, ?, ?)",
                    ("Алина", "not-used", "2026-08-24T12:00:00", "#1A73E8"),
                ).lastrowid
                storage.save_scored(conn, [{
                    "tender_id": "kontur:user-color",
                    "number": "77",
                    "title": "Поставка лицензий для тестирования",
                    "url": "https://example.test/tender/77",
                    "subject": "Поставка лицензий",
                    "customer": "Государственное бюджетное учреждение",
                    "deadline": "2099-08-24T17:30:00",
                    "source": "kontur-excel",
                    "score": 85,
                    "verdict": "take",
                    "reasons": ["Тестовая причина"],
                    "labels": ["license"],
                }])
                conn.execute(
                    "INSERT INTO user_favorites (user_id, tender_id, created_at) VALUES (?, ?, ?)",
                    (user_id, "kontur:user-color", "2026-08-24T12:05:00"),
                )
                conn.commit()
                conn.close()

                webapp.app.config["TESTING"] = True
                client = webapp.app.test_client()
                with client.session_transaction() as session:
                    session["user_id"] = user_id
                    session["username"] = "Алина"

                settings_page = client.get("/settings").get_data(as_text=True)
                self.assertIn('id="userLabelColor"', settings_page)
                self.assertIn('value="#1A73E8"', settings_page)

                home_page = client.get("/").get_data(as_text=True)
                self.assertIn("Взято: Алина", home_page)
                self.assertIn("--owner-color:#1A73E8", home_page)

                tenders_page = client.get("/tenders").get_data(as_text=True)
                self.assertIn("Взято: Алина", tenders_page)
                self.assertIn("trow--taken", tenders_page)

                favorites_page = client.get("/favorites").get_data(as_text=True)
                self.assertIn("Алина", favorites_page)
                self.assertIn("--owner-color:#1A73E8", favorites_page)

                employees_page = client.get("/employees").get_data(as_text=True)
                self.assertIn("Алина", employees_page)
                self.assertIn("--owner-color:#1A73E8", employees_page)

                tender_page = client.get(
                    "/tender/kontur:user-color"
                ).get_data(as_text=True)
                self.assertIn("Взято: Алина", tender_page)
                self.assertIn("--owner-color:#1A73E8", tender_page)

                response = client.post("/settings/color", data={"label_color": "#F4B400"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["color"], "#F4B400")
                self.assertEqual(response.get_json()["text_color"], "#191C1F")
                conn = storage.connect()
                stored = conn.execute(
                    "SELECT label_color FROM users WHERE id = ?", (user_id,)
                ).fetchone()[0]
                conn.close()
                self.assertEqual(stored, "#F4B400")

                response = client.post("/settings/color", data={"label_color": "purple"})
                self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
