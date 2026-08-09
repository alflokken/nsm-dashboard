"""Pure formatting/preference-interpretation helpers for Intervals.icu prefs."""

# Intervals.icu joins CLDR/DateTimeFormatter pattern components with "_",
# e.g. "HH_mm" or "EEE_dd_MMM_yyyy". Components arrive as discrete
# underscore-separated tokens (not a contiguous pattern string), so exact
# per-token lookup is enough — no full CLDR pattern parser needed.
_TOKEN_TO_STRFTIME = {
    "yyyy": "%Y", "yy": "%y",
    "MMMM": "%B", "MMM": "%b", "MM": "%m", "M": "%m",
    "dd": "%d", "d": "%d",
    "EEEE": "%A", "EEE": "%a", "EE": "%a", "E": "%a",
    "HH": "%H", "H": "%H",
    "hh": "%I", "h": "%I",
    "mm": "%M", "m": "%M",
    "ss": "%S", "s": "%S",
    "a": "%p",
}


def _cldr_to_strftime(raw, separator, fallback):
    """Translate an underscore-joined CLDR pattern into a strftime string.

    Unrecognized tokens are dropped rather than leaking raw CLDR letters
    into the rendered output; if nothing is recognized, `fallback` is
    returned. Never raises on garbage/empty/None input.
    """
    tokens = [t for t in (raw or "").split("_") if t]
    parts = [_TOKEN_TO_STRFTIME[t] for t in tokens if t in _TOKEN_TO_STRFTIME]
    return separator.join(parts) if parts else fallback


def date_format_to_strftime(raw_date_format):
    """e.g. 'EEE_dd_MMM_yyyy' -> '%a %d %b %Y'."""
    return _cldr_to_strftime(raw_date_format, " ", "%d %b %Y")


def time_format_to_strftime(raw_time_format):
    """e.g. 'HH_mm' -> '%H:%M'."""
    return _cldr_to_strftime(raw_time_format, ":", "%H:%M")


def is_imperial_measurement(measurement_preference):
    """True if the athlete's measurement_preference implies imperial units.

    Intervals.icu's global Distance setting is exposed as "meters" (metric)
    or "feet" (imperial) rather than literal "metric"/"imperial" strings.
    """
    return "feet" in (measurement_preference or "").lower()
