from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook

import storage
import directions
from kontur_excel import KonturExcelError, import_kontur_xlsx, parse_kontur_xlsx


HEADERS = [
    "Номер", "ИКЗ", "Название", "НМЦ", "Обеспечение заявки",
    "Обеспечение контракта", "Аванс", "Валюта закупки", "Дата публикации",
    "Окончание приема заявок", "Проведение отбора", "Этап отбора", "Тип торгов",
    "Ссылка на ЕИС", "Способ отбора", "ЭТП", "СМП, СОНО", "Метка ",
    "Комментарий", "Ответственный", "Регион", "Название", "ИНН", "КПП",
    "Место поставки", "Место поставки из документов",
]


def workbook_bytes() -> io.BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "ДАР коммерческие"
    ws.append(["Закупка"] + [None] * 19 + ["Заказчик"])
    ws.append(HEADERS)
    ws.append([
        "4567280", "", "Внедрение аналитической платформы", 12_500_000, "", "", "",
        "RUB", datetime(2099, 8, 18, 15, 1), datetime(2099, 8, 24, 17, 30), "",
        "Подача заявок", "Коммерческие", "", "Запрос предложений", "B2B", "", "",
        "", "", "77 Москва", "ООО «Тестовая крупная компания»", "7709832989",
        "772501001", "Москва", "",
    ])
    ws["A3"].hyperlink = (
        "https://zakupki.kontur.ru/api/redirect?"
        "url=https%3A%2F%2Fzakupki.kontur.ru%2F4567280_458%3Futm_source%3Dexcel&token=temp"
    )
    ws["P3"].hyperlink = (
        "https://zakupki.kontur.ru/api/redirect?"
        "url=https%3A%2F%2Fwww.b2b-center.ru%2Ftender%2F4567280&token=temp"
    )
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


def deadline_workbook() -> io.BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.append(["Закупка"] + [None] * 19 + ["Заказчик"])
    ws.append(HEADERS)
    rows = [
        ("1", "Разработка информационной системы", datetime(2026, 8, 21, 10, 0)),
        ("2", "Лицензии JMIX", datetime(2026, 8, 19, 10, 0)),
        ("3", "Разработка интернет-портала", datetime(2026, 8, 22, 13, 0)),
        ("4", "Лицензии Postgres Pro", datetime(2026, 8, 17, 18, 0)),
    ]
    for number, title, deadline in rows:
        ws.append([
            number, "", title, 12_500_000, "", "", "", "RUB",
            datetime(2026, 8, 10, 9, 0), deadline, "", "Подача заявок",
            "Коммерческие", "", "Запрос предложений", "B2B", "", "", "", "",
            "77 Москва", "ООО «Тестовая крупная компания»", "7709832989",
            "772501001", "Москва", "",
        ])
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


class KonturExcelTests(unittest.TestCase):
    def test_parse_maps_fields_and_removes_redirect_token(self):
        parsed = parse_kontur_xlsx(workbook_bytes())
        item = parsed["items"][0]
        self.assertEqual(item["tender_id"], "kontur:4567280_458")
        self.assertEqual(item["customer"], "ООО «Тестовая крупная компания»")
        self.assertEqual(item["price_rub"], 12_500_000)
        self.assertEqual(item["deadline"], "2099-08-24T17:30:00")
        self.assertEqual(item["url"], "https://zakupki.kontur.ru/4567280_458?utm_source=excel")
        self.assertNotIn("token=", item["url"])
        self.assertEqual(item["_details"]["customer_inn"], "7709832989")
        self.assertEqual(item["_details"]["etp_url"], "https://www.b2b-center.ru/tender/4567280")
        self.assertIsNone(item["contract_security_pct"])
        self.assertIsNone(item["bid_security_pct"])

    def test_import_is_idempotent_and_keeps_existing_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = storage.connect(str(Path(tmp) / "test.db"))
            first = import_kontur_xlsx(workbook_bytes(), conn=conn)
            self.assertEqual((first["new"], first["updated"]), (1, 0))
            self.assertEqual(first["new_ids"], ["kontur:4567280_458"])
            storage.set_status(conn, "kontur:4567280_458", storage.STATUS_IN_PROGRESS)

            second = import_kontur_xlsx(workbook_bytes(), conn=conn)
            self.assertEqual((second["new"], second["updated"]), (0, 1))
            self.assertEqual(second["new_ids"], [])
            tender = storage.get_tender(conn, "kontur:4567280_458")
            self.assertEqual(tender["status"], storage.STATUS_IN_PROGRESS)
            self.assertEqual(tender["source"], "kontur-excel")
            self.assertEqual(json.loads(tender["details"])["etp"], "B2B")
            conn.close()

    def test_rejects_unrelated_workbook(self):
        wb = Workbook()
        wb.active.append(["Не", "Контур"])
        data = io.BytesIO()
        wb.save(data)
        data.seek(0)
        with self.assertRaises(KonturExcelError):
            parse_kontur_xlsx(data)

    def test_deadline_policy_keeps_all_nonexpired_tenders(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = storage.connect(str(Path(tmp) / "test.db"))
            initial = import_kontur_xlsx(
                deadline_workbook(), conn=conn, now=datetime(2026, 8, 10, 12, 0)
            )
            self.assertEqual(initial["kept"], 4)

            short_id = conn.execute(
                "SELECT tender_id FROM tenders WHERE title = ?",
                ("Разработка информационной системы",),
            ).fetchone()[0]
            conn.execute("CREATE TABLE user_favorites (user_id INTEGER, tender_id TEXT)")
            conn.execute("INSERT INTO user_favorites VALUES (1, ?)", (short_id,))
            conn.commit()

            current = import_kontur_xlsx(
                deadline_workbook(), conn=conn, now=datetime(2026, 8, 18, 12, 0)
            )
            self.assertEqual(current["kept"], 3)
            self.assertEqual(current["skipped_expired"], 1)
            self.assertEqual(current["removed_by_deadline"], 1)
            titles = {row[0] for row in conn.execute("SELECT title FROM tenders")}
            self.assertEqual(titles, {"Лицензии JMIX", "Разработка информационной системы", "Разработка интернет-портала"})
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_favorites").fetchone()[0], 1)
            conn.close()


class DirectionTests(unittest.TestCase):
    def test_context_avoids_known_false_positives(self):
        unrelated = [
            "Захват магнитный PML-300",
            "Шкаф для раздевалок ML-11-30",
            "Термокружка Palermo 480 ml",
            "Внесение изменений в лицензию на пользование недрами",
            "Средства индивидуальной защиты для сотрудников",
            "Мониторинг штрафов ГИБДД и платных дорог",
        ]
        for title in unrelated:
            with self.subTest(title=title):
                self.assertEqual(directions.classify({"title": title}), "other")

    def test_context_recognizes_profile_requests(self):
        expected = {
            "Поставка лицензий Postgres Pro": "license",
            "Разработка цифровой модели технологического процесса": "software",
            "Разработка системы консолидации отчетности": "bi",
            "AI Development Platform для автоматизации разработки": "ml",
            "Разработка информационной системы управления": "software",
            "Мониторинг цен на ТП ML-РЕШЕНИЯ": "ml",
            "Развитие функционала системы Заказчика 1С:УХ": "software",
            "Сертификат технической поддержки СУБД Postgres Pro": "license",
            "Внедрение лицензируемого программного обеспечения для Big Data": "license",
        }
        for title, direction in expected.items():
            with self.subTest(title=title):
                self.assertEqual(directions.classify({"title": title}), direction)


if __name__ == "__main__":
    unittest.main()
