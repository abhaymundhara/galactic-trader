"""
strategies/dhlao.py — DHLAO (Daily High/Low Algo) strategy.

Logic:
  - Uses the previous day's high and low as key structural levels.
  - BUY  when price breaks above prev-day high with volume confirmation.
  - SELL when price breaks below prev-day low with volume confirmation.
  - ATR filter: breakout size must exceed 0.3× ATR to avoid fakeouts.
  - Stop loss: below prev-day high (long) / above prev-day low (short).
  - Take profit: 2× ATR from entry.

High-frequency scalping style — fires on 5-min bars.
"""
from __future__ import annotations
import pandas as pd
from .base_strategy import BaseStrategy, Signal


class DHLAOStrategy(BaseStrategy):
    name = "DHLAO"

    def generate_signal(
        self,
        df: pd.DataFrame,
        symbol: str,
        current_price: float,
        position_qty: float = 0.0,
    ) -> Signal:
        if len(df) < 50:
            return Signal("hold", 0.0, None, None, "Insufficient bars", self.name)

        atr = self._atr(df, 14)
        if atr == 0 or pd.isna(atr):
            return Signal("hold", 0.0, None, None, "ATR=0", self.name)

        # Identify previous session's high/low
        # Group by calendar date and pick the penultimate day
        if hasattr(df.index, 'date'):
            df = df.copy()
            df["_date"] = df.index.date
            days = sorted(df["_date"].unique())
        else:
            return Signal("hold", 0.0, None, None, "No datetime index", self.name)

        if len(days) < 2:
            return Signal("hold", 0.0, None, None, "Need 2+ days", self.name)

        prev_day = days[-2]
        prev_bars = df[df["_date"] == prev_day]
        prev_high = float(prev_bars["high"].max())
        prev_low  = float(prev_bars["low"].min())

        today_bars = df[df["_date"] == days[-1]]
        if today_bars.empty:
            return Signal("hold", 0.0, None, None, "No today bars", self.name)

        # Volume: today avg vs prev-day avg
        today_vol   = float(today_bars["volume"].mean())
        prev_vol    = float(prev_bars["volume"].mean())
        vol_ratio   = today_vol / max(prev_vol, 1)

        last_close  = float(df["close"].iloc[-1])
        last_open   = float(df["open"].iloc[-1])
        candle_body = abs(last_close - last_open)

        # ── BUY signal: breakout above prev-day high ──
        if (
            current_price > prev_high
            and (current_price - prev_high) > 0.3 * atr
            and vol_ratio > 1.1
            and position_qty == 0
        ):
            confidence = min(0.95, 0.60 + (vol_ratio - 1.0) * 0.15 + candle_body / atr * 0.1)
            sl = prev_high - 0.5 * atr
            tp = current_price + 2.0 * atr
            return Signal(
                "buy", round(confidence, 3), sl, tp,
                f"Breakout above prev-H {prev_high:.2f} | vol_ratio={vol_ratio:.2f} | ATR={atr:.4f}",
                self.name,
            )

        # ── SELL/exit signal: break below prev-day low ──
        if (
            current_price < prev_low
            and (prev_low - current_price) > 0.3 * atr
            and vol_ratio > 1.1
        ):
            if position_qty > 0:
                confidence = min(0.95, 0.60 + (vol_ratio - 1.0) * 0.15)
                return Signal(
                    "sell", round(confidence, 3), None, None,
                    f"Break below prev-L {prev_low:.2f} — exit long | vol_ratio={vol_ratio:.2f}",
                    self.name,
                )
            if position_qty == 0:
                confidence = min(0.90, 0.55 + (vol_ratio - 1.0) * 0.15)
                sl = prev_low + 0.5 * atr
                tp = current_price - 2.0 * atr
                return Signal(
                    "short", round(confidence, 3), sl, tp,
                    f"Short below prev-L {prev_low:.2f} | vol_ratio={vol_ratio:.2f}",
                    self.name,
                )

        # ── Mean-reversion inside day (rejection at levels) ──
        near_prev_high = abs(current_price - prev_high) < 0.2 * atr
        near_prev_low  = abs(current_price - prev_low)  < 0.2 * atr

        if near_prev_high and last_close < last_open and position_qty > 0:
            return Signal(
                "sell", 0.65, None, None,
                f"Rejection at prev-H {prev_high:.2f} — exit long",
                self.name,
            )

        if near_prev_low and last_close > last_open and position_qty <= 0:
            sl = prev_low - 0.5 * atr
            tp = current_price + 1.5 * atr
            return Signal(
                "buy", 0.62, sl, tp,
                f"Bounce off prev-L {prev_low:.2f}",
                self.name,
            )

        return Signal("hold", 0.0, None, None,
                      f"No DHLAO trigger | pH={prev_high:.2f} pL={prev_low:.2f}",
                      self.name)
