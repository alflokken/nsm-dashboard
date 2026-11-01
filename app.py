"""NSM - Norwegian Singles Method running tracker."""

import json
import logging
import sqlite3
import threading
import requests  # [AI-GENERATED] Explicit startup-sync exception handling.
from datetime import datetime, timedelta, timezone
from contextlib import closing  # [AI-GENERATED] Explicitly close background DB handles.
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, g, render_template, jsonify, request

from sync import sync_activities, refresh_prefs, backfill_remaining
from database import initialize_database  # [AI-GENERATED]
from stats import aggregate_stats, chart_geometry, format_duration, format_work_pace  # [AI-GENERATED]
from formatting import date_format_to_strftime, time_format_to_strftime, is_imperial_measurement

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
SCHEMA_PATH = BASE_DIR / "schema.sql"

log = logging.getLogger(__name__)


def load_config():
    """Load configuration from config.json."""
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        raise SystemExit(
            f"config.json not found at {CONFIG_PATH}. "
            "Copy config.example.json to config.json and add your API key and athlete ID."
        )
    except json.JSONDecodeError as e:
        raise SystemExit(f"config.json is not valid JSON: {e}")


# Load config and schema once at startup
CONFIG = load_config()
_DB_PATH = BASE_DIR / CONFIG.get("db_path", "nsm.db")
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)

# Serialize syncs so a visit-triggered auto-refresh never overlaps a manual one.
_sync_lock = threading.Lock()

# Apply schema once at startup
_init_db = sqlite3.connect(str(_DB_PATH))
initialize_database(_init_db, SCHEMA_PATH)  # [AI-GENERATED] Includes legacy migration.
_init_db.close()


def get_db():
    """Get a database connection for the current request (cached on g)."""
    if "db" not in g:
        g.db = sqlite3.connect(str(_DB_PATH))
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    """Close the DB connection at the end of each request."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


_KM_PER_MILE = 1.609344


def get_prefs(db):
    """Load athlete display preferences from sync_state."""
    rows = db.execute(
        "SELECT key, value FROM sync_state WHERE key LIKE 'pref_%'"
    ).fetchall()
    prefs = {row["key"]: row["value"] for row in rows}
    is_imperial = is_imperial_measurement(prefs.get("pref_measurement"))
    return {
        "distance_unit": "mi" if is_imperial else "km",
        "unit_divisor": _KM_PER_MILE * 1000 if is_imperial else 1000,
        "timezone": prefs.get("pref_timezone") or "Europe/Oslo",
        "datetime_format": (
            date_format_to_strftime(prefs.get("pref_date_format", ""))
            + " "
            + time_format_to_strftime(prefs.get("pref_time_format", ""))
        ),
    }


def _resolve_zone(prefs):
    """Resolve the athlete's timezone, falling back to UTC on a bad/unknown zone."""
    try:
        return ZoneInfo(prefs["timezone"])
    except ZoneInfoNotFoundError:
        log.warning("Unknown timezone %r, falling back to UTC", prefs.get("timezone"))
        return timezone.utc


