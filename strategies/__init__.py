"""
strategies/ — Pluggable strategy module for Galactic Trader.

Available strategies:
  - ScalpingStrategy    (scalper.py)       : RSI + EMA cross, 1-min bars, high frequency
  - MomentumStrategy    (momentum.py)      : MACD breakout + volume surge, 5-min bars
  - MeanReversionStrategy (mean_reversion.py): Bollinger Band breach + RSI, ranging markets
  - PairsTradingStrategy  (pairs_trading.py) : Statistical arbitrage on correlated pairs

Usage:
    from strategies import ScalpingStrategy, MomentumStrategy, MeanReversionStrategy
    from strategies.pairs_trading import PairsTradingStrategy, DEFAULT_PAIRS

    scalper  = ScalpingStrategy()
    signal   = scalper.generate_signal(df, "AAPL", current_price=210.50)
    print(signal.action, signal.confidence, signal.reasoning)
"""
from .base_strategy import BaseStrategy, Signal
from .scalper import ScalpingStrategy
from .momentum import MomentumStrategy
from .mean_reversion import MeanReversionStrategy
from .pairs_trading import PairsTradingStrategy, DEFAULT_PAIRS

__all__ = [
    "BaseStrategy",
    "Signal",
    "ScalpingStrategy",
    "MomentumStrategy",
    "MeanReversionStrategy",
    "PairsTradingStrategy",
    "DEFAULT_PAIRS",
]
