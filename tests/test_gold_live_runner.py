from datetime import date, datetime
from zoneinfo import ZoneInfo

from extensions.gold_paper import GoldPaperConfig
from extensions.gold_paper.live_runner import (
    GoldPaperLiveSessionRunner,
    normalize_rating,
    parse_hhmm,
    rating_to_action,
)


def make_runner(cfg: GoldPaperConfig) -> GoldPaperLiveSessionRunner:
    runner = GoldPaperLiveSessionRunner.__new__(GoldPaperLiveSessionRunner)
    runner.config = cfg
    runner.realized_pnl_usd = 0.0
    runner.recommendations = []
    return runner


def test_parse_hhmm():
    t = parse_hhmm("08:15")
    assert t.hour == 8
    assert t.minute == 15


def test_ny_close_conversion_for_winter_and_summer():
    runner = make_runner(GoldPaperConfig())

    _, close_winter = runner.session_window_for_day(date(2026, 1, 15))
    _, close_summer = runner.session_window_for_day(date(2026, 7, 15))
    _, close_overlap = runner.session_window_for_day(date(2026, 3, 23))

    # NY 17:00 is usually 22:00 London (both in standard or DST),
    # but becomes 21:00 during US-DST / UK-standard overlap weeks.
    assert close_winter.hour == 22
    assert close_summer.hour == 22
    assert close_overlap.hour == 21


def test_lot_size_calculation():
    cfg = GoldPaperConfig(
        portfolio_equity_usd=100_000,
        risk_per_trade_pct=0.5,
        max_position_notional_usd=25_000,
        assumed_stop_distance_pct=0.006,
        contract_size_oz=100,
        max_lots=5.0,
    )
    runner = make_runner(cfg)
    lots, risk_budget = runner._calc_lots("BUY", price=2000.0)
    assert risk_budget == 500.0
    assert lots > 0
    assert lots <= 5.0


def test_next_interval_alignment():
    runner = make_runner(GoldPaperConfig(interval_minutes=15))
    now = datetime(2026, 3, 23, 10, 7, 44, tzinfo=ZoneInfo("Europe/London"))
    nxt = runner.next_interval(now)
    assert nxt.minute == 15
    assert nxt.second == 0


def test_rating_normalization_and_action():
    assert normalize_rating("buy") == "BUY"
    assert normalize_rating("Overweight with conviction") == "OVERWEIGHT"
    assert rating_to_action("UNDERWEIGHT") == "SELL"
