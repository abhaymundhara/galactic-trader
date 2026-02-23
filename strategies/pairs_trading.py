"""
pairs_trading.py — Statistical arbitrage on correlated pairs.

Finds correlated pairs in a universe, computes the spread z-score,
and trades when z-score diverges beyond a threshold.

Pairs are traded as:
  z > +2.0  → Short leg A, Long leg B  (spread will revert down)
  z < -2.0  → Long leg A, Short leg B  (spread will revert up)
  z crosses 0 → exit both legs

Default pairs: (AAPL/MSFT), (XOM/CVX), (GLD/SLV)
Can also run on any two correlated symbols passed at runtime.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Literal

# Default curated pairs (symbol_a, symbol_b, correlation_note)
DEFAULT_PAIRS: list[tuple[str, str]] = [
    ("AAPL", "MSFT"),   # Big tech, r ≈ 0.85
    ("XOM",  "CVX"),    # Oil majors, r ≈ 0.90
    ("GLD",  "SLV"),    # Precious metals, r ≈ 0.80
    ("META", "GOOGL"),  # Ad-tech, r ≈ 0.80
    ("JPM",  "BAC"),    # US banks, r ≈ 0.85
]

Action = Literal["buy", "sell", "short", "cover", "hold"]


@dataclass
class PairsSignal:
    """Signal for a single leg of a pairs trade."""
    symbol: str
    action: Action
    confidence: float
    stop_loss: float | None
    take_profit: float | None
    reasoning: str
    strategy_name: str = "pairs_trading"
    pair_id: str = ""


class PairsTradingStrategy:
    name = "pairs_trading"

    def __init__(
        self,
        z_entry: float = 2.0,
        z_exit: float = 0.5,
        lookback: int = 60,       # bars for z-score calculation
        atr_sl: float = 2.0,
    ) -> None:
        self.z_entry = z_entry
        self.z_exit = z_exit
        self.lookback = lookback
        self.atr_sl = atr_sl

    def compute_spread(
        self,
        close_a: pd.Series,
        close_b: pd.Series,
        lookback: int | None = None,
    ) -> tuple[float, float, float]:
        """
        Returns (spread_value, z_score, hedge_ratio).
        Uses OLS to find the cointegration hedge ratio.
        """
        lb = lookback or self.lookback
        if len(close_a) < lb or len(close_b) < lb:
            return 0.0, 0.0, 1.0

        a = close_a.iloc[-lb:].values
        b = close_b.iloc[-lb:].values

        # OLS hedge ratio: price_a = beta * price_b + alpha
        X = np.column_stack([b, np.ones(len(b))])
        beta, alpha = np.linalg.lstsq(X, a, rcond=None)[0]
        beta = float(max(0.1, beta))  # keep positive

        spread = a - (beta * b + alpha)
        z = (spread[-1] - spread.mean()) / (spread.std() + 1e-9)
        return float(spread[-1]), float(z), beta

    def generate_signals(
        self,
        df_a: pd.DataFrame,
        df_b: pd.DataFrame,
        symbol_a: str,
        symbol_b: str,
        price_a: float,
        price_b: float,
        position_a: float = 0.0,
        position_b: float = 0.0,
    ) -> list[PairsSignal]:
        """
        Generate leg signals for a pairs trade.
        Returns a list of 0, 1, or 2 PairsSignal objects.
        """
        pair_id = f"{symbol_a}/{symbol_b}"

        spread_val, z, hedge_ratio = self.compute_spread(df_a["close"], df_b["close"])

        signals: list[PairsSignal] = []

        in_long_a  = position_a > 0 and position_b < 0
        in_short_a = position_a < 0 and position_b > 0
        in_trade   = in_long_a or in_short_a

        # ATR for SL
        def _atr(df: pd.DataFrame, period: int = 14) -> float:
            high, low, close = df["high"], df["low"], df["close"]
            tr = pd.concat([
                high - low,
                (high - close.shift()).abs(),
                (low  - close.shift()).abs(),
            ], axis=1).max(axis=1)
            return float(tr.rolling(period).mean().iloc[-1])

        atr_a = _atr(df_a)
        atr_b = _atr(df_b)

        # ── EXIT: z converges ──
        if in_long_a and z > -self.z_exit:
            signals += [
                PairsSignal(symbol_a, "sell",  0.78, None, None, f"Pairs exit: z={z:.2f} converging", pair_id=pair_id),
                PairsSignal(symbol_b, "cover", 0.78, None, None, f"Pairs exit: z={z:.2f} converging", pair_id=pair_id),
            ]
        elif in_short_a and z < self.z_exit:
            signals += [
                PairsSignal(symbol_a, "cover", 0.78, None, None, f"Pairs exit: z={z:.2f} converging", pair_id=pair_id),
                PairsSignal(symbol_b, "sell",  0.78, None, None, f"Pairs exit: z={z:.2f} converging", pair_id=pair_id),
            ]

        # ── ENTRY: spread diverges ──
        elif not in_trade:
            confidence = min(0.90, 0.65 + (abs(z) - self.z_entry) * 0.05)

            if z > self.z_entry:
                # A is overpriced vs B → short A, long B
                signals += [
                    PairsSignal(symbol_a, "short", confidence,
                        price_a + self.atr_sl * atr_a, price_a - 2.5 * atr_a,
                        f"Pairs: z={z:.2f}↑ short {symbol_a}, long {symbol_b} (β={hedge_ratio:.2f})", pair_id=pair_id),
                    PairsSignal(symbol_b, "buy",   confidence,
                        price_b - self.atr_sl * atr_b, price_b + 2.5 * atr_b,
                        f"Pairs: z={z:.2f}↑ short {symbol_a}, long {symbol_b} (β={hedge_ratio:.2f})", pair_id=pair_id),
                ]
            elif z < -self.z_entry:
                # B is overpriced vs A → long A, short B
                signals += [
                    PairsSignal(symbol_a, "buy",   confidence,
                        price_a - self.atr_sl * atr_a, price_a + 2.5 * atr_a,
                        f"Pairs: z={z:.2f}↓ long {symbol_a}, short {symbol_b} (β={hedge_ratio:.2f})", pair_id=pair_id),
                    PairsSignal(symbol_b, "short", confidence,
                        price_b + self.atr_sl * atr_b, price_b - 2.5 * atr_b,
                        f"Pairs: z={z:.2f}↓ long {symbol_a}, short {symbol_b} (β={hedge_ratio:.2f})", pair_id=pair_id),
                ]

        return signals
