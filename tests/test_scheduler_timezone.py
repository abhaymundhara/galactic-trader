from datetime import datetime

import pytz

from scheduler_service import local_time_to_utc


def test_local_time_to_utc_london():
    h, m = local_time_to_utc(8, 0, "Europe/London")
    assert 0 <= h <= 23
    assert m == 0


def test_local_time_to_utc_newyork_round_trip():
    local_h, local_m = 9, 30
    utc_h, utc_m = local_time_to_utc(local_h, local_m, "America/New_York")

    ny_tz = pytz.timezone("America/New_York")
    utc_tz = pytz.UTC
    today_ny = datetime.now(ny_tz).date()
    ny_dt = ny_tz.localize(
        datetime(today_ny.year, today_ny.month, today_ny.day, local_h, local_m)
    )
    expected_utc = ny_dt.astimezone(utc_tz)

    assert utc_h == expected_utc.hour
    assert utc_m == expected_utc.minute
