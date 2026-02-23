"""
strategies/ — Three-strategy module for Galactic Trader.

Available strategies (user-selectable from dashboard):
  - DHLAOStrategy      (dhlao.py)       : Daily High/Low breakout scalper
  - ThreeMACDStrategy  (three_macd.py)  : Triple MACD momentum scalper
  - BBRSIStrategy      (bbrsi.py)       : Bollinger Bands + RSI mean-reversion

Usage:
    from strategies import DHLAOStrategy, ThreeMACDStrategy, BBRSIStrategy, Signal
    strategy = DHLAOStrategy()
    signal   = strategy.generate_signal(df, "AAPL", current_price=210.50)
"""
from .base_strategy import BaseStrategy, Signal
from .dhlao import DHLAOStrategy
from .three_macd import ThreeMACDStrategy
from .bbrsi import BBRSIStrategy

STRATEGY_MAP = {
    "DHLAO":  DHLAOStrategy,
    "3MACD":  ThreeMACDStrategy,
    "BBRSI":  BBRSIStrategy,
}

__all__ = [
    "BaseStrategy",
    "Signal",
    "DHLAOStrategy",
    "ThreeMACDStrategy",
    "BBRSIStrategy",
    "STRATEGY_MAP",
]
