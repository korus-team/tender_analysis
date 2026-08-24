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
                now = webapp.datetime.now().replace(microsecond=0)
                latest = now - webapp.timedelta(minutes=25)
                stale = now - webapp.timedelta(days=31)
                conn = storage.connect()
                conn.execute(
                    "INSERT INTO upload_history "
                    "(user_id, username, uploaded_at, filename, sheet_name) VALUES (?,?,?,?,?)",
                    (1, "Мария", latest.isoformat(), "тендеры.xlsx", "Список 1"),
                )
                conn.execute(
                    "INSERT INTO upload_history "
                    "(user_id, username, uploaded_at, filename, sheet_name) VALUES (?,?,?,?,?)",
                    (2, "Иван", stale.isoformat(), "старый.xlsx", "Архив"),
                )
                conn.commit()
                deleted = webapp._prune_upload_history(conn, now=now)
                conn.close()
                self.assertEqual(deleted, 1)

                webapp.app.config["TESTING"] = True
                client = webapp.app.test_client()
                with client.session_transaction() as session:
                    session["user_id"] = 1
                    session["username"] = "Мария"
                    session["has_avatar"] = False

                home = client.get("/").get_data(as_text=True)
                self.assertIn(
                    f"Последняя загрузка: {latest.strftime('%d.%m.%Y %H:%M')}", home
                )
                self.assertIn("Журнал загрузок", home)

                response = client.get("/upload-history")
                page = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 200)
                for value in ("Пользователь", "Дата загрузки", "файл", "Шаблон",
                              "Мария", "тендеры.xlsx", "Список 1"):
                    self.assertIn(value, page)
                self.assertNotIn("старый.xlsx", page)

                # Сбой необязательного аудита не должен менять результат импорта.
                with patch("storage.connect", side_effect=RuntimeError("database is locked")), \
                        patch.object(webapp.logger, "exception") as log_exception:
                    webapp._record_upload_history(
                        1, "Мария", latest.isoformat(), "тендеры.xlsx", "Список 1"
                    )
                log_exception.assert_called_once()
