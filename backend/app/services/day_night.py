"""Timestamp parsing and Day/Night classification.

Rule: the *local wall-clock hour printed in the timestamp* decides the label,
so ``10/04/2026 05:09:24 +01:00`` is Night regardless of the UTC offset.

    Day   = 07:00-18:59
    Night = 19:00-06:59
"""

from __future__ import annotations

import itertools
import re
from datetime import datetime
from functools import lru_cache
from typing import Literal, NamedTuple

DayNight = Literal["Day", "Night"]

DAY_START_HOUR = 7     # inclusive
NIGHT_START_HOUR = 19  # inclusive


class TimestampParseError(ValueError):
    """Raised when a timestamp string matches none of the supported formats."""


class ParsedTimestamp(NamedTuple):
    original: datetime     # timezone-aware if the string carried an offset
    local_naive: datetime  # wall-clock time without tzinfo (what the DB stores)
    day_night: DayNight


@lru_cache(maxsize=2)
def _formats(dayfirst: bool) -> tuple[str, ...]:
    """Generate the strptime formats tried, e.g. ``%d/%m/%Y %H:%M:%S %z``."""
    dates = ("%d/%m/%Y", "%Y-%m-%d") if dayfirst else ("%m/%d/%Y", "%Y-%m-%d")
    separators = (" ", "T")
    times = ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M")
    zones = (" %z", "%z", "")
    return tuple(
        f"{d}{s}{t}{z}" for d, s, t, z in itertools.product(dates, separators, times, zones)
    )


def parse_timestamp(value: str, *, dayfirst: bool = True) -> datetime:
    """Parse a timestamp string such as ``10/04/2026 05:09:24 +01:00``.

    Args:
        value: Raw timestamp text.
        dayfirst: Interpret ``10/04/2026`` as 10 April (default, UK style) rather
            than 4 October.

    Raises:
        TimestampParseError: if the value is empty or in an unsupported format.
    """
    if not isinstance(value, str) or not value.strip():
        raise TimestampParseError("Timestamp is empty or not a string")

    text = re.sub(r"\s+", " ", value.strip())
    for fmt in _formats(dayfirst):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:  # last resort: any other ISO-8601 variant
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise TimestampParseError(f"Unrecognised timestamp format: {value!r}") from exc


def classify_day_night(
    moment: datetime,
    *,
    day_start: int = DAY_START_HOUR,
    night_start: int = NIGHT_START_HOUR,
) -> DayNight:
    """Return ``'Day'`` if the wall-clock hour is in ``[day_start, night_start)``."""
    if not 0 <= day_start < night_start <= 23:
        raise ValueError("Require 0 <= day_start < night_start <= 23")
    return "Day" if day_start <= moment.hour < night_start else "Night"


def analyse_timestamp(
    value: str,
    *,
    dayfirst: bool = True,
    day_start: int = DAY_START_HOUR,
    night_start: int = NIGHT_START_HOUR,
) -> ParsedTimestamp:
    """Parse ``value`` and classify it in one step."""
    parsed = parse_timestamp(value, dayfirst=dayfirst)
    return ParsedTimestamp(
        original=parsed,
        local_naive=parsed.replace(tzinfo=None),
        day_night=classify_day_night(parsed, day_start=day_start, night_start=night_start),
    )
