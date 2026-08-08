"""Intervals.icu sync engine for NSM."""

import logging
import time
import requests
from datetime import datetime, timedelta

from classify import classify

log = logging.getLogger(__name__)

API_BASE = "https://intervals.icu/api/v1"
FIELDS = "id,start_date_local,type,name,distance,moving_time,elapsed_time,average_heartrate,average_speed,icu_warmup_time,icu_cooldown_time"
USER_AGENT = "NSM/1.0"
_RETRY_STATUSES = {429, 500, 502, 503, 504}


def _session(api_key):
    """Create a requests session with auth and headers."""
    s = requests.Session()
    s.auth = ("API_KEY", api_key)
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def _fetch_activities(session, athlete_id, oldest, newest=None):
    """Fetch activities from Intervals.icu for a date range."""
    url = f"{API_BASE}/athlete/{athlete_id}/activities"
    params = {"oldest": oldest, "fields": FIELDS}
    if newest:
        params["newest"] = newest
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _fetch_intervals(session, activity_id, retries=1):
    """Fetch intervals and groups for a single activity (with retry)."""
    url = f"{API_BASE}/activity/{activity_id}/intervals"
    for attempt in range(retries + 1):
        resp = session.get(url, timeout=30)
        if resp.status_code in _RETRY_STATUSES and attempt < retries:
            time.sleep(1)
            continue
        resp.raise_for_status()
        data = resp.json()
        return data.get("icu_intervals", []), data.get("icu_groups", [])


def sync_activities(db, config: dict, full: bool = False) -> dict:
    """Sync activities from Intervals.icu.

    Args:
        db: sqlite3 connection
        config: dict with api_key, athlete_id, activity_types, weeks_initial_sync
        full: if True, force a full re-sync from scratch

    Returns:
        dict with sync results (count, errors, etc.)
    """
    session = _session(config["api_key"])
    try:
        return _sync(db, session, config, full)
    finally:
        session.close()


def _sync(db, session, config, full):
    """Internal sync implementation."""
    athlete_id = config["athlete_id"]
    activity_types = set(config["activity_types"])

    # Determine date range
    cursor = db.execute("SELECT value FROM sync_state WHERE key = 'last_sync_date'")
    row = cursor.fetchone()

    if row and not full:
        # Incremental sync: overlap by 2 days to catch edits
        last_sync = datetime.fromisoformat(row[0])
        oldest = (last_sync - timedelta(days=2)).strftime("%Y-%m-%d")
    else:
        # Initial sync
        weeks = config.get("weeks_initial_sync", 12)
        oldest = (datetime.now() - timedelta(weeks=weeks)).strftime("%Y-%m-%d")

    newest = datetime.now().strftime("%Y-%m-%d")

    # Fetch activities
    log.info("Fetching activities from %s to %s", oldest, newest)
    try:
        activities = _fetch_activities(session, athlete_id, oldest, newest)
    except requests.RequestException as e:
        log.error("Failed to fetch activities: %s", e)
        return {"success": False, "error": f"Failed to fetch activities: {e}"}

    log.debug("Received %d activities, filtering by types: %s", len(activities), activity_types)
    # Filter by type
    activities = [a for a in activities if a.get("type") in activity_types]
    log.info("%d activities after type filter", len(activities))

    synced = 0
    errors = []

    for activity in activities:
        activity_id_val = activity["id"]

        # Fetch intervals for classification
        try:
            intervals, groups = _fetch_intervals(session, activity_id_val)
            # Small delay to be polite to API
            time.sleep(0.1)
        except requests.RequestException as e:
            errors.append(f"{activity_id_val}: {e}")
            intervals = []
            groups = []

        # Classify and store; skip and record a single bad activity rather than aborting the whole sync
        try:
            cls = classify(activity, intervals, groups)
            now = datetime.now().isoformat()
            db.execute(
                """INSERT OR REPLACE INTO activities
                    (id, start_date_local, type, name, distance, moving_time,
                    elapsed_time, average_heartrate, average_speed,
                    icu_warmup_time, icu_cooldown_time,
                    classification, display_name, work_interval_time,
                    interval_count, interval_avg_duration, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    activity_id_val,
                    activity.get("start_date_local"),
                    activity.get("type"),
                    activity.get("name"),
                    activity.get("distance"),
                    activity.get("moving_time"),
                    activity.get("elapsed_time"),
                    activity.get("average_heartrate"),
                    activity.get("average_speed"),
                    activity.get("icu_warmup_time"),
                    activity.get("icu_cooldown_time"),
                    cls["classification"],
                    cls["display_name"],
                    cls["work_interval_time"],
                    cls["interval_count"],
                    cls["interval_avg_duration"],
                    now,
                ),
            )
            synced += 1
            if synced % 25 == 0:
                log.info("  %d/%d processed...", synced, len(activities))
        except Exception as e:
            errors.append(f"{activity_id_val}: {e}")

    # Detect deleted activities: remove local IDs not in API response
    api_ids = {a["id"] for a in activities}
    local_ids_cursor = db.execute(
        "SELECT id FROM activities WHERE start_date_local >= ? AND start_date_local <= ?",
        (oldest, newest + "T23:59:59"),
    )
    local_ids = {row[0] for row in local_ids_cursor.fetchall()}
    deleted_ids = local_ids - api_ids
    if deleted_ids:
        placeholders = ",".join("?" * len(deleted_ids))
        db.execute(f"DELETE FROM activities WHERE id IN ({placeholders})", list(deleted_ids))

    # Update sync state
    db.execute(
        "INSERT OR REPLACE INTO sync_state (key, value) VALUES ('last_sync_date', ?)",
        (newest,),
    )
    db.execute(
        "INSERT OR REPLACE INTO sync_state (key, value) VALUES ('last_sync_time', ?)",
        (datetime.now().isoformat(),),
    )
    db.commit()
    log.info("Sync complete: %d synced, %d deleted, %d errors", synced, len(deleted_ids), len(errors))

    return {
        "success": True,
        "synced": synced,
        "deleted": len(deleted_ids),
        "errors": errors,
    }
