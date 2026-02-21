import unittest

import agent


class EntryFilterTests(unittest.TestCase):
    def test_crypto_missing_volume_does_not_auto_block_buy(self):
        indicators = {
            "price": 101.0,
            "vwap": 100.0,
            "volume_ratio": 0.0,
            "volume_reliable": False,
            "rsi": 52.0,
            "macd": 1.2,
            "macd_signal": 1.0,
            "adx": 14.0,
            "ema_cross": "bullish",
        }
        higher_tf = {"ema_cross": "bearish"}

        ok, reason = agent.buy_filters_ok("ETH/USD", indicators, higher_tf)

        self.assertTrue(ok, msg=reason)

    def test_stock_low_volume_still_blocks_buy(self):
        indicators = {
            "price": 101.0,
            "vwap": 100.0,
            "volume_ratio": 0.4,
            "volume_reliable": True,
            "rsi": 52.0,
            "macd": 1.2,
            "macd_signal": 1.0,
            "adx": 20.0,
            "ema_cross": "bullish",
        }
        higher_tf = {"ema_cross": "bullish"}

        ok, reason = agent.buy_filters_ok("AAPL", indicators, higher_tf)

        self.assertFalse(ok)
        self.assertIn("low volume ratio", reason)

    def test_range_regime_allows_momentum_buy(self):
        indicators = {
            "rsi": 49.0,
            "macd": 0.8,
            "macd_signal": 0.4,
            "ema_cross": "bullish",
            "price": 101.0,
            "vwap": 100.0,
        }

        ok, reason = agent.long_regime_gate_ok(indicators, regime="range")

        self.assertTrue(ok, msg=reason)


if __name__ == "__main__":
    unittest.main()
