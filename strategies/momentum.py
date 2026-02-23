"""
momentum.py — Momentum / breakout strategy.

Logic:
  Entry (long):  MACD line crosses above signal  AND  volume > 2x 20-bar avg
                 AND  price breaks above 20-bar high (breakout confirmation)
  Entry (short): MACD line crosses below signal  AND  price breaks below 20-bar low
  SL/TP:         ATR-based — SL = 1.5×ATR, TP = 2.5×ATR
  Hold time:     5-30 minutes (5-min bars recommended)
"""
from __future__ import annotations
import pandas as pd
from ta.trend import MACD, EMAIndicator
from ta.momentum import RSIIndicator
from .base_strategy import BaseStrategy, Signal


class MomentumStrategy(BaseStrategy):
    name = "momentum"

    def __init__(
        self,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        volume_multiplier: float = 1.2,
        breakout_period: int = 10,
        atr_sl: float = 1.5,
        atr_tp: float = 2.5,
    ) -> None:
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.volume_multiplier = volume_multiplier
        self.breakout_period = breakout_period
        self.atr_sl = atr_sl
        self.atr_tp = atr_tp

    def generate_signal(
        self,
        df: pd.DataFrame,
        symbol: str,
        current_price: float,
        position_qty: float = 0.0,
    ) -> Signal:
        min_bars = self.macd_slow + self.macd_signal + self.breakout_period + 5
        if len(df) < min_bars:
            return Signal("hold", 0.0, None, None, "Insufficient bars for momentum", self.name)

        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        macd_ind = MACD(close=close, window_fast=self.macd_fast, window_slow=self.macd_slow, window_sign=self.macd_signal)
        macd_line = macd_ind.macd()
        macd_sig  = macd_ind.macd_signal()
        macd_hist = macd_ind.macd_diff()

        rsi = RSIIndicator(close=close, window=14).rsi()

        cur_macd, prev_macd = float(macd_line.iloc[-1]), float(macd_line.iloc[-2])
        cur_sig,  prev_sig  = float(macd_sig.iloc[-1]),  float(macd_sig.iloc[-2])

        vol_avg = float(volume.rolling(20).mean().iloc[-1])
        cur_vol = float(volume.iloc[-1])
        volume_surge = cur_vol > (vol_avg * self.volume_multiplier)

        recent_high = float(high.iloc[-self.breakout_period:-1].max())
        recent_low  = float(low.iloc[-self.breakout_period:-1].min())

        atr = self._atr(df)
        cur_rsi = float(rsi.iloc[-1])

        # ── EXIT long ──
        if position_qty > 0:
            macd_cross_down = prev_macd >= prev_sig and cur_macd < cur_sig
            if macd_cross_down or cur_rsi > 75:
                return Signal("sell", 0.78, None, None,
                    f"Momentum exit long: MACD cross-down or RSI={cur_rsi:.1f}", self.name)

        # ── EXIT short ──
        if position_qty < 0:
            macd_cross_up = prev_macd <= prev_sig and cur_macd > cur_sig
            if macd_cross_up or cur_rsi < 25:
                return Signal("cover", 0.78, None, None,
                    f"Momentum cover short: MACD cross-up or RSI={cur_rsi:.1f}", self.name)

        # ── ENTRY long ──
        macd_cross_bullish = prev_macd <= prev_sig and cur_macd > cur_sig
        price_breakout_up  = current_price > recent_high
        if position_qty == 0 and macd_cross_bullish and (volume_surge or price_breakout_up):
            conf_boost = max(0.0, (cur_vol / vol_avg - self.volume_multiplier) * 0.05)
            confidence = min(0.92, 0.68 + conf_boost + (0.04 if price_breakout_up else 0.0))
            sl = current_price - self.atr_sl * atr
            tp = current_price + self.atr_tp * atr
            return Signal(
                "buy", confidence, sl, tp,
                f"Momentum long: MACD cross↑, vol={cur_vol/vol_avg:.1f}x, breakout={price_breakout_up}",
                self.name,
            )

        # ── ENTRY short ──
        macd_cross_bearish = prev_macd >= prev_sig and cur_macd < cur_sig
        price_breakout_dn  = current_price < recent_low
        if position_qty == 0 and macd_cross_bearish and (volume_surge or price_breakout_dn):
            conf_boost = max(0.0, (cur_vol / vol_avg - self.volume_multiplier) * 0.05)
            confidence = min(0.92, 0.68 + conf_boost + (0.04 if price_breakout_dn else 0.0))
            sl = current_price + self.atr_sl * atr
            tp = current_price - self.atr_tp * atr
            return Signal(
                "short", confidence, sl, tp,
                f"Momentum short: MACD cross↓, vol={cur_vol/vol_avg:.1f}x, breakdown={price_breakout_dn}",
                self.name,
            )

        hist_val = float(macd_hist.iloc[-1])
        return Signal("hold", 0.0, None, None,
            f"Momentum: no setup. MACD_hist={hist_val:.4f}, vol_surge={volume_surge}", self.name)
