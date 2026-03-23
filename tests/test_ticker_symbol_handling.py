import unittest

from cli.utils import normalize_ticker_symbol
from tradingagents.agents.utils.agent_utils import build_instrument_context


class TickerSymbolHandlingTests(unittest.TestCase):
    def test_normalize_ticker_symbol_preserves_exchange_suffix(self):
        self.assertEqual(normalize_ticker_symbol(" cnc.to "), "CNC.TO")

    def test_build_instrument_context_mentions_exact_symbol(self):
        context = build_instrument_context("7203.T")
        self.assertIn("7203.T", context)
        self.assertIn("exchange suffix", context)

    def test_normalize_korean_ticker_to_krx_format(self):
        self.assertEqual(normalize_ticker_symbol("005930.ks"), "KRX:005930")
        self.assertEqual(normalize_ticker_symbol("035720.KQ"), "KRX:035720")


if __name__ == "__main__":
    unittest.main()
