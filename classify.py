"""Workout classification for Norwegian Singles Method running program."""

# Minimum recovery duration (seconds) to count as a real interval recovery.
# Intervals.icu uses 1s recoveries for brief GPS/watch pauses.
MIN_RECOVERY_SECS = 15


def classify(activity: dict, intervals: list[dict], groups: list[dict]) -> dict:
    """Classify an activity based on its interval groups and duration.

    Args:
        activity: dict with activity fields (moving_time, etc.)
        intervals: list of interval dicts from icu_intervals
        groups: list of group dicts from icu_groups

    Returns a dict with classification fields to merge into the activity record.
    """
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
        total_work_secs = count * avg_duration_secs
        return {
            "classification": "interval",
            "display_name": f"SubT-{count}x{avg_duration_mins}",
            "work_interval_time": total_work_secs,
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
            "interval_count": 0,
            "interval_avg_duration": 0,
        }

    return {
        "classification": "easy",
        "display_name": f"Easy-{round(moving_time_mins)}",
        "work_interval_time": 0,
        "interval_count": 0,
        "interval_avg_duration": 0,
    }
