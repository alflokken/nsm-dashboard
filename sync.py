"""Intervals.icu sync engine for NSM."""

import logging
import sqlite3
import time
import requests
from datetime import datetime, timedelta, timezone

from classify import classify, work_metrics

log = logging.getLogger(__name__)

API_BASE = "https://intervals.icu/api/v1"
FIELDS = "id,start_date_local,type,sub_type,name,distance,moving_time,elapsed_time,average_heartrate,average_speed,icu_warmup_time,icu_cooldown_time"
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


def _fetch_athlete_prefs(session, athlete_id):
    """Fetch athlete preferences (units, timezone, date/time format)."""
    url = f"{API_BASE}/athlete/{athlete_id}"
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    # `or` (not a .get default) also covers the API returning an explicit
    # JSON null for these fields, not just an absent key.
    return {
        "pref_measurement": data.get("measurement_preference") or "meters",
        "pref_timezone": data.get("timezone") or "Europe/Oslo",
        "pref_date_format": data.get("date_format") or "",
        "pref_time_format": data.get("time_format") or "",
    }


def _save_prefs(db, prefs):
    """Persist athlete preferences to sync_state."""
    # Drop keys from earlier, incorrect versions of this app.
    db.execute("DELETE FROM sync_state WHERE key = 'pref_pace_units'")
    for key, value in prefs.items():
        db.execute(
            "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
            (key, value),
        )


def refresh_prefs(db, config: dict, session=None) -> bool:
    """Fetch and persist athlete preferences only (no activity sync).

    Reuses `session` if given (e.g. from an in-progress sync); otherwise
    opens and closes its own. Returns True on success, False if the fetch
    failed — existing/stale prefs are left untouched on failure.
    """
    owns_session = session is None
    if owns_session:
        session = _session(config["api_key"])
    try:
        prefs = _fetch_athlete_prefs(session, config["athlete_id"])
    except requests.RequestException as e:
        log.warning("Failed to fetch athlete preferences: %s", e)
        return False
    finally:
        if owns_session:
            session.close()
    _save_prefs(db, prefs)
    # Commit immediately: preference refresh is a self-contained unit of
    # work and must not be rolled back if the rest of a sync fails later.
    db.commit()
    return True


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
        # [AI-GENERATED] A malformed payload is an error, not an easy workout.
        if not isinstance(data, dict):
            raise ValueError("Interval response must be an object")
        intervals = data.get("icu_intervals")
        groups = data.get("icu_groups")
        if not isinstance(intervals, list) or not isinstance(groups, list):
            raise ValueError("Interval response is missing interval/group lists")
        return intervals, groups


# [AI-GENERATED START] Shared persistence and resumable historical work metrics.
def _store_activity(db, activity, cls):
    fields = [
        "id", "start_date_local", "type", "name", "distance", "moving_time",
        "elapsed_time", "average_heartrate", "average_speed", "icu_warmup_time",
        "icu_cooldown_time", "classification", "display_name", "work_interval_time",
        "interval_count", "interval_avg_duration", "work_interval_distance",
        "work_metrics_status", "work_metrics_error", "synced_at",
    ]
    record = {
        **activity, **cls, "synced_at": datetime.now().isoformat(),
    }
    updates = ", ".join(f"{field} = excluded.{field}" for field in fields[1:])
    db.execute(
        f"""INSERT INTO activities ({', '.join(fields)})
            VALUES ({', '.join('?' for _ in fields)})
            ON CONFLICT(id) DO UPDATE SET {updates}""",
        [record.get(field) for field in fields],
    )


def _sync_activity(db, session, activity, errors):
    cls = None
    try:
        intervals, groups = _fetch_intervals(session, activity["id"])
        cls = classify(activity, intervals, groups)
        cls.update(work_metrics(cls["classification"], intervals, groups))
        cls.update(work_metrics_status="complete", work_metrics_error=None)
        _store_activity(db, activity, cls)
        return True
    except (requests.RequestException, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        message = f"{activity['id']}: {exc}"
        log.warning("Activity sync failed: %s", message)
        errors.append(message)
        existing = db.execute(
            "SELECT id FROM activities WHERE id = ?", (activity["id"],)
        ).fetchone()
        if existing:
            db.execute(
                """UPDATE activities SET work_metrics_status = 'failed',
                   work_metrics_error = ? WHERE id = ?""",
                (str(exc), activity["id"]),
            )
        else:
            if cls is None:
                cls = classify(activity, [], [])
                if cls["classification"] != "race":
                    cls.update(classification="unknown", display_name="Unavailable")
            cls.update(
                work_interval_time=None, work_interval_distance=None,
                work_metrics_status="failed", work_metrics_error=str(exc),
            )
            _store_activity(db, activity, cls)
        return False
    finally:
        time.sleep(0.1)


def backfill_remaining(db):
    return db.execute(
        """SELECT COUNT(*) FROM activities
           WHERE work_metrics_status != 'complete'
           AND classification IN ('interval', 'unknown')"""
    ).fetchone()[0]


def _backfill_work_metrics(db, session, attempted, errors):
    db.execute(
        """INSERT OR REPLACE INTO sync_state (key, value)
           VALUES ('work_metrics_backfill_attempted', ?)""",
        (datetime.now(timezone.utc).isoformat(),),
    )
    db.commit()
    cursor = db.execute(
        """SELECT * FROM activities WHERE work_metrics_status != 'complete'
           AND classification IN ('interval', 'unknown')
           ORDER BY start_date_local, id"""
    )
    fields = [column[0] for column in cursor.description]
    candidates = [dict(zip(fields, row)) for row in cursor.fetchall()]
    completed = 0
    for index, activity in enumerate(candidates, 1):
        if activity["id"] not in attempted:
            completed += _sync_activity(db, session, activity, errors)
        if index % 25 == 0:
            db.commit()
            log.info("Work metrics backfill: %d/%d processed", index, len(candidates))
    remaining = backfill_remaining(db)
    if not remaining:
        db.execute(
            """INSERT OR IGNORE INTO sync_state (key, value)
               VALUES ('work_metrics_backfill_completed', ?)""",
            (datetime.now(timezone.utc).isoformat(),),
        )
    else:
        db.execute(
            "DELETE FROM sync_state WHERE key = 'work_metrics_backfill_completed'"
        )
    db.commit()
    return {"completed": completed, "remaining": remaining}
# [AI-GENERATED END]


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

    # Refresh athlete preferences (pace units, timezone, date/time format)
    # before fetching/processing activity data, so this sync uses current values.
    refresh_prefs(db, config, session=session)
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
        # [AI-GENERATED] Failed interval fetches preserve previously valid data.
        synced += _sync_activity(db, session, activity, errors)
        if synced and synced % 25 == 0:
            db.commit()
            log.info("  %d/%d processed...", synced, len(activities))

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

    # [AI-GENERATED] Historical candidates are independent of the recent sync window.
    backfill = _backfill_work_metrics(db, session, api_ids, errors)

    # Update sync state
    db.execute(
        "INSERT OR REPLACE INTO sync_state (key, value) VALUES ('last_sync_date', ?)",
        (newest,),
    )
    db.execute(
        "INSERT OR REPLACE INTO sync_state (key, value) VALUES ('last_sync_time', ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    db.commit()
    log.info("Sync complete: %d synced, %d deleted, %d errors", synced, len(deleted_ids), len(errors))

    return {
        "success": True,
        "synced": synced,
        "deleted": len(deleted_ids),
        "errors": errors,
        "backfill": backfill,  # [AI-GENERATED]
    }
