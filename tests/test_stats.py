"""Tests for work-interval totals, stats aggregation and the work-metrics backfill."""

import sqlite3
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from classify import classify, work_totals
from formatting import format_duration, format_pace_seconds
from stats import aggregate_stats, chart_geometry
from sync import _backfill_work_metrics, _sync

SCHEMA = (Path(__file__).resolve().parents[1] / "schema.sql").read_text()

# Warm-up and cool-down are single-rep groups; 3 x 10 min work set with 2 recoveries.
INTERVALS = [
    {"id": 0, "type": "WORK", "group_id": "warmup", "moving_time": 600, "distance": 1200},
    {"id": 1, "type": "WORK", "group_id": "main", "moving_time": 599, "distance": 2130},
    {"id": 2, "type": "RECOVERY", "group_id": "rest", "moving_time": 60, "distance": 100},
    {"id": 3, "type": "WORK", "group_id": "main", "moving_time": 601, "distance": 2140},
    {"id": 4, "type": "RECOVERY", "group_id": "rest", "moving_time": 60, "distance": 100},
    {"id": 5, "type": "WORK", "group_id": "main", "moving_time": 600, "distance": 2140},
    {"id": 6, "type": "WORK", "group_id": "cooldown", "moving_time": 600, "distance": 1200},
]
GROUPS = [
    {"id": "warmup", "count": 1, "moving_time": 600},
    {"id": "main", "count": 3, "moving_time": 600},
    {"id": "rest", "count": 2, "moving_time": 60},
    {"id": "cooldown", "count": 1, "moving_time": 600},
]


def session(day, seconds=1800, metres=6410, classification="interval"):
    return {
        "id": day, "start_date_local": day + "T10:00:00", "classification": classification,
        "work_interval_time": seconds, "work_interval_distance": metres,
    }


class WorkTotalsTests(unittest.TestCase):
    def test_sums_only_repeated_work_intervals(self):
        seconds, metres = work_totals(INTERVALS, GROUPS)
        self.assertEqual((seconds, metres), (1800, 6410))
        self.assertEqual(format_duration(seconds), "30:00")
        self.assertEqual(format_pace_seconds(seconds / (metres / 1000)), "4:41")

    def test_no_work_raises(self):
        with self.assertRaises(ValueError):
            work_totals([], GROUPS)

    def test_classification_carries_work_totals(self):
        result = classify({"moving_time": 4200}, INTERVALS, GROUPS)
        self.assertEqual(result["display_name"], "SubT-3x10")
        self.assertEqual(result["work_interval_time"], 1800)
        self.assertEqual(result["work_interval_distance"], 6410)

    def test_other_classifications_unchanged(self):
        self.assertEqual(classify({"sub_type": "RACE"}, INTERVALS, GROUPS)["classification"], "race")
        self.assertEqual(classify({"moving_time": 3600}, [], [])["classification"], "long")
        easy = classify({"moving_time": 3000}, [], [])
        self.assertEqual(easy["classification"], "easy")
        self.assertEqual(easy["work_interval_distance"], 0)


