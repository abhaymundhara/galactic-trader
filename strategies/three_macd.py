"""
strategies/three_macd.py — Triple MACD (3MACD) strategy.

Logic:
  Three MACD configurations vote on direction:
    Fast  : EMA(5,13,1)   — micro momentum
    Mid   : EMA(12,26,9)  — standard MACD
    Slow  : EMA(21,55,9)  — swing confirmation

  Signal rules:
    - BUY  when ≥2 of 3 MACDs have histogram > 0 AND histogram is rising (momentum building).
    - SELL when ≥2 of 3 MACDs have histogram < 0 AND histogram is falling.
    - Confidence scales with how many MACDs agree and the magnitude of histogram change.
    - ATR-based SL/TP.

High-frequency scalper; best on 1-min to 5-min bars.
"""
from __future__ import annotations
import pandas as pd
from ta.trend import MACD as TaMACD
from .base_strategy import BaseStrategy, Signal


def _macd_signal(df: pd.DataFrame, fast: int, slow: int, signal: int) -> tuple[float, float]:
    """Returns (latest_histogram, histogram_change) or (0,0) if not enough data."""
    if len(df) < slow + signal + 5:
        return 0.0, 0.0
    macd = TaMACD(df["close"], window_fast=fast, window_slow=slow, window_sign=signal)
    hist = macd.macd_diff()
    if hist.isna().all():
        return 0.0, 0.0
    latest = float(hist.iloc[-1])
    prev   = float(hist.iloc[-2]) if len(hist) >= 2 else latest
    return latest, latest - prev


class ThreeMACDStrategy(BaseStrategy):
    name = "3MACD"

    # Three MACD configs: (fast, slow, signal)
    CONFIGS = [
        (5,  13,  1),   # fast scalp
        (12, 26,  9),   # classic
        (21, 55,  9),   # swing
    ]

    def generate_signal(
        self,
        df: pd.DataFrame,
        symbol: str,
        current_price: float,
        position_qty: float = 0.0,
    ) -> Signal:
        if len(df) < 70:
            return Signal("hold", 0.0, None, None, "Insufficient bars for 3MACD", self.name)

        atr = self._atr(df, 14)
        if atr == 0 or pd.isna(atr):
            return Signal("hold", 0.0, None, None, "ATR=0", self.name)

        hists = []
        changes = []
        for fast, slow, sig in self.CONFIGS:
            h, ch = _macd_signal(df, fast, slow, sig)
            hists.append(h)
            changes.append(ch)

        bulls = sum(1 for h, ch in zip(hists, changes) if h > 0 and ch > 0)
        bears = sum(1 for h, ch in zip(hists, changes) if h < 0 and ch < 0)

        # Magnitude: avg absolute histogram across bullish configs
        bull_mag = sum(abs(h) for h, ch in zip(hists, changes) if h > 0 and ch > 0)
        bear_mag = sum(abs(h) for h, ch in zip(hists, changes) if h < 0 and ch < 0)

        # Normalise magnitude by ATR
        norm = lambda m: min(1.0, m / max(atr * 0.5, 1e-9))

        if bulls >= 2 and position_qty == 0:
            confidence = round(0.55 + 0.10 * bulls + 0.15 * norm(bull_mag), 3)
            confidence = min(confidence, 0.95)
            sl = current_price - 1.5 * atr
            tp = current_price + 2.5 * atr
            reasons = [f"MACD{i+1}(h={h:.4f},Δ={c:.4f})" for i,(h,c) in enumerate(zip(hists,changes))]
            return Signal(
                "buy", confidence, sl, tp,
                f"{bulls}/3 MACDs bullish | {' | '.join(reasons)}",
                self.name,
            )

        if bears >= 2 and position_qty > 0:
            confidence = round(0.55 + 0.10 * bears + 0.15 * norm(bear_mag), 3)
            confidence = min(confidence, 0.95)
            return Signal(
                "sell", confidence, None, None,
                f"{bears}/3 MACDs bearish — exit long",
                self.name,
            )

        if bears >= 2 and position_qty == 0:
            confidence = round(0.50 + 0.10 * bears + 0.15 * norm(bear_mag), 3)
            confidence = min(confidence, 0.90)
            sl = current_price + 1.5 * atr
            tp = current_price - 2.5 * atr
            return Signal(
                "short", confidence, sl, tp,
                f"{bears}/3 MACDs bearish | short entry",
                self.name,
            )

        reasons = [f"h={h:.4f}" for h in hists]
        return Signal("hold", 0.0, None, None,
                      f"3MACD no majority | {' '.join(reasons)}", self.name)
