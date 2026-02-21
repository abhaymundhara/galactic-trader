"""
Backtesting engine for Galactic Trader.

Usage (CLI):
    python backtest.py --symbol NVDA --start 2024-01-01 --end 2024-06-30 --strategy trend_riding
    python backtest.py --symbol AAPL,MSFT --start 2024-01-01 --end 2024-03-31 --parallel

FastAPI endpoint:
    POST /backtest  { "symbol": "NVDA", "strategy": "trend_riding", "start": "2024-01-01", "end": "2024-06-30" }
"""
from __future__ import annotations
import argparse
import asyncio
import math
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
import pandas as pd

from regime import Regime, detect_regime

# ── Indicator helpers (mirror agent.py — no circular import) ──────────────────
try:
    from ta.momentum import RSIIndicator
    from ta.trend import ADXIndicator, EMAIndicator, MACD
    from ta.volatility import AverageTrueRange, BollingerBands
    from ta.volume import VolumeWeightedAveragePrice
    _TA_OK = True
except ImportError:
    _TA_OK = False


@dataclass
class BacktestResult:
    symbol: str
    strategy: str
    start: str
    end: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_return_pct: float = 0.0
    sharpe: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate: float = 0.0
    # raw trade log
    trade_log: list[dict] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "period": f"{self.start} → {self.end}",
            "trades": self.trades,
            "win_rate": round(self.win_rate, 4),
            "total_return_pct": round(self.total_return_pct, 4),
            "sharpe": round(self.sharpe, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
        }


# ── Data fetching ─────────────────────────────────────────────────────────────

