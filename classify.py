"""Workout classification for Norwegian Singles Method running program."""

# Minimum recovery duration (seconds) to count as a real interval recovery.
# Intervals.icu uses 1s recoveries for brief GPS/watch pauses.
MIN_RECOVERY_SECS = 15


def work_totals(intervals: list[dict], groups: list[dict]) -> tuple[int, float]:
    """Sum time (seconds) and distance (metres) of the repeated WORK intervals.

    Warm-up, cool-down and recoveries are excluded. Raises ValueError if no
    work interval has both time and distance.
    """
    repeated = {g["id"] for g in groups if g.get("count", 1) >= 2}
    work = [
        i for i in intervals
        if i.get("type") == "WORK" and i.get("group_id") in repeated
    ]
    seconds = sum(i.get("moving_time") or 0 for i in work)
    metres = sum(i.get("distance") or 0 for i in work)
    if seconds <= 0 or metres <= 0:
        raise ValueError("no work intervals with time and distance")
    return seconds, metres


def classify(activity: dict, intervals: list[dict], groups: list[dict]) -> dict:
    """Classify an activity based on its interval groups and duration.

    Args:
        activity: dict with activity fields (moving_time, sub_type, etc.)
        intervals: list of interval dicts from icu_intervals
        groups: list of group dicts from icu_groups

    Returns a dict with classification fields to merge into the activity record.
    """
    # A race (Intervals.icu sub_type == "RACE") always wins over interval/duration
    # based detection, since it's an explicit signal from the athlete/watch.
    if activity.get("sub_type") == "RACE":
        return {
            "classification": "race",
            "display_name": "Race",
            "work_interval_time": 0,
            "work_interval_distance": 0,
            "interval_count": 0,
            "interval_avg_duration": 0,
        }

    # Check for structured intervals:
    # 1. A group with count >= 2 (repeated work intervals)
    # 2. Real recovery intervals (duration >= MIN_RECOVERY_SECS)
    has_real_recoveries = any(
        i.get("type") == "RECOVERY" and (i.get("moving_time", 0) or 0) >= MIN_RECOVERY_SECS
        for i in intervals
    )

    rep_group = None
    if has_real_recoveries and groups:
        # Find the group with the highest count (the main interval set)
        for group in groups:
            count = group.get("count", 1)
            if count >= 2:
                if rep_group is None or count > rep_group.get("count", 0):
                    rep_group = group

    if rep_group:
        count = rep_group["count"]
        avg_duration_secs = rep_group.get("moving_time", 0) or 0
        avg_duration_mins = round(avg_duration_secs / 60)
        work_secs, work_metres = work_totals(intervals, groups)
        return {
            "classification": "interval",
            "display_name": f"SubT-{count}x{avg_duration_mins}",
            "work_interval_time": work_secs,
            "work_interval_distance": work_metres,
            "interval_count": count,
            "interval_avg_duration": round(avg_duration_secs),
        }

    moving_time_secs = activity.get("moving_time", 0) or 0
    moving_time_mins = moving_time_secs / 60

    if moving_time_mins >= 60:
        return {
            "classification": "long",
            "display_name": f"Long-{round(moving_time_mins)}",
            "work_interval_time": 0,
            "work_interval_distance": 0,
            "interval_count": 0,
            "interval_avg_duration": 0,
        }

    return {
        "classification": "easy",
        "display_name": f"Easy-{round(moving_time_mins)}",
        "work_interval_time": 0,
        "work_interval_distance": 0,
        "interval_count": 0,
        "interval_avg_duration": 0,
    }
