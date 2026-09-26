"""Things that differ on Windows, checked on any OS."""

from __future__ import annotations

import zoneinfo

from insta_outreach.util.clock import FakeClock, local_day_start


def test_timezones_work_without_a_system_tz_database() -> None:
    # Windows has no system time-zone database: zoneinfo then needs the `tzdata` package.
    zoneinfo.reset_tzpath(to=[])
    try:
        tz = zoneinfo.ZoneInfo.no_cache("Asia/Kolkata")
        assert tz.utcoffset(FakeClock().now()).total_seconds() == 5.5 * 3600
        assert local_day_start(FakeClock().now(), "Asia/Kolkata").isoformat() == "2026-09-20T18:30:00+00:00"
    finally:
        zoneinfo.reset_tzpath()