async def _fetch_historical_bars(
    symbol: str, start: str, end: str, timeframe: str = "5Min"
) -> pd.DataFrame:
    """Fetch bars from Alpaca Data API (no auth required for some tickers via paper feed)."""
    import os
    from dotenv import load_dotenv
    load_dotenv()
    key    = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")

    is_crypto = "/" in symbol or "USDT" in symbol
    if is_crypto:
        sym_enc = symbol.replace("/", "%2F")
        url = f"https://data.alpaca.markets/v1beta3/crypto/us/bars?symbols={sym_enc}&timeframe={timeframe}&start={start}&end={end}&limit=10000&feed=us"
    else:
        url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe={timeframe}&start={start}&end={end}&limit=10000&feed=iex"

    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret} if key else {}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers)
    if r.status_code != 200:
        return pd.DataFrame()

    data = r.json()
    if is_crypto:
        bars = data.get("bars", {}).get(symbol, [])
    else:
        bars = data.get("bars", [])

    if not bars:
        return pd.DataFrame()

    df = pd.DataFrame(bars)
    df.rename(columns={"t": "timestamp", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "vw": "vwap"}, inplace=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def _compute_indicators_sync(df: pd.DataFrame) -> pd.DataFrame:
    """Add indicator columns to the dataframe (vectorised, no row-by-row loop)."""
    if not _TA_OK or len(df) < 40:
        return df
    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df.get("volume", pd.Series(0.0, index=df.index))

    df["ema9"]  = EMAIndicator(close, window=9).ema_indicator()
    df["ema21"] = EMAIndicator(close, window=21).ema_indicator()
    df["rsi"]   = RSIIndicator(close, window=14).rsi()
    macd_obj    = MACD(close)
    df["macd"]        = macd_obj.macd()
    df["macd_signal"] = macd_obj.macd_signal()
    df["adx"]   = ADXIndicator(high, low, close, window=14).adx()
    df["atr"]   = AverageTrueRange(high, low, close, window=14).average_true_range()
    bb          = BollingerBands(close, window=20, window_dev=2)
    df["bb_high"]  = bb.bollinger_hband()
    df["bb_low"]   = bb.bollinger_lband()
    df["bb_width"] = (df["bb_high"] - df["bb_low"]) / close.replace(0, float("nan"))
    df = df.dropna(subset=["ema9", "ema21", "rsi", "adx", "atr"]).reset_index(drop=True)
    return df


# ── Strategy signal functions ─────────────────────────────────────────────────

def _signal_trend_riding(row: pd.Series) -> str:
    """Go long when trend is clear, exit when trend reverses."""
    if row["ema9"] > row["ema21"] and row["macd"] > row["macd_signal"] and row["adx"] > 18 and 40 < row["rsi"] < 70:
        return "buy"
    if row["ema9"] < row["ema21"] and row["macd"] < row["macd_signal"]:
        return "sell"
    return "hold"


def _signal_mean_reversion(row: pd.Series) -> str:
    """Buy oversold bounces at lower BB, sell overbought at upper BB."""
    if row["close"] <= row["bb_low"] and row["rsi"] < 35:
        return "buy"
    if row["close"] >= row["bb_high"] and row["rsi"] > 65:
        return "sell"
    return "hold"


def _signal_regime_adaptive(row: pd.Series) -> str:
    """Pick strategy based on detected regime (mirrors live agent behaviour)."""
    indicators = {
        "adx": row["adx"], "bb_width": row["bb_width"],
        "atr": row["atr"], "price": row["close"],
        "macd": row["macd"], "macd_signal": row["macd_signal"],
        "ema_cross": "bullish" if row["ema9"] > row["ema21"] else "bearish",
    }
    regime = detect_regime(indicators)
    if regime == Regime.VOLATILE:
        return "hold"
    if regime in (Regime.BULL,):
        return _signal_trend_riding(row)
    return _signal_mean_reversion(row)


STRATEGIES = {
    "trend_riding":    _signal_trend_riding,
    "mean_reversion":  _signal_mean_reversion,
    "regime_adaptive": _signal_regime_adaptive,
}


# ── Core backtest loop ────────────────────────────────────────────────────────

def _run_backtest_on_df(
    df: pd.DataFrame,
    symbol: str,
    strategy: str,
    starting_cash: float = 10_000.0,
    position_pct: float = 0.10,    # max 10% per trade
    atr_sl_mult: float = 1.5,
    atr_tp_mult: float = 2.5,
) -> BacktestResult:
    sig_fn = STRATEGIES.get(strategy, _signal_trend_riding)
    cash = starting_cash
    position_qty = 0.0
    avg_cost = 0.0
    sl = tp = 0.0
    equity_curve: list[float] = []
    trade_log: list[dict] = []

    for _, row in df.iterrows():
        price = float(row["close"])
        equity = cash + position_qty * price
        equity_curve.append(equity)

        # SL/TP check
        if position_qty > 0:
            if sl > 0 and price <= sl:
                cash += position_qty * price
                trade_log.append({"exit_price": price, "entry_price": avg_cost, "reason": "stop_loss", "pnl": (price - avg_cost) * position_qty})
                position_qty = avg_cost = sl = tp = 0.0
                continue
            if tp > 0 and price >= tp:
                cash += position_qty * price
                trade_log.append({"exit_price": price, "entry_price": avg_cost, "reason": "take_profit", "pnl": (price - avg_cost) * position_qty})
                position_qty = avg_cost = sl = tp = 0.0
                continue

        signal = sig_fn(row)

        if signal == "buy" and position_qty == 0:
            spend = equity * position_pct
            qty = spend / price
            if qty > 0 and cash >= spend:
                atr = float(row.get("atr", 0) or 0)
                position_qty = qty
                avg_cost = price
                cash -= spend
                sl = round(price - atr_sl_mult * atr, 6) if atr > 0 else 0.0
                tp = round(price + atr_tp_mult * atr, 6) if atr > 0 else 0.0
        elif signal == "sell" and position_qty > 0:
            cash += position_qty * price
            trade_log.append({"exit_price": price, "entry_price": avg_cost, "reason": "signal_exit", "pnl": (price - avg_cost) * position_qty})
            position_qty = avg_cost = sl = tp = 0.0

    # Final mark-to-market if still holding
    if position_qty > 0 and len(df):
        last_price = float(df.iloc[-1]["close"])
        cash += position_qty * last_price
        trade_log.append({"exit_price": last_price, "entry_price": avg_cost, "reason": "end_of_period", "pnl": (last_price - avg_cost) * position_qty})

    # ── Metrics ──
    wins   = sum(1 for t in trade_log if t["pnl"] > 0)
    losses = sum(1 for t in trade_log if t["pnl"] <= 0)
    total_return = (cash - starting_cash) / starting_cash if starting_cash > 0 else 0.0

    # Sharpe (annualised, assumes 5-min bars → 252*78 bars/year)
    rets = []
    for i in range(1, len(equity_curve)):
        if equity_curve[i - 1] > 0:
            rets.append((equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1])
    sharpe = 0.0
    if rets:
        mean_r = sum(rets) / len(rets)
        std_r  = math.sqrt(sum((r - mean_r) ** 2 for r in rets) / len(rets)) if len(rets) > 1 else 0
        if std_r > 0:
            periods_per_year = 252 * 78
            sharpe = (mean_r / std_r) * math.sqrt(periods_per_year)

    # Max drawdown
    peak = equity_curve[0] if equity_curve else starting_cash
    max_dd = 0.0
    for e in equity_curve:
        if e > peak:
            peak = e
        dd = (peak - e) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    n = len(trade_log)
    return BacktestResult(
        symbol=symbol,
        strategy=strategy,
        start=str(df.iloc[0]["timestamp"]) if len(df) else "",
        end=str(df.iloc[-1]["timestamp"]) if len(df) else "",
        trades=n,
        wins=wins,
        losses=losses,
        total_return_pct=round(total_return * 100, 4),
        sharpe=round(sharpe, 4),
        max_drawdown_pct=round(max_dd * 100, 4),
        win_rate=round(wins / n, 4) if n > 0 else 0.0,
        trade_log=trade_log,
    )


# ── Public async API (used by FastAPI endpoint) ───────────────────────────────

async def run_backtest(
    symbol: str,
    strategy: str = "trend_riding",
    start: str = "",
    end: str = "",
    starting_cash: float = 10_000.0,
) -> BacktestResult:
    if not start:
        start = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")
    if not end:
        end = datetime.utcnow().strftime("%Y-%m-%d")

    df = await _fetch_historical_bars(symbol, start, end)
    if df.empty:
        return BacktestResult(symbol=symbol, strategy=strategy, start=start, end=end)
    df = _compute_indicators_sync(df)
    return _run_backtest_on_df(df, symbol, strategy, starting_cash=starting_cash)


async def run_parallel_backtest(
    symbols: list[str],
    strategy: str = "trend_riding",
    start: str = "",
    end: str = "",
) -> list[BacktestResult]:
    """Run backtests for multiple symbols concurrently."""
    tasks = [run_backtest(s, strategy, start, end) for s in symbols]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if isinstance(r, BacktestResult)]


