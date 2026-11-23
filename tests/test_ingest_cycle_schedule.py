"""Cycle scheduling alignment with the Devvit ingest cron.

The bc-bulletin app posts an ingest payload at UTC 00/04/08/12/16/20
(devvit.json scheduler cron "0 */4 * * *"). The bot should run its cycle a few
minutes after each slot so posts go out shortly after a fresh payload lands.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from bc_bot.discord_bot import seconds_until_next_ingest_cycle


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


class IngestCycleScheduleTest(unittest.TestCase):
    def test_before_slot_plus_offset_same_slot(self) -> None:
        # 12:03:30Z -> the 12:05Z target is 90s away
        self.assertAlmostEqual(
            seconds_until_next_ingest_cycle(_utc("2026-11-23T12:03:30Z"), 5), 90.0
        )

    def test_after_target_rolls_to_next_4h_slot(self) -> None:
        # 12:07Z -> next target is 16:05Z (3h58m)
        self.assertAlmostEqual(
            seconds_until_next_ingest_cycle(_utc("2026-11-23T12:07:00Z"), 5),
            4 * 3600 - 120,
        )

    def test_exactly_on_target_rolls_to_next_slot(self) -> None:
        self.assertAlmostEqual(
            seconds_until_next_ingest_cycle(_utc("2026-11-23T12:05:00Z"), 5),
            4 * 3600,
        )

    def test_custom_offset(self) -> None:
        # 11:58Z with offset 7 -> 12:07Z, 9 minutes away
        self.assertAlmostEqual(
            seconds_until_next_ingest_cycle(_utc("2026-11-23T11:58:00Z"), 7), 9 * 60
        )

    def test_wraps_past_midnight(self) -> None:
        # 23:59Z -> next target is 00:05Z the next day, 6 minutes away
        self.assertAlmostEqual(
            seconds_until_next_ingest_cycle(_utc("2026-11-23T23:59:00Z"), 5), 6 * 60
        )

    def test_early_morning_slot(self) -> None:
        # 01:00Z -> 04:05Z is 3h05m away
        self.assertAlmostEqual(
            seconds_until_next_ingest_cycle(_utc("2026-11-23T01:00:00Z"), 5),
            3 * 3600 + 300,
        )


if __name__ == "__main__":
    unittest.main()
