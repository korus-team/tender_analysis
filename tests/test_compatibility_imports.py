from __future__ import annotations

import unittest

import kontur_excel
import priority_companies
from integrations import kontur_excel as kontur_integration
from services import priority_companies as priority_service


class CompatibilityImportTests(unittest.TestCase):
    def test_root_imports_still_point_to_moved_implementations(self):
        self.assertIs(kontur_excel.import_kontur_xlsx,
                      kontur_integration.import_kontur_xlsx)
        self.assertIs(priority_companies.import_xlsx, priority_service.import_xlsx)


if __name__ == "__main__":
    unittest.main()
