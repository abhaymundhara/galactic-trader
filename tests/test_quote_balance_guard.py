import unittest

import agent


class QuoteBalanceGuardTests(unittest.TestCase):
    def test_usdt_quote_buy_blocked_without_usdt_balance(self):
        old_positions = agent.state.get("positions", {})
        try:
            agent.state["positions"] = {}
            ok, reason = agent.entry_quote_balance_ok("DOGE/USDT", "buy")
            self.assertFalse(ok)
            self.assertIn("insufficient USDT balance", reason)
        finally:
            agent.state["positions"] = old_positions

    def test_usdt_quote_buy_allowed_with_usdt_balance(self):
        old_positions = agent.state.get("positions", {})
        try:
            agent.state["positions"] = {
                "USDT/USD": {"quantity": 250.0, "avg_cost": 1.0, "last_price": 1.0},
            }
            ok, reason = agent.entry_quote_balance_ok("DOGE/USDT", "buy")
            self.assertTrue(ok, msg=reason)
        finally:
            agent.state["positions"] = old_positions

    def test_usd_quote_buy_is_allowed(self):
        ok, reason = agent.entry_quote_balance_ok("DOGE/USD", "buy")
        self.assertTrue(ok, msg=reason)


if __name__ == "__main__":
    unittest.main()
