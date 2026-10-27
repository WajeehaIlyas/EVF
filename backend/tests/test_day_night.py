from __future__ import annotations

import pytest

from app.services.day_night import (
    TimestampParseError,
    analyse_timestamp,
    parse_timestamp,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10/04/2026 05:09:24 +01:00", "Night"),
        ("10/04/2026 06:59:59 +01:00", "Night"),
        ("10/04/2026 07:00:00 +01:00", "Day"),
        ("10/04/2026 12:00:00 +01:00", "Day"),
        ("10/04/2026 18:59:59 +01:00", "Day"),
        ("10/04/2026 19:00:00 +01:00", "Night"),
        ("10/04/2026 00:00:00 +01:00", "Night"),
        ("2026-04-10T12:30:00Z", "Day"),
        ("2026-04-10 23:15:00", "Night"),
    ],
)
def test_day_night_boundaries(raw: str, expected: str) -> None:
    assert analyse_timestamp(raw).day_night == expected


def test_uses_wall_clock_not_utc() -> None:
    # 05:09 local at +01:00 is 04:09 UTC; label must follow the printed (local) hour.
    result = analyse_timestamp("10/04/2026 07:30:00 +01:00")
    assert result.day_night == "Day"
    assert result.local_naive.hour == 7 and result.local_naive.tzinfo is None


def test_dayfirst_default_and_override() -> None:
    assert parse_timestamp("10/04/2026 05:09:24").month == 4
    assert parse_timestamp("10/04/2026 05:09:24", dayfirst=False).month == 10


@pytest.mark.parametrize("bad", ["", "   ", "garbage", "32/13/2026 25:61:00"])
def test_bad_timestamps_raise(bad: str) -> None:
    with pytest.raises(TimestampParseError):
        parse_timestamp(bad)
