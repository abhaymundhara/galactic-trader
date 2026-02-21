import unittest

import agent


class OrderQtyNormalizationTests(unittest.TestCase):
    def test_crypto_qty_is_rounded_down_not_up(self):
        self.assertEqual(agent.normalize_crypto_order_qty(115.91646654), 115.916466)

    def test_crypto_dust_below_step_becomes_zero(self):
        self.assertEqual(agent.normalize_crypto_order_qty(0.000000345), 0.0)

    def test_crypto_qty_keeps_six_decimal_precision(self):
        self.assertEqual(agent.normalize_crypto_order_qty(1.23456789), 1.234567)


if __name__ == "__main__":
    unittest.main()
