"""
scalper.py — High-frequency scalping strategy.

Logic:
  Entry (long):  RSI < 32  AND  EMA9 crosses above EMA21  AND  volume surge
  Entry (short): RSI > 68  AND  EMA9 crosses below EMA21  AND  volume surge
  Exit:          TP = +0.35 %  SL = -0.50 %  (ATR-adjusted when volatile)

Designed for 1-minute bars on liquid symbols: AAPL, TSLA, SPY, NVDA, MSFT.
Takes many small trades — target 0.2-0.4% per trade.
"""
from __future__ import annotations
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from .base_strategy import BaseStrategy, Signal


class ScalpingStrategy(BaseStrategy):
    name = "scalping"

    def __init__(
        self,
        rsi_period: int = 14,
        rsi_oversold: float = 32,
        rsi_overbought: float = 68,
        ema_fast: int = 9,
        ema_slow: int = 21,
        volume_surge_multiplier: float = 1.8,
        tp_pct: float = 0.0035,   # 0.35 %
        sl_pct: float = 0.0050,   # 0.50 %
    ) -> None:
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.volume_surge_multiplier = volume_surge_multiplier
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct

    def generate_signal(
        self,
        df: pd.DataFrame,
        symbol: str,
        current_price: float,
        position_qty: float = 0.0,
    ) -> Signal:
        if len(df) < max(self.ema_slow, self.rsi_period) + 3:
            return Signal("hold", 0.0, None, None, "Insufficient bars for scalping", self.name)

        close = df["close"]
        volume = df["volume"]

        rsi = RSIIndicator(close=close, window=self.rsi_period).rsi()
        ema_f = EMAIndicator(close=close, window=self.ema_fast).ema_indicator()
        ema_s = EMAIndicator(close=close, window=self.ema_slow).ema_indicator()

        cur_rsi = float(rsi.iloc[-1])
        prev_ema_f, cur_ema_f = float(ema_f.iloc[-2]), float(ema_f.iloc[-1])
        prev_ema_s, cur_ema_s = float(ema_s.iloc[-2]), float(ema_s.iloc[-1])

        vol_avg = float(volume.rolling(20).mean().iloc[-1])
        cur_vol = float(volume.iloc[-1])
        volume_surge = cur_vol > (vol_avg * self.volume_surge_multiplier)

        # ATR-based SL/TP scaling
        atr = self._atr(df, 14)
        atr_pct = atr / current_price if current_price > 0 else 0.002
        sl_pct = max(self.sl_pct, atr_pct * 1.0)
        tp_pct = max(self.tp_pct, atr_pct * 0.7)

        # ── EXIT existing long ──
        if position_qty > 0 and cur_rsi > 60:
            return Signal(
                "sell", 0.80, None, None,
                f"Scalp exit long: RSI={cur_rsi:.1f} (overbought territory)",
                self.name,
            )

        # ── EXIT existing short ──
        if position_qty < 0 and cur_rsi < 40:
            return Signal(
                "cover", 0.80, None, None,
                f"Scalp cover short: RSI={cur_rsi:.1f} (oversold territory)",
                self.name,
            )

        # ── ENTRY long ──
        ema_cross_bullish = prev_ema_f <= prev_ema_s and cur_ema_f > cur_ema_s
        if position_qty == 0 and cur_rsi < self.rsi_oversold and (ema_cross_bullish or cur_ema_f > cur_ema_s) and volume_surge:
            confidence = min(0.95, 0.65 + (self.rsi_oversold - cur_rsi) / 30)
            sl = current_price * (1 - sl_pct)
            tp = current_price * (1 + tp_pct)
            return Signal(
                "buy", confidence, sl, tp,
                f"Scalp long: RSI={cur_rsi:.1f} oversold, EMA{self.ema_fast}>{self.ema_slow}, vol_surge={volume_surge}",
                self.name,
            )

        # ── ENTRY short ──
        ema_cross_bearish = prev_ema_f >= prev_ema_s and cur_ema_f < cur_ema_s
        if position_qty == 0 and cur_rsi > self.rsi_overbought and (ema_cross_bearish or cur_ema_f < cur_ema_s) and volume_surge:
            confidence = min(0.95, 0.65 + (cur_rsi - self.rsi_overbought) / 30)
            sl = current_price * (1 + sl_pct)
            tp = current_price * (1 - tp_pct)
            return Signal(
                "short", confidence, sl, tp,
                f"Scalp short: RSI={cur_rsi:.1f} overbought, EMA{self.ema_fast}<{self.ema_slow}, vol_surge={volume_surge}",
                self.name,
            )

        return Signal("hold", 0.0, None, None, f"Scalp: no setup. RSI={cur_rsi:.1f}", self.name)