def get_weeks_data(db, num_weeks=20, distance_divisor=1000):
    """Query activities and group them by ISO week for display."""
    rows = db.execute(
        "SELECT * FROM activities ORDER BY start_date_local DESC"
    ).fetchall()

    # Group activities by (year, week) with Monday as day 0
    weeks = {}
    for row in rows:
        dt = datetime.fromisoformat(row["start_date_local"][:19])
        # ISO calendar: (year, week, weekday) where Monday=1
        iso = dt.isocalendar()
        week_key = (iso[0], iso[1])
        if week_key not in weeks:
            weeks[week_key] = {"days": {d: [] for d in range(7)}, "key": week_key}
        day_index = iso[2] - 1  # Monday=0, Sunday=6
        weeks[week_key]["days"][day_index].append(dict(row))

    # Sort weeks descending (most recent first)
    sorted_weeks = sorted(weeks.values(), key=lambda w: w["key"], reverse=True)

    # Limit to num_weeks
    total_weeks = len(sorted_weeks)
    sorted_weeks = sorted_weeks[:num_weeks]

    # Compute totals for each week
    for week in sorted_weeks:
        all_activities = []
        for day_activities in week["days"].values():
            all_activities.extend(day_activities)

        total_distance = sum(a.get("distance") or 0 for a in all_activities)
        total_moving_time = sum(a.get("moving_time") or 0 for a in all_activities)
        total_work_time = sum(a.get("work_interval_time") or 0 for a in all_activities)
        total_easy_time = total_moving_time - total_work_time

        if total_moving_time > 0:
            pct_easy = round(total_easy_time / total_moving_time * 100)
            pct_threshold = round(total_work_time / total_moving_time * 100)
        else:
            pct_easy = 0
            pct_threshold = 0

        week["totals"] = {
            "distance": round(total_distance / distance_divisor, 1),
            "duration_h": total_moving_time // 3600,
            "duration_m": (total_moving_time % 3600) // 60,
            "pct_easy": pct_easy,
            "pct_threshold": pct_threshold,
            "activity_count": len(all_activities),
        }

    return sorted_weeks, total_weeks


