from __future__ import annotations

import unittest

from services.document_requests import build_document_request


class DocumentRequestTests(unittest.TestCase):
    def test_uses_new_greeting_and_prefers_etp_link(self):
        text = build_document_request({
            "title": "Поставка лицензий",
            "url": "https://example.test/source",
            "details": {
                "kontur_url": "https://example.test/kontur",
                "etp_url": "https://example.test/etp",
            },
        })
        self.assertEqual(
            text,
            "Добрый день, коллеги! Просьба скачать документацию по закупке "
            "«Поставка лицензий» (https://example.test/etp) и переслать нам. Спасибо!",
        )
        self.assertNotIn("Здравствуйте", text)

    def test_works_without_link(self):
        self.assertEqual(
            build_document_request({"title": "Тестовая закупка"}),
            "Добрый день, коллеги! Просьба скачать документацию по закупке "
            "«Тестовая закупка» и переслать нам. Спасибо!",
        )


if __name__ == "__main__":
    unittest.main()
