"""
strategy_engine.py — Multi-strategy execution engine for Galactic Trader.

Runs all registered strategies on each symbol each cycle and returns the
highest-confidence actionable signal. Integrates with the existing agent.py
loop via `run_strategies()`.

Usage in agent.py:
    from strategy_engine import run_strategies, build_ohlcv_df

    df = build_ohlcv_df(bars)   # convert Alpaca bar dicts to DataFrame
    signal = await run_strategies(symbol, df, current_price, position_qty)
    if signal and signal.confidence >= 0.65:
        # use signal.action, signal.stop_loss, signal.take_profit
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import pandas as pd

from strategies import (
    ScalpingStrategy,
    MomentumStrategy,
    MeanReversionStrategy,
    Signal,
)
from strategies.pairs_trading import PairsTradingStrategy, DEFAULT_PAIRS, PairsSignal

logger = logging.getLogger("strategy_engine")

# ─── Strategy instances (one per type, stateless) ────────────────────────────
_scalper   = ScalpingStrategy()
_momentum  = MomentumStrategy()
_meanrev   = MeanReversionStrategy()
_pairs     = PairsTradingStrategy()

# Symbols best suited for each strategy
SCALP_SYMBOLS    = {"AAPL", "TSLA", "NVDA", "MSFT", "SPY", "QQQ", "AMD", "META"}
MOMENTUM_SYMBOLS = {"AAPL", "TSLA", "NVDA", "MSFT", "SPY", "QQQ", "AMD", "META",
                    "GOOGL", "AMZN", "BTC/USD", "ETH/USD"}
MEANREV_SYMBOLS  = {"SPY", "QQQ", "GLD", "SLV", "AAPL", "MSFT", "XOM", "CVX"}


def build_ohlcv_df(bars: list[dict[str, Any]]) -> pd.DataFrame:
    """
    Convert a list of Alpaca bar dicts into a normalised OHLCV DataFrame.
    Each bar dict should have keys: t (timestamp), o, h, l, c, v.
    """
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
    Run all applicable strategies for a symbol and return the
    highest-confidence non-hold signal, or None.

    Strategies are selected based on symbol; signals are ranked by confidence.
    """
    if df.empty or len(df) < 30:
        logger.debug(f"{symbol}: too few bars ({len(df)}) for strategy engine")
        return None

    candidates: list[Signal] = []

    sym_upper = symbol.upper()

    # ── Scalping (liquid high-vol stocks, 1-min bars) ──
    if sym_upper in SCALP_SYMBOLS:
        try:
            sig = _scalper.generate_signal(df, symbol, current_price, position_qty)
            if sig.action != "hold":
                candidates.append(sig)
        except Exception as e:
            logger.warning(f"ScalpingStrategy error on {symbol}: {e}")

    # ── Momentum (broad universe, 5-min bars) ──
    if sym_upper in MOMENTUM_SYMBOLS:
        try:
            sig = _momentum.generate_signal(df, symbol, current_price, position_qty)
            if sig.action != "hold":
                candidates.append(sig)
        except Exception as e:
            logger.warning(f"MomentumStrategy error on {symbol}: {e}")

    # ── Mean Reversion (ETFs, gold, stable equities) ──
    if sym_upper in MEANREV_SYMBOLS:
        try:
            sig = _meanrev.generate_signal(df, symbol, current_price, position_qty)
            if sig.action != "hold":
                candidates.append(sig)
        except Exception as e:
            logger.warning(f"MeanReversionStrategy error on {symbol}: {e}")

    if not candidates:
        return None

    # Return the highest-confidence signal
    best = max(candidates, key=lambda s: s.confidence)
    logger.info(
        f"{symbol} → [{best.strategy_name}] {best.action.upper()} "
        f"conf={best.confidence:.2f} | {best.reasoning}"
    )
    return best


def run_pairs_strategies(
    symbol_data: dict[str, tuple[pd.DataFrame, float, float]],
) -> list[PairsSignal]:
    """
    Run pairs trading across DEFAULT_PAIRS.

    Args:
        symbol_data: { symbol: (df, current_price, position_qty) }

    Returns:
        List of PairsSignal for all pairs with active signals.
    """
    results: list[PairsSignal] = []
    for sym_a, sym_b in DEFAULT_PAIRS:
        if sym_a not in symbol_data or sym_b not in symbol_data:
            continue
        df_a, price_a, pos_a = symbol_data[sym_a]
        df_b, price_b, pos_b = symbol_data[sym_b]

        if len(df_a) < 65 or len(df_b) < 65:
            continue

        try:
            sigs = _pairs.generate_signals(
                df_a, df_b, sym_a, sym_b, price_a, price_b, pos_a, pos_b
            )
            results.extend(sigs)
        except Exception as e:
            logger.warning(f"PairsTradingStrategy error on {sym_a}/{sym_b}: {e}")

    return results
