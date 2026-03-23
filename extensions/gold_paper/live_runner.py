from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Dict, List
from zoneinfo import ZoneInfo

import yfinance as yf

from .config import GoldPaperConfig
from .runner import GoldPaperAnalysisRunner


@dataclass
class TradeRecommendation:
    timestamp: str
    symbol: str
    rating: str
    action: str
    lots: float
    price: float
    risk_budget_usd: float
    reason: str
    pattern: str


def parse_hhmm(value: str) -> dtime:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid HH:MM value: {value}")
    hour, minute = int(parts[0]), int(parts[1])
    return dtime(hour=hour, minute=minute)


def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step


def extract_pattern(text: str) -> str:
    lowered = text.lower()
    patterns = {
        "Breakout": ["breakout", "break out"],
        "Reversal": ["reversal", "turnaround"],
        "Mean Reversion": ["mean reversion", "oversold", "overbought"],
        "Trend Following": ["trend", "momentum continuation"],
        "Range": ["range-bound", "sideways", "consolidation"],
        "Support/Resistance": ["support", "resistance"],
    }
    for label, keys in patterns.items():
        if any(k in lowered for k in keys):
            return label
    return "Unclassified"


def extract_reason(text: str, max_len: int = 260) -> str:
    compact = " ".join(text.split())
    if not compact:
        return "No rationale produced"
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 3] + "..."


def normalize_rating(raw: str) -> str:
    token = (raw or "").strip().upper()
    allowed = {"BUY", "OVERWEIGHT", "HOLD", "UNDERWEIGHT", "SELL"}
    if token in allowed:
        return token
    if "BUY" in token:
        return "BUY"
    if "SELL" in token:
        return "SELL"
    if "OVERWEIGHT" in token:
        return "OVERWEIGHT"
    if "UNDERWEIGHT" in token:
        return "UNDERWEIGHT"
    return "HOLD"


def rating_to_action(rating: str) -> str:
    if rating in {"BUY", "OVERWEIGHT"}:
        return "BUY"
    if rating in {"SELL", "UNDERWEIGHT"}:
        return "SELL"
    return "HOLD"


def rating_confidence(rating: str) -> float:
    if rating in {"BUY", "SELL"}:
        return 1.0
    if rating in {"OVERWEIGHT", "UNDERWEIGHT"}:
        return 0.65
    return 0.0


