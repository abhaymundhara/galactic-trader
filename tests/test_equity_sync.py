import unittest
from datetime import datetime, timedelta

import agent


class EquitySyncTests(unittest.TestCase):
    def test_effective_values_fall_back_to_local_when_no_broker_sync(self):
        old_state = dict(agent.state)
        try:
            agent.state["cash"] = 1000.0
            agent.state["positions"] = {
                "BTC/USD": {"quantity": 0.01, "avg_cost": 50000.0, "last_price": 52000.0}
            }
            agent.state["last_prices"] = {"BTC/USD": 52000.0}
            agent.state["broker_cash"] = 0.0
            agent.state["broker_portfolio_value"] = 0.0
            agent.state["last_account_sync"] = ""

            cash, total, positions, source = agent.effective_portfolio_values()

            self.assertEqual(source, "local")
            self.assertAlmostEqual(cash, 1000.0)
            self.assertAlmostEqual(positions, 520.0)
            self.assertAlmostEqual(total, 1520.0)
        finally:
            agent.state.clear()
            agent.state.update(old_state)

    def test_effective_values_use_fresh_broker_equity(self):
        old_state = dict(agent.state)
        try:
            agent.state["cash"] = 1000.0
            agent.state["positions"] = {
                "BTC/USD": {"quantity": 0.01, "avg_cost": 50000.0, "last_price": 52000.0}
            }
            agent.state["last_prices"] = {"BTC/USD": 52000.0}
            agent.state["broker_cash"] = 1200.0
            agent.state["broker_portfolio_value"] = 1400.0
            agent.state["last_account_sync"] = datetime.utcnow().isoformat()

            cash, total, positions, source = agent.effective_portfolio_values()

            self.assertEqual(source, "broker")
            self.assertAlmostEqual(cash, 1200.0)
            self.assertAlmostEqual(positions, 200.0)
            self.assertAlmostEqual(total, 1400.0)
        finally:
            agent.state.clear()
            agent.state.update(old_state)

    def test_stale_broker_sync_reverts_to_local(self):
        old_state = dict(agent.state)
        try:
            agent.state["cash"] = 1000.0
            agent.state["positions"] = {
                "BTC/USD": {"quantity": 0.01, "avg_cost": 50000.0, "last_price": 52000.0}
            }
            agent.state["last_prices"] = {"BTC/USD": 52000.0}
            agent.state["broker_cash"] = 1200.0
            agent.state["broker_portfolio_value"] = 1400.0
            agent.state["last_account_sync"] = (datetime.utcnow() - timedelta(minutes=10)).isoformat()

            cash, total, positions, source = agent.effective_portfolio_values(max_account_age_seconds=60)

            self.assertEqual(source, "local")
            self.assertAlmostEqual(cash, 1000.0)
            self.assertAlmostEqual(positions, 520.0)
            self.assertAlmostEqual(total, 1520.0)
        finally:
            agent.state.clear()
            agent.state.update(old_state)


if __name__ == "__main__":
    unittest.main()