# ── Grid search ───────────────────────────────────────────────────────────────

async def grid_search(
    symbol: str,
    start: str,
    end: str,
    strategies: list[str] | None = None,
) -> list[dict]:
    """
    Exhaustive strategy comparison. Returns results sorted by Sharpe ratio.
    Extend param_grid here to tune ATR multipliers, position sizing, etc.
    """
    strats = strategies or list(STRATEGIES.keys())
    tasks = [run_backtest(symbol, s, start, end) for s in strats]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out = [r.to_dict() for r in results if isinstance(r, BacktestResult)]
    out.sort(key=lambda r: r["sharpe"], reverse=True)
    return out


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Galactic Trader Backtester")
    parser.add_argument("--symbol", required=True, help="Comma-separated symbols, e.g. NVDA,AAPL")
    parser.add_argument("--start",  default="", help="Start date YYYY-MM-DD (default: 90 days ago)")
    parser.add_argument("--end",    default="", help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--strategy", default="trend_riding", help="trend_riding | mean_reversion | regime_adaptive")
    parser.add_argument("--parallel", action="store_true", help="Run multiple symbols in parallel")
    parser.add_argument("--grid",   action="store_true", help="Run grid search over all strategies")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbol.split(",") if s.strip()]

    async def _main():
        if args.grid:
            for sym in symbols:
                print(f"\n=== Grid search: {sym} ===")
                results = await grid_search(sym, args.start, args.end)
                for r in results:
                    print(r)
        elif args.parallel and len(symbols) > 1:
            results = await run_parallel_backtest(symbols, args.strategy, args.start, args.end)
            for r in results:
                print(r.to_dict())
        else:
            for sym in symbols:
                r = await run_backtest(sym, args.strategy, args.start, args.end)
                print(r.to_dict())

    asyncio.run(_main())
