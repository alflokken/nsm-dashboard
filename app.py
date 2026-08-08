"""NSM - Norwegian Singles Method running tracker."""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, jsonify, request

from sync import sync_activities

app = Flask(__name__)

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
SCHEMA_PATH = BASE_DIR / "schema.sql"


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


def get_db():
    """Get a database connection, creating the DB if needed."""
    config = load_config()
    db_path = BASE_DIR / config.get("db_path", "nsm.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    # Apply schema on every connect (IF NOT EXISTS makes this safe)
    with open(SCHEMA_PATH) as f:
        db.executescript(f.read())
    return db


def get_weeks_data(db, num_weeks=20):
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
            "distance_km": round(total_distance / 1000, 1),
            "duration_h": total_moving_time // 3600,
            "duration_m": (total_moving_time % 3600) // 60,
            "pct_easy": pct_easy,
            "pct_threshold": pct_threshold,
            "activity_count": len(all_activities),
        }

    return sorted_weeks, total_weeks


def format_pace(average_speed):
    """Convert m/s to min:sec/km string."""
    if not average_speed or average_speed <= 0:
        return "-"
    secs_per_km = 1000 / average_speed
    mins = int(secs_per_km // 60)
    secs = int(secs_per_km % 60)
    return f"{mins}:{secs:02d}"


def current_iso_week():
    """Return (year, week) for today."""
    iso = datetime.now().isocalendar()
    return (iso[0], iso[1])


@app.route("/")
def index():
    """Main page: weekly training grid."""
    num_weeks = request.args.get("weeks", 20, type=int)
    num_weeks = max(1, min(num_weeks, 500))

    db = get_db()
    weeks, total_weeks = get_weeks_data(db, num_weeks)

    # Get last sync timestamp
    row = db.execute("SELECT value FROM sync_state WHERE key = 'last_sync_time'").fetchone()
    last_sync = None
    if row:
        try:
            dt = datetime.fromisoformat(row[0])
            last_sync = dt.strftime("%d %b %H:%M")
        except (ValueError, TypeError):
            last_sync = row[0]

    db.close()
    return render_template(
        "index.html",
        weeks=weeks,
        current_week=current_iso_week(),
        today_weekday=datetime.now().isocalendar()[2] - 1,
        format_pace=format_pace,
        last_sync=last_sync,
        shown_weeks=num_weeks,
        total_weeks=total_weeks,
        has_more=num_weeks < total_weeks,
    )


@app.route("/api/refresh", methods=["POST"])
def refresh():
    """Trigger a sync with Intervals.icu."""
    config = load_config()
    db = get_db()
    result = sync_activities(db, config)
    db.close()
    return jsonify(result)


if __name__ == "__main__":
    config = load_config()
    port = config.get("port", 5000)

    # Configure logging
    level = logging.DEBUG if config.get("debug", False) else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Auto-sync on first run if DB is empty
    db = get_db()
    count = db.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    if count == 0:
        print("No activities found. Running initial sync...")
        result = sync_activities(db, config)
        if result["success"]:
            print(f"Initial sync complete: {result['synced']} activities synced.")
        else:
            print(f"Sync failed: {result.get('error', 'Unknown error')}")
        if result.get("errors"):
            print(f"  Warnings: {len(result['errors'])} interval fetch errors")
    db.close()

    print(f"Starting NSM on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
