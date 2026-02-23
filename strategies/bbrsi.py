"""
strategies/bbrsi.py — BBRSI (Bollinger Bands + RSI) strategy.

Logic:
  Mean-reversion with momentum filter:
  - BUY  when: price touches/crosses below lower BB AND RSI < 35 (oversold).
  - SELL when: price touches/crosses above upper BB AND RSI > 65 (overbought).
  - Squeeze filter: BB width < 1.5× 20-period average width → skip (low volatility, breakout risk).
  - Confidence: scales with RSI extremity and BB penetration depth.
  - ATR-based SL/TP.

Best on ranging/sideways markets; 5-min to 15-min bars.
"""
from __future__ import annotations
import pandas as pd
from ta.volatility import BollingerBands
from ta.momentum import RSIIndicator
from .base_strategy import BaseStrategy, Signal


class BBRSIStrategy(BaseStrategy):
    name = "BBRSI"

    BB_PERIOD  = 20
    BB_STDDEV  = 2.0
    RSI_PERIOD = 14
    RSI_OB     = 65.0
    RSI_OS     = 35.0

    def generate_signal(
        self,
        df: pd.DataFrame,
        symbol: str,
        current_price: float,
        position_qty: float = 0.0,
    ) -> Signal:
        if len(df) < self.BB_PERIOD + self.RSI_PERIOD + 10:
            return Signal("hold", 0.0, None, None, "Insufficient bars for BBRSI", self.name)

        atr = self._atr(df, 14)
        if atr == 0 or pd.isna(atr):
            return Signal("hold", 0.0, None, None, "ATR=0", self.name)

        close = df["close"]

        # Bollinger Bands
        bb = BollingerBands(close, window=self.BB_PERIOD, window_dev=self.BB_STDDEV)
        upper = float(bb.bollinger_hband().iloc[-1])
        lower = float(bb.bollinger_lband().iloc[-1])
        middle= float(bb.bollinger_mavg().iloc[-1])
        width = upper - lower

        # BB squeeze filter: is current width narrower than usual?
        recent_widths = (bb.bollinger_hband() - bb.bollinger_lband()).rolling(20).mean()
        avg_width = float(recent_widths.iloc[-1]) if not recent_widths.isna().all() else width
        in_squeeze = width < 0.8 * avg_width  # squeeze = very tight bands

        # RSI
        rsi_series = RSIIndicator(close, window=self.RSI_PERIOD).rsi()
        rsi = float(rsi_series.iloc[-1])
        if pd.isna(rsi):
            return Signal("hold", 0.0, None, None, "RSI=NaN", self.name)

        # Penetration depth (how far price is beyond the band, normalised)
        lower_pen = (lower - current_price) / max(width, 1e-9)  # positive when below lower
        upper_pen = (current_price - upper) / max(width, 1e-9)  # positive when above upper

        # ── BUY: oversold — price below lower BB + RSI OS ──
        if (
            current_price <= lower
            and rsi < self.RSI_OS
            and not in_squeeze
            and position_qty == 0
        ):
            # Confidence: deeper below band + lower RSI = higher confidence
            rsi_factor  = (self.RSI_OS - rsi) / self.RSI_OS           # 0→1
            bb_factor   = min(lower_pen * 4.0, 1.0)                    # 0→1
            confidence  = round(0.58 + 0.20 * rsi_factor + 0.17 * bb_factor, 3)
            confidence  = min(confidence, 0.95)
            sl = current_price - 1.5 * atr
            tp = middle  # mean-revert to middle BB
            return Signal(
                "buy", confidence, sl, tp,
                f"Price {current_price:.4f} below lower BB {lower:.4f} | RSI={rsi:.1f} | pen={lower_pen:.3f}",
                self.name,
            )

        # ── SELL/exit long: overbought — price above upper BB + RSI OB ──
        if current_price >= upper and rsi > self.RSI_OB:
            if position_qty > 0:
                confidence = round(0.60 + 0.20 * min((rsi - self.RSI_OB) / 35, 1.0), 3)
                return Signal(
                    "sell", confidence, None, None,
                    f"Price {current_price:.4f} above upper BB {upper:.4f} | RSI={rsi:.1f} — exit long",
                    self.name,
                )
            if position_qty == 0 and not in_squeeze:
                rsi_factor  = (rsi - self.RSI_OB) / (100 - self.RSI_OB)
                bb_factor   = min(upper_pen * 4.0, 1.0)
                confidence  = round(0.55 + 0.18 * rsi_factor + 0.17 * bb_factor, 3)
                confidence  = min(confidence, 0.90)
                sl = current_price + 1.5 * atr
                tp = middle
                return Signal(
                    "short", confidence, sl, tp,
                    f"Price {current_price:.4f} above upper BB {upper:.4f} | RSI={rsi:.1f} — short",
                    self.name,
                )

        # ── Exit short when price returns to mean ──
        if position_qty < 0 and current_price <= middle:
            return Signal(
                "cover", 0.70, None, None,
                f"Price {current_price:.4f} returned to BB middle {middle:.4f} — cover short",
                self.name,
            )

        return Signal(
            "hold", 0.0, None, None,
            f"BBRSI: price={current_price:.4f} lower={lower:.4f} upper={upper:.4f} RSI={rsi:.1f}"
            + (" [squeeze]" if in_squeeze else ""),
            self.name,
        )
