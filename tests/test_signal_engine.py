from __future__ import annotations

import shutil
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import pandas as pd

from src.indicators import add_indicators
from src.rules.registry import RuleRegistry
from src.rules.simple_editor import ensure_rule_catalog_seeded
from src.signal_engine import (
    candidate_framework_diagnostics,
    candidate_rule_catalog,
    evaluate_latest,
    load_rules,
)
from tests.market_fixture import sample_ohlcv


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SignalEngineTests(unittest.TestCase):
    """默认框架的判定必须只由出厂规则决定。

    ``evaluate_latest`` 缺省会向 ``RuleRegistry(PROJECT_ROOT)`` 取当前生效版本，
    也就是开发机上那份可以随时被编辑的规则库。这里统一注入一个只装了出厂规则的
    临时注册表，测试结果才不会随本机改过哪条规则而漂移。
    """

    @classmethod
    def setUpClass(cls):
        cls._catalog_root = tempfile.TemporaryDirectory()
        root = Path(cls._catalog_root.name)
        (root / "rules" / "catalog").mkdir(parents=True)
        shutil.copytree(
            PROJECT_ROOT / "rules" / "catalog" / "builtin",
            root / "rules" / "catalog" / "builtin",
        )
        ensure_rule_catalog_seeded(root)
        cls.registry = RuleRegistry(root)

    @classmethod
    def tearDownClass(cls):
        cls._catalog_root.cleanup()

    def load_demo(self) -> pd.DataFrame:
        frame = sample_ohlcv()
        frame["date"] = pd.to_datetime(frame["date"])
        return add_indicators(frame)

    def test_catalog_has_twenty_rules_split_evenly_across_four_groups(self):
        catalog = candidate_rule_catalog()
        self.assertEqual(20, len(catalog))
        self.assertEqual(20, len({item["id"] for item in catalog}))
        counts: dict[str, int] = {}
        for item in catalog:
            counts[item["group"]] = counts.get(item["group"], 0) + 1
        self.assertEqual(
            {
                "trend_structure": 5,
                "price_volume": 5,
                "momentum": 5,
                "location_risk": 5,
            },
            counts,
        )

    def test_candidate_signal_is_composed_from_four_rule_groups(self):
        rules = deepcopy(load_rules(str(PROJECT_ROOT / "config" / "strategy_rules.yaml")))
        rules["candidate_framework"].update(
            {
                "active_rules": [
                    "close_above_ma20",
                    "volume_ratio_above_1_3",
                    "macd_hist_improving",
                    "not_over_extended",
                ],
                "pass_score_ratio": 0.75,
                "priority_high_ratio": 0.90,
            }
        )
        result = evaluate_latest(
            self.load_demo(),
            rules=rules,
            market_context="supportive",
            registry=self.registry,
        )
        self.assertEqual(
            {"trend_structure", "price_volume", "momentum", "location_risk"},
            set(result.groups),
        )
        self.assertGreaterEqual(result.group_pass_count, 3)
        self.assertIn(result.priority["level"], {"P1", "P2"})
        self.assertEqual(4, result.active_rule_count)
        self.assertEqual(result.matched_rule_count / result.active_rule_count, result.score_ratio)

    def test_only_selected_rules_vote_in_candidate_ratio(self):
        rules = deepcopy(load_rules(str(PROJECT_ROOT / "config" / "strategy_rules.yaml")))
        rules["candidate_framework"].update(
            {
                "active_rules": [
                    "close_above_ma20",
                    "volume_ratio_above_1_3",
                    "macd_hist_improving",
                    "not_over_extended",
                ],
                "pass_score_ratio": 0.75,
                "priority_high_ratio": 0.90,
            }
        )
        result = evaluate_latest(
            self.load_demo(),
            rules=rules,
            market_context="neutral",
            registry=self.registry,
        )
        self.assertEqual("BUY_CANDIDATE", result.status)
        self.assertEqual(4, result.matched_rule_count)
        self.assertEqual(4, result.active_rule_count)
        self.assertEqual(1.0, result.score_ratio)

    def test_framework_diagnostics_rejects_single_group_and_warns_on_strictness(self):
        rules = deepcopy(load_rules(str(PROJECT_ROOT / "config" / "strategy_rules.yaml")))
        rules["candidate_framework"].update(
            {
                "active_rules": [
                    "close_above_ma20",
                    "close_above_ma60",
                    "ma5_above_ma10",
                    "ma20_slope_positive",
                ],
                "pass_score_ratio": 0.80,
                "priority_high_ratio": 0.90,
            }
        )
        diagnostics = candidate_framework_diagnostics(rules)
        self.assertEqual("INVALID", diagnostics["status"])
        self.assertTrue(any("至少覆盖 3" in item for item in diagnostics["errors"]))
        self.assertTrue(any("零候选" in item for item in diagnostics["warnings"]))

    def test_explicit_rule_set_is_not_overridden_by_hidden_legacy_risk_score(self):
        frame = self.load_demo()
        latest_index = frame.index[-1]
        frame.loc[latest_index, "close"] = frame.loc[latest_index, "ma20"] - 1
        frame.loc[latest_index, "ma5"] = frame.loc[latest_index, "ma10"] - 1
        frame.loc[latest_index, "macd_hist"] = -1
        frame.loc[latest_index, "gap_pct"] = 0

        rules = deepcopy(load_rules(str(PROJECT_ROOT / "config" / "strategy_rules.yaml")))
        registry = self.registry
        definition = registry.get("gap_not_extreme", 1)
        result = evaluate_latest(
            frame,
            rules=rules,
            registry=registry,
            rule_set={
                "scored": {
                    "rules": ["gap_not_extreme@1"],
                    "pass_ratio": 1.0,
                },
                "near_policy": {"score_ratio": 0.5},
                "group_policy": {
                    "minimum_groups": 1,
                    "group_pass_ratio": 0.5,
                },
            },
            resolved_rule_set={
                "required": [],
                "veto": [],
                "scored": [definition],
            },
        )

        self.assertGreaterEqual(result.risk_score, 3)
        self.assertEqual("BUY_CANDIDATE", result.status)
        self.assertEqual(["gap_not_extreme"], result.passed)

    def test_secondary_rules_execute_only_after_primary_candidate_is_formed(self):
        rules = deepcopy(load_rules(str(PROJECT_ROOT / "config" / "strategy_rules.yaml")))
        registry = self.registry
        secondary = registry.get("close_above_ma60", 1)

        def evaluate(primary_rule_id: str):
            primary = registry.get(primary_rule_id, 1)
            return evaluate_latest(
                self.load_demo(),
                rules=rules,
                registry=registry,
                rule_set={
                    "scored": {
                        "rules": [f"{primary_rule_id}@1"],
                        "pass_ratio": 1.0,
                    },
                    "secondary": {
                        "rules": ["close_above_ma60@1"],
                        "minimum_match": 1,
                    },
                    "near_policy": {"score_ratio": 0.5},
                    "group_policy": {
                        "minimum_groups": 1,
                        "group_pass_ratio": 0.5,
                    },
                },
                resolved_rule_set={
                    "required": [],
                    "veto": [],
                    "scored": [primary],
                    "secondary": [secondary],
                },
            )

        rejected = evaluate("rsi_healthy")
        self.assertEqual("NO_TRADE", rejected.status)
        self.assertFalse(
            any(
                item.get("screening_stage") == "secondary"
                for item in rejected.rule_results
            )
        )

        candidate = evaluate("gap_not_extreme")
        self.assertEqual("BUY_CANDIDATE", candidate.status)
        self.assertEqual(
            ["close_above_ma60"],
            [
                item["rule_id"]
                for item in candidate.rule_results
                if item.get("screening_stage") == "secondary"
            ],
        )


if __name__ == "__main__":
    unittest.main()
