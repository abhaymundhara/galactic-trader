import unittest

import agent


class ShortPositionFlowTests(unittest.TestCase):
    def test_normalize_position_qty_handles_negative_short_qty(self):
        self.assertEqual(agent._normalize_position_qty("-5", "short"), 5.0)
        self.assertEqual(agent._normalize_position_qty(-3, "short"), 3.0)

    def test_normalize_position_qty_keeps_long_positive(self):
        self.assertEqual(agent._normalize_position_qty("7", "long"), 7.0)
        self.assertEqual(agent._normalize_position_qty("-2", "long"), 0.0)

    def test_should_analyse_symbol_respects_stock_market_state(self):
        self.assertTrue(agent.should_analyse_symbol("BTC/USD", stock_market_open=False))
        self.assertFalse(agent.should_analyse_symbol("AAPL", stock_market_open=False))
        self.assertTrue(agent.should_analyse_symbol("AAPL", stock_market_open=True))

    def test_entry_market_open_ok_blocks_stock_when_closed(self):
        old_stock_open = agent.state.get("stock_market_open")
        old_any_open = agent.state.get("market_open")
        try:
            agent.state["stock_market_open"] = False
            agent.state["market_open"] = True
            ok, reason = agent.entry_market_open_ok("AAPL")
            self.assertFalse(ok)
            self.assertIn("Stock market closed", reason)

            ok_crypto, _ = agent.entry_market_open_ok("ETH/USD")
            self.assertTrue(ok_crypto)
        finally:
            agent.state["stock_market_open"] = old_stock_open
            agent.state["market_open"] = old_any_open


if __name__ == "__main__":
    unittest.main()
