"""[AI-GENERATED] Isolated work metrics, backfill, aggregation, and route tests."""

import builtins
import importlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from classify import classify, work_metrics
from database import initialize_database
from stats import aggregate_stats, chart_geometry, format_duration, format_work_pace
from sync import (
    _backfill_work_metrics, _fetch_intervals, _store_activity, _sync,
    _sync_activity, backfill_remaining,
)

ROOT = Path(__file__).resolve().parents[1]


def sample_intervals():
    return [
        {"id": 0, "type": "WORK", "group_id": "warmup", "moving_time": 600, "distance": 1200},
        {"id": 1, "type": "WORK", "group_id": "main", "moving_time": 599, "distance": 2130},
        {"id": 2, "type": "RECOVERY", "group_id": "rest", "moving_time": 60, "distance": 100},
        {"id": 3, "type": "WORK", "group_id": "main", "moving_time": 601, "distance": 2140},
        {"id": 4, "type": "RECOVERY", "group_id": "rest", "moving_time": 60, "distance": 100},
        {"id": 5, "type": "WORK", "group_id": "main", "moving_time": 600, "distance": 2140},
        {"id": 6, "type": "WORK", "group_id": "cooldown", "moving_time": 600, "distance": 1200},
    ]


def sample_groups():
    return [
        {"id": "warmup", "count": 1, "moving_time": 600},
        {"id": "main", "count": 3, "moving_time": 600},
        {"id": "rest", "count": 2, "moving_time": 60},
        {"id": "cooldown", "count": 1, "moving_time": 600},
    ]


def activity_record(activity_id="a1", day="2026-10-26", status="complete", seconds=1800, metres=6410):
    return {
        "id": activity_id, "start_date_local": day + "T10:00:00",
        "type": "Run", "moving_time": 4200, "distance": 9100,
        "average_speed": 2.5, "classification": "interval",
        "display_name": "SubT-3x10", "work_interval_time": seconds,
        "interval_count": 3, "interval_avg_duration": 600,
        "work_interval_distance": metres, "work_metrics_status": status,
        "work_metrics_error": None,
    }


def load_test_app(db_path):
    """Import dashboard with a fake config, without touching its live database."""
    real_open = builtins.open
    config = {
        "api_key": "test-only", "athlete_id": "0",
        "activity_types": ["Run"], "db_path": str(db_path),
        "sync_stale_minutes": 60,
    }

    def fake_open(path, *args, **kwargs):
        if Path(path) == ROOT / "config.json":
            return io.StringIO(json.dumps(config))
        return real_open(path, *args, **kwargs)

    with patch("builtins.open", side_effect=fake_open):
        module = importlib.import_module("app")
    module._DB_PATH = db_path
    module.CONFIG = config
    return module


