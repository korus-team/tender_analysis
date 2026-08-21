from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

import priority_companies
import storage


def companies_workbook() -> io.BytesIO:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Компании"
    worksheet.append(["Служебная строка"])
    worksheet.append(["Название", "ИНН заказчика", "Комментарий", "Наименование компании"])
    worksheet.append(["Внутренняя запись 1", 7707083893, "важная", "ПАО Сбербанк"])
    worksheet.append(["Внутренняя запись 2", "7707083893", "дубликат", "Сбербанк"])
    worksheet.append(["Внутренняя запись 3", "не ИНН", "ошибка", "Компания без ИНН"])
    worksheet.append(["Внутренняя запись 4", "7736050003", "важная", "ПАО Газпром"])
    stream = io.BytesIO()
    workbook.save(stream)
    stream.seek(0)
    return stream


class PriorityCompaniesTests(unittest.TestCase):
    def test_import_finds_columns_among_extra_columns_and_deduplicates_by_inn(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = storage.connect(str(Path(tmp) / "test.db"))
            priority_companies.ensure_schema(conn)
            summary = priority_companies.import_xlsx(companies_workbook(), conn)

            self.assertEqual(summary["sheet"], "Компании")
            self.assertEqual(summary["added"], 2)
            self.assertEqual(summary["duplicates"], 1)
            self.assertEqual(summary["invalid"], 1)
            rows = conn.execute(
                "SELECT name, inn FROM priority_companies ORDER BY inn"
            ).fetchall()
            self.assertEqual(
                [(row["name"], row["inn"]) for row in rows],
                [("ПАО Сбербанк", "7707083893"), ("ПАО Газпром", "7736050003")],
            )
            conn.close()

    def test_priority_match_uses_only_exact_inn(self):
        inns = {"7707083893"}
        self.assertTrue(priority_companies.is_priority_tender(
            {"details": {"customer_inn": "7707083893"}, "customer": "Другое имя"}, inns,
        ))
        self.assertFalse(priority_companies.is_priority_tender(
            {"details": {"customer_inn": "7736050003"}, "customer": "ПАО Сбербанк"}, inns,
        ))
        self.assertFalse(priority_companies.is_priority_tender(
            {"customer": "ПАО Сбербанк"}, inns,
        ))

    def test_missing_required_headers_is_reported(self):
        workbook = Workbook()
        workbook.active.append(["Компания", "КПП"])
        stream = io.BytesIO()
        workbook.save(stream)
        stream.seek(0)
        with tempfile.TemporaryDirectory() as tmp:
            conn = storage.connect(str(Path(tmp) / "test.db"))
            with self.assertRaises(priority_companies.PriorityCompanyImportError):
                priority_companies.import_xlsx(stream, conn)
            conn.close()


if __name__ == "__main__":
    unittest.main()
