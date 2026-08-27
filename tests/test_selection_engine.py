from __future__ import annotations

import unittest

import pandas as pd

from src.selection_engine import evaluate_selection


class SelectionEngineTests(unittest.TestCase):
    def market_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "date": pd.bdate_range("2026-01-01", periods=60),
                "open": [10.0] * 60,
                "high": [10.5] * 60,
                "low": [9.8] * 60,
                "close": [10.2] * 60,
                "volume": [1_000_000] * 60,
                "amount": [20_000_000] * 60,
            }
        )

    def test_selection_passes_structured_market_rules(self):
        rules = {
            "selection_rules": {
                "enabled": True,
                "allowed_markets": ["SH", "SZ"],
                "exclude_st": True,
                "exclude_suspended": True,
                "min_price": 3,
                "max_price": 100,
                "min_avg_amount_20d": 10_000_000,
            }
        }
        result = evaluate_selection(self.market_frame(), "600000.SH", "浦发银行", rules)
        self.assertEqual("PASS", result.status)
        self.assertFalse(result.blockers)

    def test_selection_rejects_st_and_price_limit(self):
        rules = {
            "selection_rules": {
                "enabled": True,
                "allowed_markets": ["SZ"],
                "exclude_st": True,
                "exclude_suspended": True,
                "min_price": 20,
                "max_price": 0,
                "min_avg_amount_20d": 0,
            }
        }
        result = evaluate_selection(self.market_frame(), "000001.SZ", "*ST 示例", rules)
        self.assertEqual("FAIL", result.status)
        self.assertGreaterEqual(len(result.blockers), 2)

    def test_selection_distinguishes_missing_liquidity_data_from_rule_failure(self):
        rules = {
            "selection_rules": {
                "enabled": True,
                "allowed_markets": ["SH"],
                "exclude_st": True,
                "exclude_suspended": True,
                "min_price": 0,
                "max_price": 0,
                "min_avg_amount_20d": 10_000_000,
            }
        }
        frame = self.market_frame().drop(columns=["amount"])
        result = evaluate_selection(frame, "600000.SH", "浦发银行", rules)
        self.assertEqual("DATA_GAP", result.status)
        self.assertFalse(result.failed)
        self.assertTrue(result.data_gaps)


if __name__ == "__main__":
    unittest.main()