class AggregateTests(unittest.TestCase):
    today = date(2026, 11, 1)  # Sunday of ISO week 44

    def test_pace_is_weighted_not_averaged(self):
        rows = [session("2026-10-26", 1800, 6000), session("2026-10-27", 600, 3000)]
        week = aggregate_stats(rows, self.today)[-1]
        self.assertEqual((week["work_time"], week["work_distance"]), (2400, 9))
        self.assertAlmostEqual(week["pace"], 2400 / 9)  # 266.7, not (300 + 200) / 2
        self.assertTrue(week["ongoing"])

    def test_other_classifications_are_ignored(self):
        rows = [session("2026-10-26"), session("2026-10-27", classification="race")]
        self.assertEqual(len(aggregate_stats(rows, self.today)[-1]["sessions"]), 1)

    def test_empty_weeks_and_year_boundary(self):
        periods = aggregate_stats([session("2025-12-29")], date(2026, 1, 11))
        self.assertEqual(len(periods), 12)
        self.assertEqual(periods[-2]["label"], "2026-W01")
        self.assertEqual(periods[-1]["work_distance"], 0)
        self.assertIsNone(periods[-1]["pace"])

    def test_monthly_recomputes_from_totals(self):
        rows = [session("2026-10-01", 1800, 6000), session("2026-10-20", 600, 3000)]
        october = next(p for p in aggregate_stats(rows, self.today, grouping="monthly") if p["label"] == "2026-10")
        self.assertAlmostEqual(october["pace"], 2400 / 9)

    def test_range_all_and_imperial(self):
        periods = aggregate_stats([session("2025-01-01")], self.today, None, divisor=1609.344)
        self.assertEqual(periods[0]["label"], "2025-W01")
        self.assertAlmostEqual(periods[0]["pace"], 1800 * 1609.344 / 6410)

    def test_missing_work_data_leaves_gap(self):
        rows = [session("2026-10-19"), session("2026-10-26", None, None)]
        periods = aggregate_stats(rows, self.today)
        self.assertEqual(periods[-1]["missing"], 1)
        self.assertIsNone(periods[-1]["pace"])
        chart = chart_geometry(periods, "pace")
        self.assertNotIn("2026-W44", [p["label"] for p in chart["points"]])

    def test_faster_pace_is_higher_on_chart(self):
        rows = [session("2026-10-12", 1200, 5000), session("2026-10-26", 1800, 5000)]
        fast, slow = chart_geometry(aggregate_stats(rows, self.today), "pace")["points"]
        self.assertLess(fast["y"], slow["y"])


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(SCHEMA)
        patcher = patch("sync.time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.db.close)

    def add(self, activity_id, day, distance=None, seconds=1800):
        self.db.execute(
            """INSERT INTO activities (id, start_date_local, type, moving_time, classification,
               display_name, work_interval_time, work_interval_distance, synced_at)
               VALUES (?, ?, 'Run', 4200, 'interval', 'SubT-3x10', ?, ?, 'x')""",
            (activity_id, day + "T10:00:00", seconds, distance),
        )

    def row(self, activity_id):
        return self.db.execute(
            "SELECT work_interval_time, work_interval_distance FROM activities WHERE id = ?",
            (activity_id,),
        ).fetchone()

    def test_fills_old_rows_and_skips_complete_ones(self):
        self.add("old", "2025-01-01")
        self.add("done", "2026-10-26", distance=5000)
        with patch("sync._fetch_intervals", return_value=(INTERVALS, GROUPS)) as fetch:
            self.assertEqual(_backfill_work_metrics(self.db, Mock()), [])
        fetch.assert_called_once()
        self.assertEqual(self.row("old"), (1800, 6410))
        self.assertEqual(self.row("done"), (1800, 5000))

    def test_edited_session_is_reclassified(self):
        self.add("old", "2025-01-01")
        with patch("sync._fetch_intervals", return_value=([], [])):
            self.assertEqual(_backfill_work_metrics(self.db, Mock()), [])
        self.assertEqual(
            self.db.execute("SELECT classification, work_interval_distance FROM activities").fetchone(),
            ("long", 0),
        )

    def test_failure_stays_pending_and_is_retried(self):
        self.add("old", "2025-01-01")
        with patch("sync._fetch_intervals", side_effect=requests.HTTPError("down")):
            self.assertEqual(len(_backfill_work_metrics(self.db, Mock())), 1)
        self.assertIsNone(self.row("old")[1])
        with patch("sync._fetch_intervals", return_value=(INTERVALS, GROUPS)):
            _backfill_work_metrics(self.db, Mock())
        self.assertEqual(self.row("old")[1], 6410)

    def test_failed_fetch_keeps_existing_row_and_skips_new_one(self):
        self.add("known", "2026-10-26", distance=5000)
        self.db.execute("INSERT INTO sync_state VALUES ('last_sync_date', '2026-11-01')")
        fetched = [
            {"id": "known", "start_date_local": "2026-10-26T10:00:00", "type": "Run", "moving_time": 4200},
            {"id": "new", "start_date_local": "2026-10-31T10:00:00", "type": "Run", "moving_time": 3000},
        ]
        config = {"athlete_id": "0", "activity_types": ["Run"]}
        with patch("sync.refresh_prefs"), patch("sync._fetch_activities", return_value=fetched), \
                patch("sync._fetch_intervals", side_effect=requests.HTTPError("down")):
            result = _sync(self.db, Mock(), config, False)
        self.assertEqual(len(result["errors"]), 2)
        self.assertEqual(self.row("known"), (1800, 5000))
        self.assertIsNone(self.row("new"))


if __name__ == "__main__":
    unittest.main()
