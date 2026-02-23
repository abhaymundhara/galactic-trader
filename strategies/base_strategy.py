"""
base_strategy.py — Abstract base for all Galactic Trader strategies.

Every strategy:
  - receives a DataFrame of OHLCV bars
  - returns a Signal (action, confidence, sl, tp, reasoning)
  - has no side effects (order submission is done by the engine)
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal
import pandas as pd

Action = Literal["buy", "sell", "short", "cover", "hold"]


@dataclass
class Signal:
    action: Action
    confidence: float          # 0.0 – 1.0
    stop_loss: float | None    # absolute price
    take_profit: float | None  # absolute price
    reasoning: str
    strategy_name: str


class BaseStrategy(ABC):
    """All strategies inherit from this. Implement `generate_signal`."""

    name: str = "base"

    @abstractmethod
    def generate_signal(
        self,
        df: pd.DataFrame,
        symbol: str,
        current_price: float,
        position_qty: float = 0.0,
    ) -> Signal:
        """
        Args:
            df: OHLCV DataFrame with columns: open, high, low, close, volume.
                Index is DatetimeIndex, most-recent row is last.
            symbol: Ticker string.
            current_price: Latest trade price.
            position_qty: Current held qty (positive = long, negative = short, 0 = flat).

        Returns:
            Signal dataclass.
        """
        ...

    def _atr(self, df: pd.DataFrame, period: int = 14) -> float:
        """Average True Range — used for dynamic SL/TP."""
        high = df["high"]
        low = df["low"]
        close = df["close"]
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        return float(tr.rolling(period).mean().iloc[-1])
