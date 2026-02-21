"""4-regime market detector: BULL, BEAR, SIDEWAYS, VOLATILE."""
from enum import Enum


class Regime(str, Enum):
    BULL     = "bull"
    BEAR     = "bear"
    SIDEWAYS = "sideways"
    VOLATILE = "volatile"


# Prometheus label mapping (int for Gauge)
REGIME_TO_INT = {
    Regime.SIDEWAYS: 0,
    Regime.BULL:     1,
    Regime.BEAR:     2,
    Regime.VOLATILE: 3,
}


def detect_regime(indicators: dict, higher_tf: dict | None = None) -> Regime:
    """
    Classify market regime from a computed indicator dict (output of compute_indicators).

    Rules (in priority order):
      VOLATILE  — ATR/price > 0.03  OR  BB width > 0.08         (high intraday range)
      BULL      — ADX > 20, trending up: EMA9 > EMA21, MACD > signal
      BEAR      — ADX > 20, trending down: EMA9 < EMA21, MACD < signal
      SIDEWAYS  — everything else (low ADX, narrow BB, no directional bias)

    Higher-timeframe confirmation (optional):
      If higher_tf is provided, require h_cross to match for BULL/BEAR label.
    """
    adx      = float(indicators.get("adx",      0) or 0)
    bb_width = float(indicators.get("bb_width", 0) or 0)
    atr      = float(indicators.get("atr",      0) or 0)
    price    = float(indicators.get("price",    1) or 1)
    macd_val = float(indicators.get("macd",     0) or 0)
    macd_sig = float(indicators.get("macd_signal", 0) or 0)
    ema_cross = indicators.get("ema_cross", "unknown")

    # Higher-TF cross confirmation
    h_cross = (higher_tf or {}).get("ema_cross", ema_cross)

    # 1. Volatility check
    atr_pct = atr / price if price > 0 else 0
    if atr_pct > 0.03 or bb_width > 0.08:
        return Regime.VOLATILE

    # 2. Directional trend check (ADX threshold lowered slightly from rustrade's 25 to 18
    #    to be more sensitive on 5-min bars used by galactic-trader)
    if adx >= 18:
        bullish = ema_cross == "bullish" and h_cross == "bullish" and macd_val > macd_sig
        bearish = ema_cross == "bearish" and h_cross == "bearish" and macd_val < macd_sig
        if bullish:
            return Regime.BULL
        if bearish:
            return Regime.BEAR

    # 3. Default: low conviction, mean-revert territory
    return Regime.SIDEWAYS


# ── Strategy selector ────────────────────────────────────────────────────────

REGIME_STRATEGY_MAP: dict[Regime, str] = {
    Regime.BULL:     "trend_riding",       # follow momentum
    Regime.BEAR:     "mean_reversion",     # fade over-extended moves short-side
    Regime.SIDEWAYS: "mean_reversion",     # BB + RSI oscillation
    Regime.VOLATILE: "hold",               # sit out — too noisy for reliable signals
}


def strategy_for_regime(regime: Regime) -> str:
    """Return the preferred strategy name for a given regime."""
    return REGIME_STRATEGY_MAP[regime]