class WorkMetricsTests(unittest.TestCase):
    def test_docker_image_includes_local_python_modules(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        copied = set()
        for line in dockerfile.splitlines():
            if line.startswith("COPY ") and line.endswith(" ./"):
                copied.update(line.split()[1:-1])
        for module in ROOT.glob("*.py"):
            with self.subTest(module=module.name):
                self.assertIn(module.name, copied)

    def test_validated_example_and_rounding(self):
        result = work_metrics("interval", sample_intervals(), sample_groups())
        self.assertEqual(result["work_interval_time"], 1800)
        self.assertEqual(result["work_interval_distance"], 6410)
        self.assertEqual(format_duration(result["work_interval_time"]), "30:00")
        self.assertEqual(f'{result["work_interval_distance"] / 1000:.2f}', "6.41")
        self.assertEqual(format_work_pace(1800 / 6.41), "4:41")
        self.assertEqual(format_work_pace(299.9), "5:00")

    def test_multiple_sets_and_duplicate_exclusion(self):
        intervals = sample_intervals() + [
            {"id": 7, "type": "WORK", "group_id": "short", "moving_time": 180, "distance": 700},
            {"id": 8, "type": "WORK", "group_id": "short", "moving_time": 181, "distance": 701},
            sample_intervals()[1],
        ]
        groups = sample_groups() + [{"id": "short", "count": 2, "moving_time": 180}]
        self.assertEqual(
            work_metrics("interval", intervals, groups),
            {"work_interval_time": 2161, "work_interval_distance": 7811},
        )

    def test_invalid_data_is_not_zero(self):
        for bad in (None, -1, float("nan"), float("inf"), True, "123"):
            intervals = sample_intervals()
            intervals[1]["distance"] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                work_metrics("interval", intervals, sample_groups())
        with self.assertRaises(ValueError):
            work_metrics("interval", [], sample_groups())

    def test_classification_preserved(self):
        self.assertEqual(classify({"sub_type": "RACE"}, sample_intervals(), sample_groups())["classification"], "race")
        self.assertEqual(classify({"moving_time": 4200}, sample_intervals(), sample_groups())["display_name"], "SubT-3x10")
        self.assertEqual(classify({"moving_time": 3600}, [], [])["classification"], "long")
        self.assertEqual(classify({"moving_time": 3000}, [], [])["classification"], "easy")
        for classification in ("race", "long", "easy"):
            self.assertEqual(work_metrics(classification, [], [])["work_interval_time"], 0)


class AggregationTests(unittest.TestCase):
    def test_weighted_pace_and_race_exclusion(self):
        rows = [
            activity_record("a", seconds=1800, metres=6000),
            activity_record("b", seconds=600, metres=3000),
            {**activity_record("race"), "classification": "race"},
        ]
        period = aggregate_stats(rows, date(2026, 11, 1))["periods"][-1]
        self.assertEqual(period["session_count"], 2)
        self.assertEqual(period["work_time"], 2400)
        self.assertEqual(period["work_distance"], 9)
        self.assertAlmostEqual(period["pace"], 2400 / 9)
        self.assertNotEqual(period["pace"], (300 + 200) / 2)

    def test_empty_periods_and_year_boundary(self):
        data = aggregate_stats([activity_record(day="2025-12-29")], date(2026, 1, 11))
        self.assertEqual(len(data["periods"]), 12)
        self.assertEqual(data["periods"][-2]["label"], "2026-W01")
        self.assertEqual(data["periods"][-1]["work_distance"], 0)
        self.assertIsNone(data["periods"][-1]["pace"])
        self.assertTrue(data["periods"][-1]["ongoing"])

    def test_months_recompute_from_sessions(self):
        rows = [
            activity_record("a", day="2026-10-01", seconds=1800, metres=6000),
            activity_record("b", day="2026-10-20", seconds=600, metres=3000),
        ]
        data = aggregate_stats(rows, date(2026, 11, 1), "12", "monthly")
        october = next(p for p in data["periods"] if p["label"] == "2026-10")
        self.assertAlmostEqual(october["pace"], 2400 / 9)
        self.assertFalse(october["partial"])
        self.assertTrue(data["periods"][0]["partial"])
        self.assertTrue(data["periods"][-1]["ongoing"])
        self.assertTrue(data["periods"][-1]["partial"])

    def test_missing_data_creates_chart_gap(self):
        rows = [
            activity_record(day="2026-10-19"),
            activity_record("missing", day="2026-10-26", status="failed", metres=None),
        ]
        periods = aggregate_stats(rows, date(2026, 11, 1))["periods"]
        self.assertFalse(periods[-1]["complete"])
        self.assertEqual(periods[-1]["missing_count"], 1)
        self.assertIsNone(periods[-1]["pace"])
        self.assertNotIn("2026-W44", [p["label"] for p in chart_geometry(periods, "work_distance")["points"]])

    def test_unknown_activity_marks_period_incomplete(self):
        row = {**activity_record(), "classification": "unknown", "work_metrics_status": "failed"}
        period = aggregate_stats([row], date(2026, 11, 1))["periods"][-1]
        self.assertEqual(period["session_count"], 0)
        self.assertFalse(period["complete"])

    def test_all_history_and_imperial(self):
        data = aggregate_stats([activity_record(day="2025-01-01")], date(2026, 11, 1), "all", divisor=1609.344)
        period = next(p for p in data["periods"] if p["session_count"])
        self.assertEqual(data["start"], date(2024, 12, 30))
        self.assertAlmostEqual(period["work_distance"], 6410 / 1609.344)
        self.assertAlmostEqual(period["pace"], 1800 * 1609.344 / 6410)

    def test_chart_direction_alignment_and_no_lines_across_gaps(self):
        rows = [
            activity_record("fast", day="2026-10-12", seconds=1200, metres=5000),
            activity_record("slow", day="2026-10-26", seconds=1800, metres=5000),
        ]
        periods = aggregate_stats(rows, date(2026, 11, 1))["periods"]
        pace = chart_geometry(periods, "pace")
        distance = chart_geometry(periods, "work_distance")
        self.assertLess(pace["points"][0]["y"], pace["points"][1]["y"])
        self.assertEqual(pace["segments"], [])
        self.assertEqual(pace["labels"], distance["labels"])

    def test_date_window_excludes_old_and_future_rows(self):
        rows = [
            activity_record("old", day="2026-01-01"),
            activity_record("future", day="2026-11-02"),
            activity_record("valid"),
        ]
        periods = aggregate_stats(rows, date(2026, 11, 1))["periods"]
        self.assertEqual(sum(p["session_count"] for p in periods), 1)


class DatabaseAndSyncTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        initialize_database(self.db, ROOT / "schema.sql")
        self.sleep = patch("sync.time.sleep")
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.db.close()

    def add(self, record):
        _store_activity(self.db, record, record)
        self.db.commit()

    def test_legacy_migration_idempotent(self):
        self.db.close()
        self.db = sqlite3.connect(":memory:")
        self.db.executescript("""
            CREATE TABLE activities (
                id TEXT PRIMARY KEY, classification TEXT, work_interval_time INTEGER,
                start_date_local TEXT);
            INSERT INTO activities VALUES ('interval', 'interval', 1800, '2026-01-01');
            INSERT INTO activities VALUES ('easy', 'easy', 0, '2026-01-01');
        """)
        initialize_database(self.db, ROOT / "schema.sql")
        initialize_database(self.db, ROOT / "schema.sql")
        self.assertEqual(
            self.db.execute("SELECT work_interval_time, work_interval_distance, work_metrics_status FROM activities WHERE id = 'interval'").fetchone(),
            (1800, None, "pending"),
        )
        self.assertEqual(backfill_remaining(self.db), 1)
        self.assertEqual(self.db.execute("SELECT work_metrics_status FROM activities WHERE id = 'easy'").fetchone()[0], "complete")

    def test_successful_sync_uses_actual_work(self):
        with patch("sync._fetch_intervals", return_value=(sample_intervals(), sample_groups())):
            self.assertTrue(_sync_activity(self.db, Mock(), activity_record(), []))
        self.assertEqual(
            self.db.execute("SELECT work_interval_time, work_interval_distance, work_metrics_status FROM activities").fetchone(),
            (1800, 6410, "complete"),
        )

    def test_failed_fetch_preserves_prior_data(self):
        self.add(activity_record())
        errors = []
        with patch("sync._fetch_intervals", side_effect=requests.HTTPError("unavailable")):
            self.assertFalse(_sync_activity(self.db, Mock(), activity_record(), errors))
        self.assertEqual(
            self.db.execute("SELECT classification, work_interval_time, work_interval_distance, work_metrics_status FROM activities").fetchone(),
            ("interval", 1800, 6410, "failed"),
        )
        self.assertEqual(len(errors), 1)

    def test_failed_new_activity_is_unknown_not_easy(self):
        with patch("sync._fetch_intervals", side_effect=requests.HTTPError("unavailable")):
            _sync_activity(self.db, Mock(), activity_record(), [])
        self.assertEqual(
            self.db.execute("SELECT classification, work_interval_distance FROM activities").fetchone(),
            ("unknown", None),
        )

    def test_malformed_payload_and_retry(self):
        session = Mock()
        session.get.return_value.json.return_value = {}
        session.get.return_value.status_code = 200
        with self.assertRaises(ValueError):
            _fetch_intervals(session, "a")
        session.get.return_value.json.return_value = []
        with self.assertRaises(ValueError):
            _fetch_intervals(session, "a")
        throttled = Mock(status_code=429)
        good = Mock(status_code=200)
        good.json.return_value = {"icu_intervals": sample_intervals(), "icu_groups": sample_groups()}
        session.get.side_effect = [throttled, good]
        self.assertEqual(_fetch_intervals(session, "a")[0], sample_intervals())

    def test_backfill_resumes_retries_and_skips_complete(self):
        self.add(activity_record("old", day="2025-01-01", status="pending", metres=None))
        self.add(activity_record("ready"))
        errors = []
        with patch("sync._fetch_intervals", side_effect=requests.HTTPError("retry")):
            result = _backfill_work_metrics(self.db, Mock(), set(), errors)
        self.assertEqual(result["remaining"], 1)
        self.assertIsNone(self.db.execute("SELECT value FROM sync_state WHERE key = 'work_metrics_backfill_completed'").fetchone())
        with patch("sync._fetch_intervals", return_value=(sample_intervals(), sample_groups())) as fetch:
            result = _backfill_work_metrics(self.db, Mock(), set(), [])
            fetch.assert_called_once()
            self.assertEqual(fetch.call_args.args[1], "old")
        self.assertEqual(result, {"completed": 1, "remaining": 0})
        with patch("sync._fetch_intervals") as fetch:
            _backfill_work_metrics(self.db, Mock(), set(), [])
            fetch.assert_not_called()

    def test_interrupted_backfill_commits_bounded_progress(self):
        for index in range(26):
            self.add(activity_record(str(index), day="2025-01-01", status="pending", metres=None))
        responses = [(sample_intervals(), sample_groups())] * 25 + [RuntimeError("interrupted")]
        with patch("sync._fetch_intervals", side_effect=responses):
            with self.assertRaises(RuntimeError):
                _backfill_work_metrics(self.db, Mock(), set(), [])
        self.db.rollback()
        self.assertEqual(backfill_remaining(self.db), 1)
        with patch("sync._fetch_intervals", return_value=(sample_intervals(), sample_groups())):
            self.assertEqual(_backfill_work_metrics(self.db, Mock(), set(), [])["remaining"], 0)

    def test_incremental_sync_backfills_older_history(self):
        self.add(activity_record("old", day="2025-01-01", status="pending", metres=None))
        self.db.execute("INSERT INTO sync_state VALUES ('last_sync_date', '2026-11-01')")
        config = {"athlete_id": "0", "activity_types": ["Run"]}
        with patch("sync.refresh_prefs"), patch("sync._fetch_activities", return_value=[activity_record("recent", day="2026-11-01")]), patch("sync._fetch_intervals", return_value=(sample_intervals(), sample_groups())) as fetch:
            result = _sync(self.db, Mock(), config, False)
        self.assertEqual(result["backfill"]["remaining"], 0)
        self.assertEqual({call.args[1] for call in fetch.call_args_list}, {"old", "recent"})


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp.name) / "test.db"
        cls.module = load_test_app(cls.db_path)
        cls.module.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.client = self.module.app.test_client()
        with closing(sqlite3.connect(str(self.db_path))) as db, db:
            db.execute("DELETE FROM activities")
            db.execute("DELETE FROM sync_state")
            _store_activity(db, activity_record(day=date.today().isoformat()), activity_record())
            db.execute("INSERT INTO sync_state VALUES ('last_sync_time', ?)", (datetime.now(timezone.utc).isoformat(),))

    def test_training_and_stats_render_without_api_calls(self):
        with patch("sync._session") as session:
            for url in ("/", "/stats", "/stats?weeks=all&grouping=monthly"):
                with self.subTest(url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(b"Training", response.data)
                    self.assertIn(b"Stats", response.data)
            session.assert_not_called()
        response = self.client.get("/stats")
        self.assertIn(b"6.41", response.data)
        self.assertIn(b"4:41", response.data)
        self.assertIn(b"30:00", response.data)
        self.assertIn(b"Average work pace", response.data)
        training = self.client.get("/")
        self.assertIn(b"9.1km", training.data)
        self.assertIn(b"6:40", training.data)

    def test_invalid_filters_are_explicit(self):
        self.assertEqual(self.client.get("/stats?weeks=garbage").status_code, 400)
        self.assertEqual(self.client.get("/stats?grouping=garbage").status_code, 400)

    def test_empty_and_incomplete_states(self):
        with closing(sqlite3.connect(str(self.db_path))) as db, db:
            db.execute("DELETE FROM activities")
        self.assertIn(b"No SubT sessions", self.client.get("/stats").data)
        with closing(sqlite3.connect(str(self.db_path))) as db, db:
            record = activity_record(day=date.today().isoformat(), status="failed", metres=None)
            record["work_metrics_error"] = "distance unavailable"
            _store_activity(db, record, record)
        response = self.client.get("/stats")
        self.assertIn(b"incomplete", response.data)
        self.assertIn(b"distance unavailable", response.data)
        self.assertIn(b"pending or failed", response.data)

    def test_imperial_units(self):
        with closing(sqlite3.connect(str(self.db_path))) as db, db:
            db.execute("INSERT INTO sync_state VALUES ('pref_measurement', 'feet')")
        response = self.client.get("/stats")
        self.assertIn(b"3.98", response.data)
        self.assertIn(b"7:32", response.data)
        self.assertIn(b"Pace /mi", response.data)

    def test_refresh_lock_and_shared_sync(self):
        self.module._sync_lock.acquire()
        try:
            self.assertEqual(self.client.post("/api/refresh").status_code, 409)
        finally:
            self.module._sync_lock.release()
        with patch.object(self.module, "sync_activities", return_value={"success": True, "errors": []}) as sync:
            self.assertEqual(self.client.post("/api/refresh").status_code, 200)
            sync.assert_called_once()

    def test_stale_and_backfill_trigger_do_not_loop_after_attempt(self):
        with closing(sqlite3.connect(str(self.db_path))) as db, db:
            db.execute("UPDATE activities SET work_metrics_status = 'pending'")
        self.assertIn(b"doRefresh();", self.client.get("/stats").data)
        with closing(sqlite3.connect(str(self.db_path))) as db, db:
            db.execute("INSERT INTO sync_state VALUES ('work_metrics_backfill_attempted', 'now')")
        self.assertNotIn(b"doRefresh();", self.client.get("/stats").data)
        with closing(sqlite3.connect(str(self.db_path))) as db, db:
            old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            db.execute("UPDATE sync_state SET value = ? WHERE key = 'last_sync_time'", (old,))
        self.assertIn(b"doRefresh();", self.client.get("/stats").data)


if __name__ == "__main__":
    unittest.main()