class GoldPaperLiveSessionRunner:
    """Runs 15m paper recommendations from London open to NY close."""

    def __init__(self, config: GoldPaperConfig):
        self.config = config
        self.analysis_runner = GoldPaperAnalysisRunner(config=config)
        self.realized_pnl_usd = 0.0
        self.recommendations: List[TradeRecommendation] = []

    def session_window_for_day(self, day: date) -> tuple[datetime, datetime]:
        london_tz = ZoneInfo(self.config.session_timezone)
        ny_tz = ZoneInfo("America/New_York")

        london_open = datetime.combine(
            day,
            parse_hhmm(self.config.london_open_time),
            tzinfo=london_tz,
        )

        ny_close = datetime.combine(
            day,
            parse_hhmm(self.config.newyork_close_time_ny),
            tzinfo=ny_tz,
        )
        ny_close_in_london = ny_close.astimezone(london_tz)

        if ny_close_in_london <= london_open:
            ny_close_in_london += timedelta(days=1)

        return london_open, ny_close_in_london

    def next_interval(self, now: datetime) -> datetime:
        minute = now.minute
        interval = self.config.interval_minutes
        next_minute_block = ((minute // interval) + 1) * interval
        next_tick = now.replace(second=0, microsecond=0)
        if next_minute_block >= 60:
            next_tick = (next_tick + timedelta(hours=1)).replace(minute=0)
        else:
            next_tick = next_tick.replace(minute=next_minute_block)
        return next_tick

    def _get_market_price(self) -> float:
        symbol = self.config.market_price_symbol
        data = yf.download(symbol, period="1d", interval="1m", progress=False)
        if not data.empty:
            val = data["Close"].dropna().iloc[-1]
            if hasattr(val, "item"):
                val = val.item()
            return float(val)

        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="2d", interval="15m")
        if hist.empty:
            raise RuntimeError(f"No market data for {symbol}")
        return float(hist["Close"].dropna().iloc[-1])

    def _calc_lots(self, rating: str, price: float) -> tuple[float, float]:
        action = rating_to_action(rating)
        confidence = rating_confidence(rating)

        if action == "HOLD" or confidence <= 0:
            return 0.0, 0.0

        risk_budget_usd = (
            self.config.portfolio_equity_usd
            * (self.config.risk_per_trade_pct / 100.0)
            * confidence
        )

        stop_distance_usd_per_oz = price * self.config.assumed_stop_distance_pct
        loss_per_lot = stop_distance_usd_per_oz * self.config.contract_size_oz
        lots_by_risk = risk_budget_usd / max(loss_per_lot, 1e-9)

        notional_per_lot = price * self.config.contract_size_oz
        lots_by_notional = self.config.max_position_notional_usd / max(notional_per_lot, 1e-9)

        lots = min(lots_by_risk, lots_by_notional, self.config.max_lots)
        lots = max(0.0, floor_to_step(lots, 0.01))
        return lots, risk_budget_usd

    def _run_single_cycle(self, now_london: datetime) -> List[TradeRecommendation]:
        outputs = self.analysis_runner.run()
        market_price = self._get_market_price()
        recs: List[TradeRecommendation] = []

        for out in outputs:
            full_text = out.get("final_trade_decision", "")
            rating = normalize_rating(out.get("decision", ""))
            action = rating_to_action(rating)

            if abs(self.realized_pnl_usd) >= self.config.max_daily_loss_usd:
                action = "HOLD"
                rating = "HOLD"

            lots, risk_budget = self._calc_lots(rating=rating, price=market_price)
            if action == "HOLD":
                lots = 0.0

            rec = TradeRecommendation(
                timestamp=now_london.isoformat(),
                symbol=out["symbol"],
                rating=rating,
                action=action,
                lots=lots,
                price=market_price,
                risk_budget_usd=risk_budget,
                reason=extract_reason(full_text),
                pattern=extract_pattern(full_text),
            )
            recs.append(rec)
            self.recommendations.append(rec)

        return recs

    def _build_summary(self, start: datetime, end: datetime) -> Dict:
        buys = [r for r in self.recommendations if r.action == "BUY"]
        sells = [r for r in self.recommendations if r.action == "SELL"]
        holds = [r for r in self.recommendations if r.action == "HOLD"]

        pattern_counts: Dict[str, int] = {}
        for rec in self.recommendations:
            pattern_counts[rec.pattern] = pattern_counts.get(rec.pattern, 0) + 1

        return {
            "session": {
                "timezone": self.config.session_timezone,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "interval_minutes": self.config.interval_minutes,
            },
            "portfolio": {
                "equity_usd": self.config.portfolio_equity_usd,
                "max_daily_loss_usd": self.config.max_daily_loss_usd,
                "risk_per_trade_pct": self.config.risk_per_trade_pct,
            },
            "decisions": {
                "total_cycles": len(self.recommendations),
                "buy_count": len(buys),
                "sell_count": len(sells),
                "hold_count": len(holds),
                "buy_lots_total": round(sum(r.lots for r in buys), 2),
                "sell_lots_total": round(sum(r.lots for r in sells), 2),
            },
            "patterns": pattern_counts,
            "latest_recommendations": [asdict(r) for r in self.recommendations[-5:]],
        }

    def _write_summary(self, summary: Dict, output_dir: str) -> tuple[Path, Path]:
        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = root / f"session_summary_{stamp}.json"
        md_path = root / f"session_summary_{stamp}.md"

        json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

        latest_rows = summary.get("latest_recommendations", [])
        lines = [
            "# Gold Session Summary",
            "",
            f"- Start: `{summary['session']['start']}`",
            f"- End: `{summary['session']['end']}`",
            f"- Interval: `{summary['session']['interval_minutes']}m`",
            "",
            "## Trade Direction Totals",
            "",
            f"- Buy count: `{summary['decisions']['buy_count']}`",
            f"- Sell count: `{summary['decisions']['sell_count']}`",
            f"- Hold count: `{summary['decisions']['hold_count']}`",
            f"- Total buy lots: `{summary['decisions']['buy_lots_total']}`",
            f"- Total sell lots: `{summary['decisions']['sell_lots_total']}`",
            "",
            "## Pattern Mix",
            "",
        ]
        for pattern, count in summary.get("patterns", {}).items():
            lines.append(f"- {pattern}: `{count}`")

        lines += [
            "",
            "## Latest Recommendations",
            "",
            "| Time | Symbol | Rating | Action | Lots | Price | Pattern | Reason |",
            "|---|---|---|---|---:|---:|---|---|",
        ]

        for row in latest_rows:
            reason = row["reason"].replace("|", "/")
            lines.append(
                f"| {row['timestamp']} | {row['symbol']} | {row['rating']} | {row['action']} | {row['lots']:.2f} | {row['price']:.2f} | {row['pattern']} | {reason} |"
            )

        md_path.write_text("\n".join(lines), encoding="utf-8")
        return json_path, md_path

    def run_session(self, output_dir: str = "overlay_results/gold_paper_live") -> Dict:
        london_tz = ZoneInfo(self.config.session_timezone)
        now = datetime.now(london_tz)
        if self.config.max_cycles:
            # Dry-run mode: execute immediately for N cycles regardless of clock.
            session_start = now
            session_end = now + timedelta(minutes=self.config.interval_minutes * self.config.max_cycles)
        else:
            session_start, session_end = self.session_window_for_day(now.date())
            if now < session_start:
                time.sleep((session_start - now).total_seconds())

        cycles = 0
        while True:
            now = datetime.now(london_tz)
            if not self.config.max_cycles and now > session_end:
                break
            if self.config.max_cycles and cycles >= self.config.max_cycles:
                break

            self._run_single_cycle(now)
            cycles += 1

            next_tick = self.next_interval(datetime.now(london_tz))
            sleep_seconds = (next_tick - datetime.now(london_tz)).total_seconds()
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

        summary = self._build_summary(session_start, session_end)
        json_path, md_path = self._write_summary(summary, output_dir)
        summary["summary_files"] = {
            "json": str(json_path.resolve()),
            "markdown": str(md_path.resolve()),
        }
        return summary


if __name__ == "__main__":
    runner = GoldPaperLiveSessionRunner(GoldPaperConfig())
    result = runner.run_session()
    print(json.dumps(result, indent=2))
