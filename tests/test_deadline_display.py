from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import storage


class DeadlineDisplayTests(unittest.TestCase):
    def test_saved_zero_day_reason_is_refreshed_to_hours_for_display(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = str(Path(tmp) / "test.db")
            original_connect = storage.connect

            def temp_connect(path=database):
                return original_connect(database)

            sys.modules.pop("app", None)
            with patch("storage.connect", side_effect=temp_connect):
                webapp = importlib.import_module("app")
                deadline = datetime.now() + timedelta(hours=6)
                annotated = webapp._annotate([{
                    "tender_id": "deadline-hours",
                    "title": "Поставка лицензий СУБД",
                    "subject": "Передача неисключительных прав",
                    "deadline": deadline.isoformat(),
                    "reasons": [
                        "До дедлайна 0 дн., но для лицензии короткий срок допустим"
                    ],
                    "labels": [],
                }])[0]
                reason = annotated["reasons"][0]
                self.assertRegex(
                    reason,
                    r"^До дедлайна [56] ч\., но для лицензии короткий срок допустим$",
                )
                self.assertNotIn("0 дн.", reason)


if __name__ == "__main__":
    unittest.main()
