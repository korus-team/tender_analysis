from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from icp_config import ICP
from scoring import score_tender


class ScoringDeadlineTests(unittest.TestCase):
    def test_license_deadline_under_one_day_is_explained_in_hours(self):
        now = datetime(2026, 8, 24, 10, 0)
        result = score_tender(
            {
                "title": "Поставка лицензий СУБД",
                "subject": "Передача неисключительных прав на использование СУБД",
                "days_left": 0,
                "deadline": (now + timedelta(hours=6)).isoformat(),
            },
            ICP,
            now=now,
        )
        deadline_reason = next(reason for reason in result.reasons if reason.startswith("До дедлайна"))
        self.assertEqual(
            deadline_reason,
            "До дедлайна 6 ч., но для лицензии короткий срок допустим",
        )
        self.assertNotIn("0 дн.", deadline_reason)


if __name__ == "__main__":
    unittest.main()
