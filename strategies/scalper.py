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
        rsi_oversold: float = 42,
        rsi_overbought: float = 58,
        ema_fast: int = 9,
        ema_slow: int = 21,
        volume_surge_multiplier: float = 1.3,
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

        ema_cross_bullish = prev_ema_f <= prev_ema_s and cur_ema_f > cur_ema_s
        ema_cross_bearish = prev_ema_f >= prev_ema_s and cur_ema_f < cur_ema_s
        ema_bullish = cur_ema_f > cur_ema_s
        ema_bearish = cur_ema_f < cur_ema_s

        # ── ENTRY long ──
        # RSI oversold is the primary trigger; EMA alignment & volume boost confidence.
        # Works in downtrends too (oversold bounce scalp).
        if position_qty == 0 and cur_rsi < self.rsi_oversold:
            ema_boost  = 0.06 if (ema_cross_bullish or ema_bullish) else 0.0
            vol_boost  = 0.04 if volume_surge else 0.0
            confidence = min(0.95, 0.66 + (self.rsi_oversold - cur_rsi) / 40 + ema_boost + vol_boost)
            sl = current_price * (1 - sl_pct)
            tp = current_price * (1 + tp_pct)
            return Signal(
                "buy", confidence, sl, tp,
                f"Scalp long: RSI={cur_rsi:.1f} oversold, ema_bull={ema_bullish}, vol_surge={volume_surge}",
                self.name,
            )

        # ── ENTRY short ──
        # RSI overbought is the primary trigger; EMA & volume boost confidence.
        if position_qty == 0 and cur_rsi > self.rsi_overbought:
            ema_boost  = 0.06 if (ema_cross_bearish or ema_bearish) else 0.0
            vol_boost  = 0.04 if volume_surge else 0.0
            confidence = min(0.95, 0.66 + (cur_rsi - self.rsi_overbought) / 40 + ema_boost + vol_boost)
            sl = current_price * (1 + sl_pct)
            tp = current_price * (1 - tp_pct)
            return Signal(
                "short", confidence, sl, tp,
                f"Scalp short: RSI={cur_rsi:.1f} overbought, ema_bear={ema_bearish}, vol_surge={volume_surge}",
                self.name,
            )

        return Signal("hold", 0.0, None, None,
            f"Scalp: no setup. RSI={cur_rsi:.1f}, ema_bull={ema_bullish}, vol_surge={volume_surge}", self.name)
