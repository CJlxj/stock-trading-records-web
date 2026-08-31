from __future__ import annotations

import shutil
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import pandas as pd

import src.signal_engine as signal_engine
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

    ``evaluate_latest`` 现在**必须**由调用方显式传入 ``registry``——它读的是某个
    实例的可编辑规则库。以前缺省会回退到 ``RuleRegistry(PROJECT_ROOT)``，
    也就是开发机上那份随时可被编辑的规则库，测试结果会随本机改过哪条规则而漂移。
    这里统一注入一个只装了出厂规则的临时注册表。
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


class SignalEngineRootIsolationTests(unittest.TestCase):
    """锁定「实例数据必须显式传根、出厂定义随代码走」这条边界。

    重构前：``src/signal_engine.py`` 有模块级 ``PROJECT_ROOT``，
    ``evaluate_latest`` 在缺 ``registry`` 时回退去读它，等于拿代码所在目录的
    用户规则库评估别人的实例；出厂目录还在 import 时就被读掉，
    想换根只能改写模块全局（``tests/test_full_review.py`` 当年正是这么做的）。
    """

    def test_evaluate_latest_requires_an_explicit_rule_registry(self):
        # 少传 registry 必须当场报错，而不是静默去读代码所在目录的规则库。
        frame = add_indicators(sample_ohlcv().assign(date=lambda f: pd.to_datetime(f["date"])))
        with self.assertRaises(TypeError):
            evaluate_latest(frame, rules=load_rules())

    def test_module_keeps_no_instance_root_global(self):
        # 不允许再出现「实例根」形态的模块级全局，否则又会有人去 monkeypatch 它。
        self.assertFalse(hasattr(signal_engine, "PROJECT_ROOT"))
        self.assertTrue(hasattr(signal_engine, "SHIPPED_RULES_ROOT"))

    def test_importing_the_module_reads_no_rule_catalog(self):
        # 出厂目录改成惰性读取：import 期不做规则目录 I/O。
        source = Path(signal_engine.__file__).read_text(encoding="utf-8")
        catalog_call = "_shipped_scored_catalog("
        module_level_calls = [
            line
            for line in source.splitlines()
            if catalog_call in line and not line.startswith((" ", "\t", "@", "def "))
        ]
        self.assertEqual([], module_level_calls)

    def test_shipped_catalog_is_scoped_to_the_root_it_is_given(self):
        # 出厂定义按根取，且结果可缓存复用；不同根给出各自的出厂内容。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            builtin_dir = root / "rules" / "catalog" / "builtin"
            builtin_dir.mkdir(parents=True)
            shutil.copy(
                PROJECT_ROOT / "rules" / "catalog" / "builtin" / "candidate_rules.yaml",
                builtin_dir / "candidate_rules.yaml",
            )
            scoped = signal_engine.candidate_rule_catalog(root)
            shipped = signal_engine.candidate_rule_catalog()

            self.assertEqual(
                {item["id"] for item in shipped}, {item["id"] for item in scoped}
            )
            # 返回的是副本：调用方改动不会污染缓存。
            scoped[0]["id"] = "mutated"
            self.assertNotEqual(
                "mutated", signal_engine.candidate_rule_catalog(root)[0]["id"]
            )

    def test_catalog_by_id_matches_the_catalog(self):
        by_id = signal_engine.candidate_catalog_by_id()
        catalog = signal_engine.candidate_rule_catalog()
        self.assertEqual({item["id"] for item in catalog}, set(by_id))
        self.assertEqual(len(catalog), len(by_id))
