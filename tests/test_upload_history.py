from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import storage


class UploadHistoryTests(unittest.TestCase):
    def test_home_and_history_show_latest_successful_upload(self):
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
                    "INSERT INTO upload_history "
                    "(user_id, username, uploaded_at, filename, sheet_name) VALUES (?,?,?,?,?)",
                    (1, "Мария", "2026-08-24T14:35:00", "тендеры.xlsx", "Список 1"),
                )
                conn.execute(
                    "INSERT INTO upload_history "
                    "(user_id, username, uploaded_at, filename, sheet_name) VALUES (?,?,?,?,?)",
                    (2, "Иван", "2026-07-01T10:00:00", "старый.xlsx", "Архив"),
                )
                conn.commit()
                deleted = webapp._prune_upload_history(
                    conn, now=webapp.datetime.fromisoformat("2026-08-24T15:00:00")
                )
                conn.close()
                self.assertEqual(deleted, 1)

                webapp.app.config["TESTING"] = True
                client = webapp.app.test_client()
                with client.session_transaction() as session:
                    session["user_id"] = 1
                    session["username"] = "Мария"
                    session["has_avatar"] = False

                home = client.get("/").get_data(as_text=True)
                self.assertIn("Последняя загрузка: 24.08.2026 14:35", home)
                self.assertIn("Журнал загрузок", home)

                response = client.get("/upload-history")
                page = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 200)
                for value in ("Пользователь", "Дата загрузки", "файл", "Шаблон",
                              "Мария", "тендеры.xlsx", "Список 1"):
                    self.assertIn(value, page)
                self.assertNotIn("старый.xlsx", page)

                # Журнал общий: запись Марии видна в сессии другого пользователя.
                with client.session_transaction() as session:
                    session["user_id"] = 2
                    session["username"] = "Иван"
                    session["has_avatar"] = False
                shared_page = client.get("/upload-history").get_data(as_text=True)
                self.assertIn("Мария", shared_page)
                self.assertIn("тендеры.xlsx", shared_page)

                # Открытие журнала не пишет в SQLite и работает при активной записи импорта.
                lock_conn = storage.connect()
                try:
                    lock_conn.execute("BEGIN IMMEDIATE")
                    locked_response = client.get("/upload-history")
                    self.assertEqual(locked_response.status_code, 200)
                    self.assertIn("тендеры.xlsx", locked_response.get_data(as_text=True))
                finally:
                    lock_conn.rollback()
                    lock_conn.close()


if __name__ == "__main__":
    unittest.main()
