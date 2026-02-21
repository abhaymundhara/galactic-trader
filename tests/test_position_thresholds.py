import unittest

import agent


class PositionThresholdTests(unittest.TestCase):
    def test_crypto_dust_is_not_actionable(self):
        self.assertFalse(agent.is_actionable_position_qty("BTC/USD", 0.000000345))
        self.assertTrue(agent.is_actionable_position_qty("BTC/USD", 0.000001))

    def test_stock_minimum_is_one_share(self):
        self.assertFalse(agent.is_actionable_position_qty("AAPL", 0.8))
        self.assertTrue(agent.is_actionable_position_qty("AAPL", 1.0))


if __name__ == "__main__":
    unittest.main()
