"""
mean_reversion.py — Mean reversion via Bollinger Bands + RSI confirmation.

Logic:
  Long:  Price closes below lower BB  AND  RSI < 35  → expect bounce to mean
  Short: Price closes above upper BB  AND  RSI > 65  → expect fade to mean
  TP:    Middle band (SMA20)
  SL:    2×ATR beyond entry

Works best in sideways/ranging markets. The engine skips this strategy
in strong trend regimes (handled by momentum.py instead).
"""
from __future__ import annotations
import pandas as pd
from ta.volatility import BollingerBands
from ta.momentum import RSIIndicator
from .base_strategy import BaseStrategy, Signal


class MeanReversionStrategy(BaseStrategy):
    name = "mean_reversion"

    def __init__(
        self,
        bb_period: int = 20,
        bb_std: float = 2.0,
        rsi_period: int = 14,
        rsi_long_threshold: float = 35,
        rsi_short_threshold: float = 65,
        atr_sl: float = 2.0,
    ) -> None:
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.rsi_period = rsi_period
        self.rsi_long_threshold = rsi_long_threshold
        self.rsi_short_threshold = rsi_short_threshold
        self.atr_sl = atr_sl

    def generate_signal(
        self,
        df: pd.DataFrame,
        symbol: str,
        current_price: float,
        position_qty: float = 0.0,
    ) -> Signal:
        if len(df) < self.bb_period + self.rsi_period + 5:
            return Signal("hold", 0.0, None, None, "Insufficient bars for mean-reversion", self.name)

        close = df["close"]

        bb = BollingerBands(close=close, window=self.bb_period, window_dev=self.bb_std)
        upper  = float(bb.bollinger_hband().iloc[-1])
        lower  = float(bb.bollinger_lband().iloc[-1])
        middle = float(bb.bollinger_mavg().iloc[-1])
        bb_pct = float(bb.bollinger_pband().iloc[-1])  # 0=lower, 1=upper

        rsi = RSIIndicator(close=close, window=self.rsi_period).rsi()
        cur_rsi = float(rsi.iloc[-1])
        atr = self._atr(df)

        # ── EXIT long: price hit middle band (mean) ──
        if position_qty > 0 and current_price >= middle:
            return Signal("sell", 0.75, None, None,
                f"MeanRev exit long: price ${current_price:.2f} reached mid-band ${middle:.2f}", self.name)

        # ── EXIT short: price hit middle band ──
        if position_qty < 0 and current_price <= middle:
            return Signal("cover", 0.75, None, None,
                f"MeanRev cover short: price ${current_price:.2f} reached mid-band ${middle:.2f}", self.name)

        # ── ENTRY long: below lower BB + RSI oversold ──
        if position_qty == 0 and current_price < lower and cur_rsi < self.rsi_long_threshold:
            confidence = min(0.90, 0.65 + (self.rsi_long_threshold - cur_rsi) / 35)
            sl = current_price - self.atr_sl * atr
            tp = middle  # target mean
            return Signal(
                "buy", confidence, sl, tp,
                f"MeanRev long: price ${current_price:.2f} < lower_BB ${lower:.2f}, RSI={cur_rsi:.1f}",
                self.name,
            )

        # ── ENTRY short: above upper BB + RSI overbought ──
        if position_qty == 0 and current_price > upper and cur_rsi > self.rsi_short_threshold:
            confidence = min(0.90, 0.65 + (cur_rsi - self.rsi_short_threshold) / 35)
            sl = current_price + self.atr_sl * atr
            tp = middle  # target mean
            return Signal(
                "short", confidence, sl, tp,
                f"MeanRev short: price ${current_price:.2f} > upper_BB ${upper:.2f}, RSI={cur_rsi:.1f}",
                self.name,
            )

        return Signal("hold", 0.0, None, None,
            f"MeanRev: no setup. BB%={bb_pct:.2f}, RSI={cur_rsi:.1f}", self.name)
