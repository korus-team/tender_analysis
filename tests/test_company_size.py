from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import company_size
import storage


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    @property
    def headers(self):
        class Headers(dict):
            pass
        if not hasattr(self, "_headers"):
            self._headers = Headers()
        return self._headers

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class CompanyRevenueTests(unittest.TestCase):
    def test_inn_checksum_validation(self):
        self.assertEqual(company_size.normalize_inn("7707083893"), "7707083893")
        self.assertEqual(company_size.normalize_inn("7707083894"), "")
        self.assertEqual(company_size.normalize_inn("123"), "")

    def test_fns_gir_bo_converts_thousands_and_uses_exact_inn(self):
        payload = {
            "content": [{
                "id": 6765454,
                "inn": "<strong>2310031475</strong>",
                "shortName": "АО ТАНДЕР",
                "bfo": {"period": "2025", "gainSum": 3_050_026_081},
            }]
        }
        session = FakeSession(FakeResponse(payload))
        client = company_size.FnsGirBoRevenueClient(session=session)
        result = client.lookup("2310031475")

        self.assertTrue(result.passes)
        self.assertEqual(result.revenue_rub, 3_050_026_081_000)
        self.assertEqual(result.report_year, 2025)
        sent = session.calls[0][1]
        self.assertEqual(sent["params"]["query"], "2310031475")
        self.assertEqual(sent["params"]["page"], 0)

    def test_cache_prevents_duplicate_api_request(self):
        class Client:
            def __init__(self):
                self.calls = 0

            def lookup(self, inn, company_name=None):
                self.calls += 1
                return company_size.RevenueCheck(
                    inn, 20_000_000_000, 2025, company_name,
                    "fns-gir-bo", "found",
                )

            def lookup_by_name(self, company_name=None):
                raise AssertionError("поиск по названию не должен вызываться")

        with tempfile.TemporaryDirectory() as tmp:
            conn = storage.connect(str(Path(tmp) / "test.db"))
            client = Client()
            verified_path = Path(tmp) / "verified.csv"
            first = company_size.check_revenue_by_inn(
                conn, "7707083893", "ПАО Сбербанк", client=client,
                verified_registry_path=verified_path,
            )
            second = company_size.check_revenue_by_inn(
                conn, "7707083893", "ПАО Сбербанк", client=client,
                verified_registry_path=verified_path,
            )
            self.assertFalse(first.from_cache)
            self.assertTrue(second.from_cache)
            self.assertEqual(client.calls, 1)
            self.assertIn("7707083893", verified_path.read_text(encoding="utf-8"))
            conn.close()

    def test_exact_local_name_registry_is_last_fallback(self):
        session = FakeSession(FakeResponse({"content": []}))
        client = company_size.FnsGirBoRevenueClient(session=session)
        with tempfile.TemporaryDirectory() as tmp:
            conn = storage.connect(str(Path(tmp) / "test.db"))
            registry = Path(tmp) / "names.txt"
            registry.write_text(
                "00001. [Тест] ООО «Тестовая крупная компания» | "
                "Головная структура: Тест | Регион/Охват: РФ\n",
                encoding="utf-8",
            )
            result = company_size.check_revenue_by_inn(
                conn, "7707083893", "ООО «Тестовая крупная компания»",
                client=client, name_registry_path=registry,
                verified_registry_path=Path(tmp) / "verified.csv",
            )
            self.assertTrue(result.passes)
            self.assertEqual(result.source, "local-name-registry")
            self.assertEqual(result.status, "registry_found")
            self.assertEqual(len(session.calls), 2)
            conn.close()

    def test_name_lookup_accepts_only_close_company_name(self):
        payload = {"content": [{
            "inn": "<strong>2310031475</strong>",
            "shortName": "АО ТАНДЕР",
            "bfo": {"period": "2025", "gainSum": 3_050_026_081},
        }]}
        client = company_size.FnsGirBoRevenueClient(
            session=FakeSession(FakeResponse(payload))
        )
        found = client.lookup_by_name("АО Тандер")
        mismatch = client.lookup_by_name("ООО Совсем другая компания")
        self.assertTrue(found.passes)
        self.assertEqual(found.source, "fns-gir-bo-name")
        self.assertFalse(mismatch.is_confirmed)

    def test_name_lookup_cannot_confirm_a_different_inn(self):
        class Client:
            def lookup(self, inn, company_name=None):
                return company_size.RevenueCheck(
                    inn, None, None, company_name, "fns-gir-bo", "not_found",
                )

            def lookup_by_name(self, company_name=None):
                return company_size.RevenueCheck(
                    "2310031475", 3_050_026_081_000, 2025, "АО ТАНДЕР",
                    "fns-gir-bo-name", "found",
                )

        with tempfile.TemporaryDirectory() as tmp:
            conn = storage.connect(str(Path(tmp) / "test.db"))
            verified_path = Path(tmp) / "verified.csv"
            result = company_size.check_revenue_by_inn(
                conn, "7707083893", "АО ТАНДЕР", client=Client(),
                name_registry_path=Path(tmp) / "names.txt",
                verified_registry_path=verified_path,
            )

            self.assertFalse(result.passes)
            self.assertFalse(result.is_confirmed)
            self.assertEqual(result.inn, "7707083893")
            self.assertEqual(result.status, "inn_mismatch")
            self.assertIsNone(result.revenue_rub)
            self.assertFalse(verified_path.exists())
            conn.close()


if __name__ == "__main__":
    unittest.main()
