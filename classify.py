"""Workout classification for Norwegian Singles Method running program."""

import math  # [AI-GENERATED] Reject non-finite work metrics.

# Minimum recovery duration (seconds) to count as a real interval recovery.
# Intervals.icu uses 1s recoveries for brief GPS/watch pauses.
MIN_RECOVERY_SECS = 15


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


# [AI-GENERATED START] Actual work sums use interval membership, not group averages.
def work_metrics(classification: str, intervals: list[dict], groups: list[dict]) -> dict:
    if classification != "interval":
        return {"work_interval_time": 0, "work_interval_distance": 0}

    group_ids = {
        group["id"] for group in groups if (group.get("count") or 0) >= 2
    }
    work = [
        interval for interval in intervals
        if interval.get("type") == "WORK" and interval.get("group_id") in group_ids
    ]
    if not work:
        raise ValueError("No work repetitions found in repeated interval groups")

    seen = set()
    total_time = 0
    total_distance = 0.0
    for interval in work:
        identity = interval.get("id")
        if identity is None:
            raise ValueError("Work interval has no id")
        if identity in seen:
            continue
        seen.add(identity)
        seconds = interval.get("moving_time")
        metres = interval.get("distance")
        for name, value in (("moving_time", seconds), ("distance", metres)):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"Work interval {identity} has invalid {name}")
        total_time += seconds
        total_distance += metres
    if total_time <= 0 or total_distance <= 0:
        raise ValueError("Work repetitions require positive total time and distance")
    return {
        "work_interval_time": total_time,
        "work_interval_distance": total_distance,
    }
# [AI-GENERATED END]
