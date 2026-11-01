"""[AI-GENERATED] Work-only calendar aggregation and local SVG chart geometry."""

from datetime import date, timedelta


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = round(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_work_pace(seconds_per_unit: float | None) -> str:
    if seconds_per_unit is None:
        return "-"
    seconds = round(seconds_per_unit)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _period_start(day: date, grouping: str) -> date:
    if grouping == "monthly":
        return day.replace(day=1)
    return day - timedelta(days=day.weekday())


def _next_period(day: date, grouping: str) -> date:
    if grouping == "monthly":
        return (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return day + timedelta(days=7)


def aggregate_stats(
    rows: list[dict], today: date, weeks: str = "12",
    grouping: str = "weekly", divisor: float = 1000,
) -> dict:
    if weeks not in ("12", "26", "52", "all"):
        raise ValueError("Unsupported stats range")
    if grouping not in ("weekly", "monthly"):
        raise ValueError("Unsupported stats grouping")
    if divisor <= 0:
        raise ValueError("Distance divisor must be positive")

    current_monday = _period_start(today, "weekly")
    if weeks == "all":
        dates = [date.fromisoformat(row["start_date_local"][:10]) for row in rows]
        start = _period_start(min(dates + [today]), "weekly")
    else:
        start = current_monday - timedelta(weeks=int(weeks) - 1)

    periods = {}
    day = _period_start(start, grouping)
    while day <= today:
        end = _next_period(day, grouping)
        iso = day.isocalendar()
        label = day.strftime("%Y-%m") if grouping == "monthly" else f"{iso.year}-W{iso.week:02d}"
        periods[day] = {
            "label": label, "start": day, "end": end - timedelta(days=1),
            "ongoing": day <= today < end,
            "partial": grouping == "monthly" and (day < start or end > today + timedelta(days=1)),
            "sessions": [], "session_count": 0, "missing_count": 0,
            "work_time": 0, "work_distance": 0.0, "pace": None,
        }
        day = end

    for row in rows:
        activity_day = date.fromisoformat(row["start_date_local"][:10])
        if not start <= activity_day <= today:
            continue
        if row["classification"] not in ("interval", "unknown"):
            continue
        period = periods[_period_start(activity_day, grouping)]
        session = dict(row)
        session["pace"] = None
        if row["classification"] == "interval":
            period["session_count"] += 1
        valid = (
            row.get("work_metrics_status") == "complete"
            and row.get("work_interval_distance") is not None
            and row.get("work_interval_time") is not None
            and row["work_interval_distance"] > 0
            and row["work_interval_time"] > 0
        )
        if valid:
            period["work_time"] += row["work_interval_time"]
            period["work_distance"] += row["work_interval_distance"] / divisor
            session["pace"] = row["work_interval_time"] * divisor / row["work_interval_distance"]
        else:
            period["missing_count"] += 1
        period["sessions"].append(session)

    result = list(periods.values())
    for period in result:
        period["complete"] = period["missing_count"] == 0
        if period["complete"] and period["work_distance"] > 0:
            period["pace"] = period["work_time"] / period["work_distance"]
    return {"periods": result, "start": start, "end": today}


def chart_geometry(periods: list[dict], metric: str) -> dict:
    if metric not in ("pace", "work_distance"):
        raise ValueError("Unsupported chart metric")
    width = max(760, 70 + len(periods) * 18)
    left, top, height = 65, 20, 180
    plot_width = width - left - 20
    step = plot_width / max(len(periods), 1)
    values = [
        period[metric] for period in periods
        if period["complete"] and period[metric] is not None
    ]
    if metric == "pace":
        low = min(values, default=240)
        high = max(values, default=360)
        padding = max((high - low) * 0.1, 5)
        low, high = max(0, low - padding), high + padding
    else:
        low, high = 0, max(max(values, default=0) * 1.1, 1)

    def y(value):
        fraction = (value - low) / (high - low)
        return top + height * (fraction if metric == "pace" else 1 - fraction)

    ticks = []
    for index in range(5):
        value = low + (high - low) * index / 4
        ticks.append({
            "y": y(value),
            "label": format_work_pace(value) if metric == "pace" else f"{value:.1f}",
        })

    points, segments, labels = [], [], []
    previous = None
    label_every = max(1, (len(periods) + 9) // 10)
    for index, period in enumerate(periods):
        x = left + step * (index + 0.5)
        if index % label_every == 0 or index == len(periods) - 1:
            labels.append({"x": x, "label": period["label"]})
        value = period[metric] if period["complete"] else None
        if value is None:
            previous = None
            continue
        point = {
            "x": x, "y": y(value), "bar_x": x - step * 0.34,
            "bar_width": step * 0.68, "bar_height": top + height - y(value),
            "label": period["label"],
            "value": format_work_pace(value) if metric == "pace" else f"{value:.2f}",
            "ongoing": period["ongoing"], "partial": period["partial"],
        }
        if previous is not None:
            segments.append({"a": previous, "b": point})
        points.append(point)
        previous = point
    return {
        "width": width, "height": 260, "left": left, "right": width - 20,
        "bottom": top + height, "ticks": ticks, "labels": labels,
        "points": points, "segments": segments, "metric": metric,
    }
