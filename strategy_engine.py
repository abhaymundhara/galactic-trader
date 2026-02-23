"""
strategy_engine.py — Single-strategy execution engine for Galactic Trader.

Only three strategies are supported: DHLAO, 3MACD, BBRSI.
The active strategy is selected from the dashboard and stored in `active_strategy`.
Switching is live — next tick will use the new strategy with no restart required.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import pandas as pd

from strategies import DHLAOStrategy, ThreeMACDStrategy, BBRSIStrategy, Signal, STRATEGY_MAP

logger = logging.getLogger("strategy_engine")

# ── Active strategy (mutable, changed via /api/strategy) ─────────────────────
_instances = {
    "DHLAO": DHLAOStrategy(),
    "3MACD": ThreeMACDStrategy(),
    "BBRSI": BBRSIStrategy(),
}

# Default strategy on startup
active_strategy: str = "BBRSI"


def set_active_strategy(name: str) -> bool:
    """Switch the active strategy. Returns True if valid, False otherwise."""
    global active_strategy
    if name not in _instances:
        return False
    active_strategy = name
    logger.info(f"Strategy switched to {name}")
    return True


def get_active_strategy() -> str:
    return active_strategy


def build_ohlcv_df(bars: list[dict[str, Any]]) -> pd.DataFrame:
    """Convert a list of Alpaca bar dicts into a normalised OHLCV DataFrame."""
    if not bars:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame([{
        "timestamp": b.get("t", ""),
        "open":   float(b.get("o", b.get("open",  0))),
        "high":   float(b.get("h", b.get("high",  0))),
        "low":    float(b.get("l", b.get("low",   0))),
        "close":  float(b.get("c", b.get("close", 0))),
        "volume": float(b.get("v", b.get("volume",0))),
    } for b in bars])

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp"]).set_index("timestamp").sort_index()
    return df


def run_strategies(
    symbol: str,
    df: pd.DataFrame,
    current_price: float,
    position_qty: float = 0.0,
) -> Signal | None:
    """
    Run the currently active strategy for the given symbol.
    Returns a Signal if confidence >= 0.55, else None.
    """
    if df.empty or len(df) < 20:
        logger.debug(f"{symbol}: too few bars ({len(df)}) for strategy engine")
        return None

    strategy = _instances[active_strategy]
    try:
        sig = strategy.generate_signal(df, symbol, current_price, position_qty)
    except Exception as e:
        logger.warning(f"{active_strategy} error on {symbol}: {e}")
        return None

    if sig.action == "hold" or sig.confidence < 0.55:
        return None

    logger.info(
        f"{symbol} [{active_strategy}] {sig.action.upper()} "
        f"conf={sig.confidence:.2f} | {sig.reasoning}"
    )
    return sig
