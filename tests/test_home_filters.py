from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import storage


class HomeFiltersTests(unittest.TestCase):
    def test_special_filters_and_search_fields(self):
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
                    "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                    ("filter-user", "not-used", "2026-08-24T12:00:00"),
                ).lastrowid
                conn.execute(
                    "INSERT INTO priority_companies (name, inn, created_at) VALUES (?, ?, ?)",
                    ("Альфа", "7707083893", "2026-08-24T12:00:00"),
                )
                common = {
                    "url": "https://example.test/tender",
                    "deadline": "2099-08-24T17:30:00",
                    "source": "kontur-excel",
                    "score": 85,
                    "verdict": "take",
                    "reasons": ["Тестовая причина"],
                    "labels": [],
                }
                storage.save_scored(conn, [
                    {
                        **common,
                        "tender_id": "kontur:priority-filter",
                        "number": "PRIORITY-100",
                        "title": "Разработка информационной системы Альфа",
                        "subject": "Разработка информационной системы",
                        "customer": "ООО Альфа",
                        "details": {"customer_inn": "7707083893", "eis_number": "100"},
                    },
                    {
                        **common,
                        "tender_id": "kontur:license-filter",
                        "number": "LICENSE-200",
                        "title": "Поставка лицензий СУБД",
                        "subject": "Неисключительные права на использование СУБД",
                        "customer": "ГБУ Лицензионный заказчик",
                        "details": {"eis_number": "200"},
                    },
                    {
                        **common,
                        "tender_id": "kontur:search-filter",
                        "number": "SEARCH-7788",
                        "title": "Внедрение информационной платформы Омега",
                        "subject": "Внедрение информационной системы",
                        "customer": "Уникальный заказчик Омега",
                        "details": {"eis_number": "300"},
                    },
                ])
                storage.save_enrichment(
                    conn, "kontur:priority-filter",
                    {"details": {"customer_inn": "7707083893", "eis_number": "100"}},
                )
                storage.save_enrichment(
                    conn, "kontur:license-filter",
                    {"details": {"eis_number": "200"}},
                )
                storage.save_enrichment(
                    conn, "kontur:search-filter",
                    {"details": {"eis_number": "300"}},
                )
                conn.commit()
                conn.close()

                webapp.app.config["TESTING"] = True
                client = webapp.app.test_client()
                with client.session_transaction() as session:
                    session["user_id"] = user_id
                    session["username"] = "filter-user"

                page = client.get("/").get_data(as_text=True)
                self.assertNotIn('name="direction"', page)
                self.assertNotIn('name="show"', page)
                self.assertNotIn("Показать:", page)
                self.assertIn('name="special"', page)
                self.assertIn("Приоритетные компании", page)
                self.assertIn("Лицензионные", page)

                priority_page = client.get("/?special=priority").get_data(as_text=True)
                self.assertIn("Разработка информационной системы Альфа", priority_page)
                self.assertNotIn("Поставка лицензий СУБД", priority_page)
                self.assertNotIn("информационной платформы Омега", priority_page)

                license_page = client.get("/?special=license").get_data(as_text=True)
                self.assertIn("Поставка лицензий СУБД", license_page)
                self.assertNotIn("информационной системы Альфа", license_page)
                self.assertNotIn("информационной платформы Омега", license_page)

                combined_page = client.get(
                    "/", query_string={"special": "license", "ptype": "gov"}
                ).get_data(as_text=True)
                self.assertIn("Поставка лицензий СУБД", combined_page)
                self.assertIn('<option value="license" selected>', combined_page)
                self.assertIn('<option value="gov" selected>', combined_page)

                for query in ("Омега", "7788", "Уникальный заказчик"):
                    with self.subTest(query=query):
                        search_page = client.get("/", query_string={"q": query}).get_data(
                            as_text=True
                        )
                        self.assertIn("Внедрение информационной платформы Омега", search_page)
                        self.assertNotIn("Разработка информационной системы Альфа", search_page)
                        self.assertNotIn("Поставка лицензий СУБД", search_page)


if __name__ == "__main__":
    unittest.main()
