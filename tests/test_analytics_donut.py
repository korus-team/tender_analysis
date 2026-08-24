from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import storage


class AnalyticsDonutTests(unittest.TestCase):
    def test_all_four_categories_have_matching_segments_and_legend_counts(self):
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
                    ("analytics-user", "not-used", "2026-08-24T12:00:00"),
                ).lastrowid
                conn.executemany(
                    "INSERT INTO priority_companies (name, inn, created_at) VALUES (?, ?, ?)",
                    [
                        ("Коммерческая А", "7707083893", "2026-08-24T12:00:00"),
                        ("Коммерческая Б", "7736050003", "2026-08-24T12:00:00"),
                    ],
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
                tenders = [
                    ({**common, "tender_id": "com", "number": "1",
                      "title": "Разработка информационной системы",
                      "subject": "Разработка информационной системы",
                      "customer": "Коммерческая А"},
                     {"customer_inn": "7707083893"}),
                    ({**common, "tender_id": "com-lic", "number": "2",
                      "title": "Поставка лицензий СУБД",
                      "subject": "Неисключительные права на использование СУБД",
                      "customer": "Коммерческая Б"},
                     {"customer_inn": "7736050003"}),
                    ({**common, "tender_id": "gov", "number": "3",
                      "title": "Внедрение информационной платформы",
                      "subject": "Внедрение информационной системы",
                      "customer": "ГБУ Государственный заказчик"},
                     {"eis_number": "3"}),
                    ({**common, "tender_id": "gov-lic", "number": "4",
                      "title": "Государственная поставка лицензий",
                      "subject": "Передача неисключительных прав на программное обеспечение",
                      "customer": "ФГБУ Лицензионный заказчик"},
                     {"eis_number": "4"}),
                ]
                storage.save_scored(conn, [record for record, _ in tenders])
                for record, details in tenders:
                    storage.save_enrichment(conn, record["tender_id"], {"details": details})
                conn.close()

                webapp.app.config["TESTING"] = True
                client = webapp.app.test_client()
                with client.session_transaction() as session:
                    session["user_id"] = user_id
                    session["username"] = "analytics-user"

                page = client.get("/analytics").get_data(as_text=True)
                expected = {
                    "com": ("Коммерческие", "#8F5BE8"),
                    "com_lic": ("Коммерческие лицензионные", "#F05B9D"),
                    "gov_lic": ("Государственные лицензионные", "#35B85A"),
                    "gov": ("Государственные", "#3F82D7"),
                }
                for key, (label, color) in expected.items():
                    with self.subTest(category=key):
                        self.assertEqual(page.count(f'data-category="{key}"'), 2)
                        self.assertIn(f'stroke="{color}"', page)
                        self.assertIn(f"{label} · 1", page)
                self.assertIn('<b>4</b>\n        <span>тендеров</span>', page)


if __name__ == "__main__":
    unittest.main()
