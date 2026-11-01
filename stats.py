"""SubT work-interval totals per week/month, and SVG chart geometry for the stats page."""

from datetime import date, timedelta

from formatting import format_pace_seconds


def _period_start(day, grouping):
    if grouping == "monthly":
        return day.replace(day=1)
    return day - timedelta(days=day.weekday())  # Monday


def _next_start(start, grouping):
    if grouping == "monthly":
        return (start + timedelta(days=32)).replace(day=1)
    return start + timedelta(days=7)


def _label(start, grouping):
    if grouping == "monthly":
        return start.strftime("%Y-%m")
    year, week, _ = start.isocalendar()
    return f"{year}-W{week:02d}"


def aggregate_stats(rows, today, weeks=12, grouping="weekly", divisor=1000):
    """Total work-only time/distance per period, oldest first.

    rows: activity dicts. weeks: how many calendar weeks back, or None for all
    history. Monthly grouping uses whole calendar months from the one containing
    the range start. Pace is total work time / total work distance (seconds per
    km or mile), never an average of session paces. Interval sessions without
    work metrics yet count as `missing`, and make that period's pace unknown.
    """
    sessions = [r for r in rows if r["classification"] == "interval"]
    if weeks is None:
        first = min(
            (date.fromisoformat(r["start_date_local"][:10]) for r in sessions),
            default=today,
        )
    else:
        first = today - timedelta(weeks=weeks - 1, days=today.weekday())

    periods = {}
    start = _period_start(first, grouping)
    while start <= today:
        end = _next_start(start, grouping)
        periods[start] = {
            "label": _label(start, grouping),
            "ongoing": end > today,
            "sessions": [],
            "work_time": 0,
            "work_distance": 0.0,
            "missing": 0,
            "pace": None,
        }
        start = end

    for row in sessions:
        day = date.fromisoformat(row["start_date_local"][:10])
        period = periods.get(_period_start(day, grouping))
        if period is None:
            continue
        session = dict(row, pace=None)
        if row["work_interval_distance"]:
            session["pace"] = row["work_interval_time"] * divisor / row["work_interval_distance"]
            period["work_time"] += row["work_interval_time"]
            period["work_distance"] += row["work_interval_distance"] / divisor
        else:
            period["missing"] += 1
        period["sessions"].append(session)

    for period in periods.values():
        if period["work_distance"] and not period["missing"]:
            period["pace"] = period["work_time"] / period["work_distance"]
    return list(periods.values())


def chart_geometry(periods, metric):
    """Pre-computed SVG coordinates for a chart over `periods`.

    metric 'pace' is drawn as a line with faster (lower) pace higher up;
    'work_distance' as bars. Periods with unknown values leave a gap.
    """
    width = max(760, 70 + len(periods) * 18)
    left, top, height = 65, 20, 180
    step = (width - left - 20) / len(periods)

    def value(period):
        return None if period["missing"] else period[metric]

    known = [value(p) for p in periods if value(p) is not None]
    if metric == "pace":
        low, high = min(known, default=240), max(known, default=360)
        pad = max((high - low) * 0.1, 5)
        low, high = max(low - pad, 0), high + pad
    else:
        low, high = 0, max(max(known, default=0) * 1.1, 1)

    def y(v):
        fraction = (v - low) / (high - low)
        return top + height * (fraction if metric == "pace" else 1 - fraction)

    def tick_label(v):
        return format_pace_seconds(v) if metric == "pace" else f"{v:.1f}"

    ticks = [
        {"y": y(low + (high - low) * i / 4), "label": tick_label(low + (high - low) * i / 4)}
        for i in range(5)
    ]

    points, segments, labels = [], [], []
    previous = None
    label_every = max(1, (len(periods) + 9) // 10)
    for i, period in enumerate(periods):
        x = left + step * (i + 0.5)
        if i % label_every == 0:
            labels.append({"x": x, "label": period["label"]})
        v = value(period)
        if v is None:
            previous = None
            continue
        point = {
            "x": x, "y": y(v), "label": period["label"],
            "text": tick_label(v) if metric == "pace" else f"{v:.2f}",
            "faded": period["ongoing"],
        }
        if previous:
            segments.append((previous, point))
        points.append(point)
        previous = point
    return {
        "width": width, "height": top + height + 40, "left": left,
        "right": width - 20, "bottom": top + height, "bar_width": step * 0.68,
        "ticks": ticks, "labels": labels, "points": points, "segments": segments,
    }