def format_pace(average_speed, divisor=1000):
    """Convert m/s to min:sec per unit distance."""
    if not average_speed or average_speed <= 0:
        return "-"
    secs_per_unit = divisor / average_speed
    mins = int(secs_per_unit // 60)
    secs = int(secs_per_unit % 60)
    return f"{mins}:{secs:02d}"


@app.template_filter("pace")
def pace_filter(average_speed):
    """Jinja2 filter: {{ speed | pace }}."""
    prefs = g.get("prefs") or {}
    divisor = prefs.get("unit_divisor", 1000)
    return format_pace(average_speed, divisor)


def current_iso_week(tz):
    """Return (year, week) for today, in the given timezone."""
    iso = datetime.now(tz).isocalendar()
    return (iso[0], iso[1])


# [AI-GENERATED START] Shared sync context for the training and stats pages.
def sync_context(db, prefs, tz):
    # Get last sync timestamp, converted to the athlete's timezone
    row = db.execute("SELECT value FROM sync_state WHERE key = 'last_sync_time'").fetchone()
    last_sync = None
    last_sync_dt = None
    if row:
        try:
            last_sync_dt = datetime.fromisoformat(row[0])
            if last_sync_dt.tzinfo is None:
                last_sync_dt = last_sync_dt.replace(tzinfo=timezone.utc)  # legacy naive rows: assume UTC
            last_sync = last_sync_dt.astimezone(tz).strftime(prefs["datetime_format"])
        except (ValueError, TypeError):
            last_sync = row[0]

    # Stale = never synced, or older than the configured threshold. The page tells
    # the browser to kick off a background refresh when true (stale-while-revalidate).
    stale_minutes = CONFIG.get("sync_stale_minutes", 60)
    if last_sync_dt is None:
        stale = True
    else:
        stale = datetime.now(timezone.utc) - last_sync_dt > timedelta(minutes=stale_minutes)

    attempted = db.execute(
        "SELECT value FROM sync_state WHERE key = 'work_metrics_backfill_attempted'"
    ).fetchone()
    if backfill_remaining(db) and not attempted:
        stale = True
    return {
        "last_sync": last_sync, "stale": stale,
        "stale_minutes": stale_minutes, "syncing": _sync_lock.locked(),
    }
# [AI-GENERATED END]


@app.route("/")
def index():
    """Main page: weekly training grid."""
    num_weeks = request.args.get("weeks", 20, type=int)
    num_weeks = max(1, min(num_weeks, 500))
    db = get_db()
    prefs = get_prefs(db)
    g.prefs = prefs
    tz = _resolve_zone(prefs)
    weeks, total_weeks = get_weeks_data(db, num_weeks, prefs["unit_divisor"])

    return render_template(
        "index.html",
        weeks=weeks,
        current_week=current_iso_week(tz),
        today_weekday=datetime.now(tz).isocalendar()[2] - 1,
        format_pace=format_pace,
        **sync_context(db, prefs, tz),
        shown_weeks=num_weeks,
        total_weeks=total_weeks,
        has_more=num_weeks < total_weeks,
        prefs=prefs,
    )


# [AI-GENERATED START] SQLite-only stats route with shared weighted chart/table data.
@app.route("/stats")
def stats_page():
    weeks = request.args.get("weeks", "12")
    grouping = request.args.get("grouping", "weekly")
    if weeks not in ("12", "26", "52", "all") or grouping not in ("weekly", "monthly"):
        return "Invalid stats range or grouping", 400
    db = get_db()
    prefs = get_prefs(db)
    g.prefs = prefs
    tz = _resolve_zone(prefs)
    rows = [dict(row) for row in db.execute(
        "SELECT * FROM activities ORDER BY start_date_local, id"
    )]
    data = aggregate_stats(rows, datetime.now(tz).date(), weeks, grouping, prefs["unit_divisor"])
    remaining = backfill_remaining(db)
    complete = db.execute(
        """SELECT COUNT(*) FROM activities WHERE classification = 'interval'
           AND work_metrics_status = 'complete'"""
    ).fetchone()[0]
    return render_template(
        "stats.html", prefs=prefs, weeks=weeks, grouping=grouping,
        data=data, remaining=remaining, completed=complete,
        pace_chart=chart_geometry(data["periods"], "pace"),
        distance_chart=chart_geometry(data["periods"], "work_distance"),
        format_duration=format_duration, format_work_pace=format_work_pace,
        **sync_context(db, prefs, tz),
    )


def _startup_backfill():
    if not _sync_lock.acquire(blocking=False):
        return
    try:
        with closing(sqlite3.connect(str(_DB_PATH))) as db, db:
            result = sync_activities(db, CONFIG)
            if not result["success"]:
                log.error("Startup work metrics sync failed: %s", result.get("error"))
    except (sqlite3.Error, requests.RequestException, ValueError) as exc:
        log.exception("Startup work metrics sync failed: %s", exc)
    finally:
        _sync_lock.release()
# [AI-GENERATED END]

@app.route("/api/refresh", methods=["POST"])
def refresh():
    """Trigger a sync with Intervals.icu."""
    # Non-blocking: if a sync is already running, tell the caller instead of queuing.
    if not _sync_lock.acquire(blocking=False):
        return jsonify({"success": False, "busy": True}), 409
    try:
        db = get_db()
        result = sync_activities(db, CONFIG)
        return jsonify(result)
    except Exception as e:
        log.exception("Sync failed")
        return jsonify({"success": False, "error": str(e)}), 502
    finally:
        _sync_lock.release()


if __name__ == "__main__":
    port = CONFIG.get("port", 5000)

    # Configure logging once with correct level
    level = logging.DEBUG if CONFIG.get("debug", False) else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )

    db = sqlite3.connect(str(_DB_PATH))
    db.row_factory = sqlite3.Row

    print("Refreshing athlete preferences...")
    if not refresh_prefs(db, CONFIG):
        print("  Warning: could not refresh preferences (using last known values, if any).")

    # Auto-sync on first run if DB is empty
    count = db.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    if count == 0:
        print("No activities found. Running initial sync...")
        result = sync_activities(db, CONFIG)
        if result["success"]:
            print(f"Initial sync complete: {result['synced']} activities synced.")
        else:
            print(f"Sync failed: {result.get('error', 'Unknown error')}")
        if result.get("errors"):
            print(f"  Warnings: {len(result['errors'])} interval fetch errors")
    db.close()

    # [AI-GENERATED] Start historical backfill without delaying page rendering.
    with closing(sqlite3.connect(str(_DB_PATH))) as db:
        needs_backfill = backfill_remaining(db) > 0
    if needs_backfill:
        threading.Thread(target=_startup_backfill, daemon=True).start()

    print(f"Starting NSM on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
