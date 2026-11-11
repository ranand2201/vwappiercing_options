import unittest
from datetime import datetime

from BusinessLogic.vwappiercing_options.Logic.pattern_rules import (
    candle_close_time,
    has_closed_at_or_after,
)


class CandleCloseTimeTests(unittest.TestCase):
    def test_candle_close_time_accepts_timezone_suffix(self):
        self.assertEqual(
            candle_close_time("2026-11-09 11:10:00+05:30", 5),
            datetime(2026, 11, 9, 11, 15),
        )

    def test_has_closed_at_or_after_accepts_timezone_suffix(self):
        self.assertTrue(
            has_closed_at_or_after(
                "2026-11-09 11:10:00+05:30",
                5,
                "2026-11-09 11:05:00+05:30",
                5,
            )
        )

    def test_has_closed_at_or_after_accepts_mixed_timestamp_formats(self):
        self.assertTrue(
            has_closed_at_or_after(
                "2026-11-09 11:10:00+05:30",
                5,
                "2026-11-09 11:14:00",
                1,
            )
        )


if __name__ == "__main__":
    unittest.main()
