from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from src.rule_manager import RuleValidationError, get_editable_rules, save_editable_rules


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RuleManagerTests(unittest.TestCase):
    def make_project(self, temp_dir: str) -> Path:
        root = Path(temp_dir)
        config_dir = root / "config"
        config_dir.mkdir(parents=True)
        shutil.copy(PROJECT_ROOT / "config" / "strategy_rules.yaml", config_dir / "strategy_rules.yaml")
        return root

    def test_save_updates_candidate_combination_and_preserves_noneditable_thresholds(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_project(temp_dir)
            rules = get_editable_rules(root)
            original_overbought = yaml.safe_load(
                (root / "config" / "strategy_rules.yaml").read_text(encoding="utf-8")
            )["thresholds"]["rsi_overbought"]
            rules["selection"].update({"allowed_markets": ["SH", "SZ"], "min_price": 3, "notes": "只看熟悉行业"})
            rules["technical"].update(
                {
                    "active_rules": [
                        "close_above_ma20",
                        "volume_ratio_above_1_3",
                        "macd_hist_improving",
                        "rsi_healthy",
                        "not_over_extended",
                        "atr_not_high",
                    ],
                    "pass_score_ratio": 0.70,
                    "watch_score_ratio": 0.45,
                    "priority_high_ratio": 0.85,
                    "volume_ratio_buy": 1.5,
                }
            )
            rules["buy"].update(
                {
                    "normal_position_pct_max": 0.25,
                    "high_risk_position_pct": 0.40,
                    "heavy_position_pct": 0.55,
                    "require_written_plan": False,
                    "require_stop_loss": False,
                    "require_max_loss": False,
                    "require_sell_condition": False,
                }
            )
            rules["sell"].update(
                {
                    "default_stop_loss_pct": -0.06,
                    "require_predefined_trigger": False,
                    "prohibit_emotion_exit": False,
                    "require_sell_condition": False,
                }
            )

            saved = save_editable_rules(root, rules)
            raw = yaml.safe_load((root / "config" / "strategy_rules.yaml").read_text(encoding="utf-8"))
            self.assertEqual(["SH", "SZ"], saved["selection"]["allowed_markets"])
            self.assertEqual(6, len(saved["technical"]["active_rules"]))
            self.assertEqual(0.70, saved["technical"]["pass_score_ratio"])
            self.assertEqual(0.25, raw["position_rules"]["normal_position_pct_max"])
            self.assertEqual(-0.06, raw["signal_rules"]["stop_trigger"]["default_stop_loss_pct"])
            self.assertEqual("equal_vote", raw["candidate_framework"]["scoring_mode"])
            self.assertEqual(0.70, raw["candidate_framework"]["pass_score_ratio"])
            self.assertEqual(1.5, raw["thresholds"]["volume_ratio_buy"])
            self.assertTrue(raw["trade_rules"]["buy"]["require_written_plan"])
            self.assertTrue(raw["trade_rules"]["buy"]["require_stop_loss"])
            self.assertTrue(raw["trade_rules"]["buy"]["require_max_loss"])
            self.assertTrue(raw["trade_rules"]["buy"]["require_sell_condition"])
            self.assertTrue(raw["trade_rules"]["sell"]["require_predefined_trigger"])
            self.assertTrue(raw["trade_rules"]["sell"]["prohibit_emotion_exit"])
            self.assertTrue(raw["trade_rules"]["sell"]["require_sell_condition"])
            self.assertEqual(original_overbought, raw["thresholds"]["rsi_overbought"])

    def test_invalid_position_order_does_not_rewrite_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_project(temp_dir)
            path = root / "config" / "strategy_rules.yaml"
            before = path.read_text(encoding="utf-8")
            rules = get_editable_rules(root)
            rules["buy"]["normal_position_pct_max"] = 0.50
            rules["buy"]["high_risk_position_pct"] = 0.40
            with self.assertRaises(RuleValidationError):
                save_editable_rules(root, rules)
            self.assertEqual(before, path.read_text(encoding="utf-8"))

    def test_invalid_candidate_group_coverage_does_not_rewrite_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_project(temp_dir)
            path = root / "config" / "strategy_rules.yaml"
            before = path.read_text(encoding="utf-8")
            rules = get_editable_rules(root)
            rules["technical"]["active_rules"] = [
                "close_above_ma20",
                "close_above_ma60",
                "ma5_above_ma10",
                "ma20_slope_positive",
            ]
            with self.assertRaises(RuleValidationError):
                save_editable_rules(root, rules)
            self.assertEqual(before, path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
