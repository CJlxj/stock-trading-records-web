"""共享合成场景的隔离与空实例契约。

这些测试守住 shared-test-data 的隔离合同：场景只存在于调用方给的临时目录，
空实例不受开发 worktree 里 Git 忽略的本地数据影响。
"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.full_review import get_bootstrap
from src.personal_data import list_trade_records
from src.rules.registry import RuleRegistry
from src.rules.version_store import RuleSetStore
from src.signal_engine import load_rules
from src.stock_library import read_local_stock_library
from tests.market_fixture import sample_ohlcv
from tests.panel_scenario_fixture import (
    CANDIDATE_SYMBOL,
    EMPTY_FIRST_RUN,
    PRIMARY_REJECT_SYMBOL,
    PRIMARY_RULE_REF,
    SCENARIO_RULE_SET_NAME,
    SCENARIO_STOCKS,
    SCENARIO_TRADE_DATE,
    SCENARIO_TRADES,
    SECONDARY_REJECT_SYMBOL,
    SECONDARY_RULE_REF,
    TIME_ORDER_AMBIGUOUS,
    TIME_ORDER_AMBIGUOUS_DATE,
    TIME_ORDER_AMBIGUOUS_TRADES,
    LEDGER_INCOMPLETE,
    READY_CURRENT_FLOW,
    ZERO_CANDIDATE,
    ZERO_CANDIDATE_RULE_REFS,
    build_ledger_incomplete,
    build_ready_current_flow,
    build_time_order_ambiguous,
    build_zero_candidate,
    scenario_day_frames,
    screening_rows_by_symbol,
    SEED_SOURCE_ROOT,
    USER_RUNTIME_PATHS,
    assert_no_user_runtime_data,
    build_empty_first_run,
    install_panel_defaults,
)


class PanelScenarioIsolationTests(unittest.TestCase):
    def test_empty_first_run_boots_with_no_user_runtime_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            scenario = build_empty_first_run(temp_dir)
            root = scenario["root"]

            self.assertEqual(EMPTY_FIRST_RUN, scenario["scenario"])
            # 正式启动必需的版本化种子在场。
            self.assertTrue((root / "config" / "strategy_rules.yaml").is_file())
            self.assertTrue(
                (root / "rules" / "catalog" / "builtin" / "candidate_rules.yaml").is_file()
            )
            # 七项「无」逐条核对。
            for relative in USER_RUNTIME_PATHS:
                self.assertFalse((root / relative).exists(), relative)
            assert_no_user_runtime_data(root)

    def test_empty_first_run_has_no_user_created_current_rule_set(self):
        # 内置规则模板是出厂数据，不能被当成用户运行数据；
        # 而「用户创建的当前规则组合」必须确实为空。
        with tempfile.TemporaryDirectory() as temp_dir:
            scenario = build_empty_first_run(temp_dir)
            root = scenario["root"]

            self.assertTrue(scenario["rule_catalog_seeded"]["seeded"])
            self.assertGreater(scenario["rule_catalog_seeded"]["imported_count"], 0)
            # 出厂模板已由正式初始化逻辑导入可编辑规则库。
            self.assertEqual(
                scenario["rule_catalog_seeded"]["imported_count"],
                len(RuleRegistry(root).list_rules()),
            )
            # 但没有任何规则组合，也没有筛选批次。
            self.assertEqual([], RuleSetStore(root).list())
            self.assertFalse((root / "rules" / "rule_sets").exists())
            self.assertFalse((root / "history" / "screenings.sqlite3").exists())

    def test_empty_first_run_can_skip_the_factory_import(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            scenario = build_empty_first_run(temp_dir, seed_rule_catalog=False)
            root = scenario["root"]

            self.assertFalse(scenario["rule_catalog_seeded"]["seeded"])
            self.assertFalse((root / "rules" / "catalog" / "user").exists())
            self.assertTrue((root / "config" / "strategy_rules.yaml").is_file())

    def test_empty_instance_stays_empty_despite_gitignored_local_data(self):
        # 隔离的关键证明：空实例的结果只由 target_root 决定。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = build_empty_first_run(temp_dir)["root"]
            bootstrap = get_bootstrap(root)

            self.assertEqual([], bootstrap["symbols"])
            self.assertEqual("", bootstrap["default_symbol"])
            personal = bootstrap["personal_data"]
            self.assertFalse(personal["ready"])
            self.assertFalse(personal["has_demo_data"])
            self.assertEqual("MISSING", personal["trades"]["state"])
            self.assertEqual("MISSING", personal["positions"]["state"])

            # 开发 worktree 里确实存在 Git 忽略的本地行情时，做一次对照：
            # 同一个函数读真实根目录会有股票，读空实例仍然为空。
            local_data = SEED_SOURCE_ROOT / "data"
            has_local_market_data = local_data.is_dir() and any(
                child.is_dir() and child.name != "universe"
                for child in local_data.iterdir()
            )
            if has_local_market_data:
                self.assertNotEqual(
                    [],
                    get_bootstrap(SEED_SOURCE_ROOT)["symbols"],
                    "开发 worktree 本应有本地行情，对照失效说明这条证明没有生效",
                )
                self.assertEqual([], get_bootstrap(root)["symbols"])

    def test_scenario_factory_refuses_to_write_into_the_repository(self):
        # 场景绝不允许写进仓库：那会覆盖开发目录里的真实运行数据。
        for target in (SEED_SOURCE_ROOT, SEED_SOURCE_ROOT / "records"):
            with self.assertRaisesRegex(AssertionError, "仓库之外"):
                install_panel_defaults(target)
            with self.assertRaisesRegex(AssertionError, "仓库之外"):
                build_empty_first_run(target)

    def test_scenario_self_check_catches_leaked_user_runtime_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = build_empty_first_run(temp_dir)["root"]
            leaked = root / "records" / "my_trades.csv"
            leaked.parent.mkdir(parents=True, exist_ok=True)
            leaked.write_text("trade_date,symbol\n", encoding="utf-8")
            with self.assertRaisesRegex(AssertionError, "混入了用户运行数据"):
                assert_no_user_runtime_data(root)

    def test_two_scenarios_do_not_share_state(self):
        with tempfile.TemporaryDirectory() as first_dir:
            with tempfile.TemporaryDirectory() as second_dir:
                first = build_empty_first_run(first_dir)["root"]
                second = build_empty_first_run(second_dir)["root"]

                self.assertNotEqual(first, second)
                (first / "records").mkdir(parents=True, exist_ok=True)
                (first / "records" / "my_trades.csv").write_text(
                    "trade_date,symbol\n2026-08-30,600760.SH\n", encoding="utf-8"
                )
                # 往第一个场景写东西不影响第二个。
                self.assertFalse((second / "records" / "my_trades.csv").exists())
                self.assertEqual([], get_bootstrap(second)["symbols"])

    def test_rebuilding_in_a_fresh_root_gives_the_same_business_result(self):
        # 基线要求：两个全新临时根重复构建同一场景，业务结果必须一致。
        # 路径、运行时间和随机请求号不参与比较，只比可重复的业务结果。
        def business_result(target_root: str) -> dict:
            scenario = build_empty_first_run(target_root)
            root = scenario["root"]
            bootstrap = get_bootstrap(root)
            return {
                "scenario": scenario["scenario"],
                "seeded": scenario["rule_catalog_seeded"]["seeded"],
                "imported_count": scenario["rule_catalog_seeded"]["imported_count"],
                "rule_ids": sorted(
                    str(rule["id"]) for rule in RuleRegistry(root).list_rules()
                ),
                "rule_sets": RuleSetStore(root).list(),
                "symbols": bootstrap["symbols"],
                "default_symbol": bootstrap["default_symbol"],
                "trades_state": bootstrap["personal_data"]["trades"]["state"],
                "positions_state": bootstrap["personal_data"]["positions"]["state"],
            }

        with tempfile.TemporaryDirectory() as first_dir:
            first = business_result(first_dir)
        with tempfile.TemporaryDirectory() as second_dir:
            second = business_result(second_dir)

        self.assertEqual(first, second)
        self.assertNotEqual([], first["rule_ids"])

    def test_empty_first_run_separates_no_data_from_a_failed_run(self):
        # 基线要求：空实例必须给出「有效空状态」，并且能和「运行失败」区分开。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = build_empty_first_run(temp_dir)["root"]
            personal = get_bootstrap(root)["personal_data"]

            for kind in ("trades", "positions"):
                # 尚无数据是 MISSING，不是读取失败的 INVALID。
                self.assertEqual("MISSING", personal[kind]["state"], kind)
                self.assertNotEqual("INVALID", personal[kind]["state"], kind)
                self.assertEqual(0, personal[kind]["rows"], kind)
                self.assertEqual("尚未导入", personal[kind]["message"], kind)
            self.assertTrue(personal["local_only"])
            # 无账户资产：账户总资产不存在，也不给默认值。
            self.assertIsNone(personal["positions"]["latest_date"])
            self.assertEqual(0, personal["positions"]["symbols"])


if __name__ == "__main__":
    unittest.main()


class ReadyCurrentFlowTests(unittest.TestCase):
    """READY_CURRENT_FLOW：逐层验证业务结果**及其来源**，不只检查文件存在。"""

    @classmethod
    def setUpClass(cls):
        cls._context = tempfile.TemporaryDirectory()
        cls.scenario = build_ready_current_flow(cls._context.name)
        cls.root = cls.scenario["root"]

    @classmethod
    def tearDownClass(cls):
        cls._context.cleanup()

    def test_three_stocks_have_aligned_deterministic_day_data(self):
        library = read_local_stock_library(self.root)
        self.assertEqual(
            {symbol for symbol, _ in SCENARIO_STOCKS},
            set(library["symbol"].astype(str)),
        )
        for symbol, _ in SCENARIO_STOCKS:
            files = list((self.root / "data" / symbol / "raw" / "day").glob("*.csv"))
            self.assertEqual(1, len(files), symbol)
            frame = pd.read_csv(files[0])
            # 字段完整、截止日期一致。
            self.assertEqual(
                set(), {"date", "open", "high", "low", "close", "volume"} - set(frame.columns)
            )
            self.assertFalse(frame.isna().any().any(), symbol)
            self.assertEqual(SCENARIO_TRADE_DATE, str(frame["date"].iloc[-1])[:10], symbol)

    def test_day_data_is_derived_from_the_shared_ohlcv_fixture(self):
        # 复用 tests/market_fixture.py，不新建第二套同义生成器。
        base = sample_ohlcv(end=SCENARIO_TRADE_DATE)
        frames = scenario_day_frames()
        self.assertEqual({symbol for symbol, _ in SCENARIO_STOCKS}, set(frames))
        for symbol, frame in frames.items():
            self.assertEqual(len(base), len(frame), symbol)
            self.assertEqual(
                list(base["date"].astype(str)), list(frame["date"].astype(str)), symbol
            )

    def test_screening_uses_the_fixed_date_without_relaxing_default_freshness(self):
        rules = load_rules(str(self.root / "config" / "strategy_rules.yaml"))
        self.assertEqual(7, rules["data_quality"]["max_age_days"]["day"])

        screening = self.scenario["screening"]
        self.assertEqual(
            SCENARIO_TRADE_DATE,
            screening["dataset_manifest"]["current_date"],
        )
        self.assertTrue(
            all(row["data_age_days"] == 0 for row in screening["row_results"])
        )

    def test_current_rule_set_is_created_and_activated_through_the_store(self):
        active = RuleSetStore(self.root).active()
        self.assertEqual(self.scenario["rule_set"]["id"], active["id"])
        self.assertEqual(SCENARIO_RULE_SET_NAME, active["name"])
        self.assertEqual("simple_all", active["editor_mode"])
        # simple_all 的含义：没有必须满足/命中即排除，一级规则全过才算候选。
        self.assertEqual([], active["required"]["rules"])
        self.assertEqual([], active["veto"]["rules"])
        self.assertEqual(1.0, active["scored"]["pass_ratio"])
        self.assertEqual([PRIMARY_RULE_REF], active["scored"]["rules"])
        self.assertEqual([SECONDARY_RULE_REF], active["secondary"]["rules"])

    def test_screening_batch_produces_the_three_required_outcomes(self):
        rows = screening_rows_by_symbol(self.scenario["screening"])
        self.assertEqual({symbol for symbol, _ in SCENARIO_STOCKS}, set(rows))

        candidate = rows[CANDIDATE_SYMBOL]
        self.assertTrue(candidate["primary_candidate"])
        self.assertEqual("PASS", candidate["secondary_status"])
        self.assertTrue(candidate["final_candidate"])

        primary_reject = rows[PRIMARY_REJECT_SYMBOL]
        self.assertFalse(primary_reject["primary_candidate"])
        self.assertFalse(primary_reject["final_candidate"])

        secondary_reject = rows[SECONDARY_REJECT_SYMBOL]
        self.assertTrue(secondary_reject["primary_candidate"])
        self.assertEqual("FAIL", secondary_reject["secondary_status"])
        self.assertFalse(secondary_reject["final_candidate"])

    def test_每层结果都能追溯到它的来源(self):
        meta = self.scenario["screening"]["meta"]
        # 批次记着它用的规则组合、数据指纹和筛选范围。
        self.assertEqual(self.scenario["rule_set"]["id"], meta["rule_set_id"])
        self.assertEqual(SCENARIO_TRADE_DATE, meta["as_of_trade_date"])
        self.assertEqual("local_library", meta["universe_scope"])
        for key in ("dataset_hash", "rule_hash", "input_fingerprint"):
            self.assertTrue(str(meta.get(key) or "").strip(), key)

        # 每一行都带规则实值，候选结论可以逐条对照。
        candidate = screening_rows_by_symbol(self.scenario["screening"])[CANDIDATE_SYMBOL]
        scored = [
            item for item in candidate["rule_evidence"] if item.get("kind") != "base_gate"
        ]
        self.assertEqual(
            {"close_above_ma20", "close_above_ma60"},
            {str(item["rule_id"]) for item in scored},
        )
        self.assertTrue(all(item["status"] == "PASS" for item in scored))

    def test_three_trades_are_written_through_the_official_entry(self):
        listed = list_trade_records(self.root)
        self.assertEqual(len(SCENARIO_TRADES), listed["count"])
        # 列表最新在前，按时间正序核对四类归类中的三类。
        records = list(reversed(listed["records"]))
        self.assertEqual(
            ["首次买入", "加仓", "减仓"],
            [record["operation_label"] for record in records],
        )
        self.assertEqual([1000, 1500, 900], [r["remaining_shares"] for r in records])
        # 每笔都有明确实际时间和明确费用，因此是精确完整值。
        for record, expected in zip(records, SCENARIO_TRADES):
            self.assertTrue(record["trade_time_known"], expected["request_id"])
            self.assertEqual(expected["trade_time"], record["trade_time_label"])
            self.assertEqual(expected["fee"], record["fee"])
            self.assertTrue(record["fee_reported"])
            self.assertTrue(record["exact"])
            self.assertEqual("", record["incomplete_label"])

        summary = listed["symbol_summaries"][0]
        self.assertEqual(CANDIDATE_SYMBOL, summary["symbol"])
        self.assertEqual(900, summary["remaining_shares"])
        self.assertTrue(summary["exact"])

    def test_scenario_prepares_no_future_business_objects(self):
        # 第一版不造持仓快照、账户资产、计划、纪律结论或复盘对象。
        self.assertFalse((self.root / "records" / "positions_snapshot.csv").exists())
        personal = get_bootstrap(self.root)["personal_data"]
        self.assertEqual("MISSING", personal["positions"]["state"])
        for record in list_trade_records(self.root)["records"]:
            self.assertEqual("历史未审查", record["rule_status"])

    def test_rebuilding_in_a_fresh_root_gives_the_same_business_result(self):
        def business_result(scenario: dict) -> dict:
            listed = list_trade_records(scenario["root"])
            rows = screening_rows_by_symbol(scenario["screening"])
            active = scenario["rule_set"]
            return {
                "rule_set": {
                    "name": active["name"],
                    "scored": active["scored"]["rules"],
                    "secondary": active["secondary"]["rules"],
                },
                "screening": {
                    symbol: (
                        row["primary_candidate"],
                        row["secondary_status"],
                        row["final_candidate"],
                        row["matched_rule_count"],
                    )
                    for symbol, row in rows.items()
                },
                "trades": [
                    (
                        record["trade_date"],
                        record["trade_time"],
                        record["operation_label"],
                        record["remaining_shares"],
                        record["avg_cost_after_trade"],
                        record["realized_pnl"],
                        record["fee"],
                    )
                    for record in listed["records"]
                ],
                "summary": listed["symbol_summaries"],
            }

        first = business_result(self.scenario)
        with tempfile.TemporaryDirectory() as temp_dir:
            second = business_result(build_ready_current_flow(temp_dir))
        # 运行时间、临时路径和随机组合编号不参与比较。
        self.assertEqual(first, second)


class ZeroCandidateTests(unittest.TestCase):
    """ZERO_CANDIDATE：一次**有效的成功批次**，只是最终候选为零。

    必须能和三种看起来相似、性质完全不同的状态区分开：
    空实例（什么都还没有）、数据缺口（有股票但算不出来）、运行错误（根本没跑完）。
    """

    @classmethod
    def setUpClass(cls):
        cls._context = tempfile.TemporaryDirectory()
        cls.scenario = build_zero_candidate(cls._context.name)
        cls.root = cls.scenario["root"]
        cls.rows = screening_rows_by_symbol(cls.scenario["screening"])

    @classmethod
    def tearDownClass(cls):
        cls._context.cleanup()

    def test_final_candidate_count_is_zero(self):
        self.assertEqual(len(SCENARIO_STOCKS), len(self.rows))
        self.assertEqual(
            [], [s for s, row in self.rows.items() if row["final_candidate"]]
        )
        self.assertEqual(
            [], [s for s, row in self.rows.items() if row["primary_candidate"]]
        )

    def test_it_is_not_the_empty_instance(self):
        # 空实例什么都没有；这里股票、行情、规则组合、批次全都在。
        self.assertEqual(
            {symbol for symbol, _ in SCENARIO_STOCKS},
            set(read_local_stock_library(self.root)["symbol"].astype(str)),
        )
        self.assertEqual(
            len(SCENARIO_STOCKS), len(get_bootstrap(self.root)["symbols"])
        )
        self.assertTrue(RuleSetStore(self.root).active())
        self.assertEqual(len(SCENARIO_STOCKS), len(self.rows))

        with tempfile.TemporaryDirectory() as temp_dir:
            empty = build_empty_first_run(temp_dir)["root"]
            self.assertEqual([], get_bootstrap(empty)["symbols"])
            self.assertEqual([], RuleSetStore(empty).list())

    def test_it_is_not_a_data_gap(self):
        # 数据缺口是「算不出来」；这里每只股票都算出了结果，只是没达标。
        for symbol, row in self.rows.items():
            self.assertNotEqual("DATA_GAP", row["technical_status"], symbol)
            self.assertEqual("LOCAL", row["history_status"], symbol)
            self.assertEqual(SCENARIO_TRADE_DATE, str(row["data_date"])[:10], symbol)
            self.assertFalse(row["data_gaps"], symbol)
            self.assertGreater(row["active_rule_count"], 0, symbol)
            # 每条一级规则都真的算出了 PASS/FAIL，没有一条是 DATA_GAP。
            scored = [
                item for item in row["rule_evidence"] if item.get("kind") != "base_gate"
            ]
            self.assertTrue(scored, symbol)
            self.assertEqual(
                set(), {item["status"] for item in scored} - {"PASS", "FAIL"}, symbol
            )

    def test_it_is_not_a_failed_run(self):
        # 运行错误是「没跑完」；这里批次完整落库，指纹齐全，可追溯。
        meta = self.scenario["screening"]["meta"]
        self.assertEqual(self.scenario["rule_set"]["id"], meta["rule_set_id"])
        self.assertEqual(SCENARIO_TRADE_DATE, meta["as_of_trade_date"])
        self.assertEqual("local_library", meta["universe_scope"])
        for key in ("dataset_hash", "rule_hash", "input_fingerprint"):
            self.assertTrue(str(meta.get(key) or "").strip(), key)
        for symbol, row in self.rows.items():
            self.assertFalse(row["blocking_rule_error"], symbol)

    def test_no_rule_was_weakened_and_no_near_miss_was_promoted(self):
        active = RuleSetStore(self.root).active()
        # 启用的组合与请求的一字不差，没有被自动放宽。
        self.assertEqual(list(ZERO_CANDIDATE_RULE_REFS), active["scored"]["rules"])
        self.assertEqual(1.0, active["scored"]["pass_ratio"])
        self.assertEqual([], active["required"]["rules"])
        self.assertEqual([], active["veto"]["rules"])

        # 有股票落在「接近门槛」，但它绝不能被算成候选。
        watch = [s for s, row in self.rows.items() if row["technical_status"] == "WATCH"]
        self.assertTrue(watch, "本场景应至少有一只接近门槛的股票，用来验证它不被提拔")
        for symbol in watch:
            self.assertFalse(self.rows[symbol]["final_candidate"], symbol)
            self.assertFalse(self.rows[symbol]["primary_candidate"], symbol)

        # 三只各自倒在不同规则上，说明是规则严、不是数据坏。
        failed = {
            symbol: sorted(
                str(item["rule_id"])
                for item in row["rule_evidence"]
                if item.get("kind") != "base_gate" and item["status"] == "FAIL"
            )
            for symbol, row in self.rows.items()
        }
        self.assertTrue(all(failed.values()), failed)

    def test_zero_candidate_repeats_identically_in_a_fresh_root(self):
        def business_result(scenario: dict) -> dict:
            rows = screening_rows_by_symbol(scenario["screening"])
            return {
                "scored": scenario["rule_set"]["scored"]["rules"],
                "rows": {
                    symbol: (
                        row["primary_candidate"],
                        row["final_candidate"],
                        row["technical_status"],
                        row["history_status"],
                        tuple(
                            sorted(
                                (str(item["rule_id"]), item["status"])
                                for item in row["rule_evidence"]
                                if item.get("kind") != "base_gate"
                            )
                        ),
                    )
                    for symbol, row in rows.items()
                },
            }

        first = business_result(self.scenario)
        with tempfile.TemporaryDirectory() as temp_dir:
            second = business_result(build_zero_candidate(temp_dir))
        self.assertEqual(first, second)

    def test_zero_candidate_writes_no_trades_or_positions(self):
        # 本轮不增加成交异常、持仓冲突、账户资产缺失或筛选过期场景。
        listed = list_trade_records(self.root)
        self.assertEqual(0, listed["count"])
        self.assertEqual([], listed["symbol_summaries"])
        self.assertFalse((self.root / "records" / "positions_snapshot.csv").exists())


class LedgerIncompleteTests(unittest.TestCase):
    """LEDGER_INCOMPLETE：只有一只股票的一段历史缺字段，其余一切照常。"""

    @classmethod
    def setUpClass(cls):
        cls._context = tempfile.TemporaryDirectory()
        cls.scenario = build_ledger_incomplete(cls._context.name)
        cls.root = cls.scenario["root"]
        cls.listed = list_trade_records(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls._context.cleanup()

    def test_missing_fields_stay_empty_without_backfill(self):
        warnings = self.scenario["imported"]["warnings"]
        for label in ("成交后剩余股数", "成交后平均成本", "该笔已实现盈亏"):
            self.assertTrue(
                any(label in item and "不做推算" in item for item in warnings), label
            )
        # 落盘单元格必须真的是空，不是 "0"。
        raw = list(
            csv.DictReader(
                (self.root / "records" / "my_trades.csv")
                .read_text(encoding="utf-8-sig")
                .splitlines()
            )
        )
        imported_row = next(r for r in raw if r["trade_date"] == "2026-06-15")
        for column in ("remaining_shares", "avg_cost_after_trade", "realized_pnl", "fee"):
            self.assertEqual("", imported_row[column], column)

    def test_the_original_trades_are_still_shown(self):
        self.assertEqual(3, self.listed["count"])
        records = list(reversed(self.listed["records"]))
        # 日期、时间、方向、价格、数量这些原始事实一条不少。
        self.assertEqual(
            ["2026-06-15", "2026-06-16", "2026-06-17"],
            [record["trade_date"] for record in records],
        )
        self.assertEqual(
            ["09:35", "10:20", "14:05"],
            [record["trade_time_label"] for record in records],
        )
        self.assertEqual(["BUY", "BUY", "SELL"], [r["side"] for r in records])
        self.assertEqual([49.19, 50.10, 52.60], [r["price"] for r in records])
        self.assertEqual([1000, 500, 600], [r["shares"] for r in records])

    def test_exact_ledger_numbers_and_the_summary_are_marked_incomplete(self):
        for record in self.listed["records"]:
            self.assertIsNone(record["remaining_shares"], record["trade_date"])
            self.assertIsNone(record["avg_cost_after_trade"], record["trade_date"])
            self.assertEqual("未归类", record["operation_label"], record["trade_date"])
            self.assertFalse(record["complete"], record["trade_date"])
            self.assertEqual("历史数据不完整", record["incomplete_label"])
        self.assertEqual(3, self.listed["incomplete_record_count"])

        summary = self.listed["symbol_summaries"][0]
        self.assertEqual(CANDIDATE_SYMBOL, summary["symbol"])
        self.assertIsNone(summary["remaining_shares"])
        self.assertIsNone(summary["avg_cost"])
        self.assertFalse(summary["complete"])
        self.assertFalse(summary["exact"])
        self.assertEqual("历史数据不完整", summary["incomplete_label"])

    def test_the_fee_fact_is_missing_too(self):
        imported = next(
            r for r in self.listed["records"] if r["trade_date"] == "2026-06-15"
        )
        self.assertIsNone(imported["fee"])
        self.assertFalse(imported["fee_reported"])

    def test_market_rules_and_screening_stay_normal(self):
        # 局部账本问题不得扩大成整个面板不可用。
        rows = screening_rows_by_symbol(self.scenario["screening"])
        self.assertEqual(
            [CANDIDATE_SYMBOL], [s for s, row in rows.items() if row["final_candidate"]]
        )
        for symbol, row in rows.items():
            self.assertNotEqual("DATA_GAP", row["technical_status"], symbol)
            self.assertEqual("LOCAL", row["history_status"], symbol)
        self.assertTrue(RuleSetStore(self.root).active())
        self.assertEqual(
            len(SCENARIO_STOCKS), len(get_bootstrap(self.root)["symbols"])
        )

    def test_ledger_incomplete_repeats_identically_in_a_fresh_root(self):
        def business_result(scenario: dict) -> dict:
            listed = list_trade_records(scenario["root"])
            return {
                "count": listed["count"],
                "incomplete": listed["incomplete_record_count"],
                "records": [
                    (
                        record["trade_date"],
                        record["trade_time"],
                        record["side"],
                        record["price"],
                        record["shares"],
                        record["remaining_shares"],
                        record["avg_cost_after_trade"],
                        record["operation_label"],
                        record["incomplete_label"],
                    )
                    for record in listed["records"]
                ],
                "summary": listed["symbol_summaries"],
                "candidates": sorted(
                    symbol
                    for symbol, row in screening_rows_by_symbol(
                        scenario["screening"]
                    ).items()
                    if row["final_candidate"]
                ),
            }

        first = business_result(self.scenario)
        with tempfile.TemporaryDirectory() as temp_dir:
            # 必须在临时目录仍然存在时读取，否则比较的是一个已被删除的根。
            second = business_result(build_ledger_incomplete(temp_dir))
        self.assertEqual(first, second)

    def test_only_one_stock_is_affected(self):
        # 只改一只股票的一段历史；另外两只根本没有成交，不受牵连。
        self.assertEqual(
            [CANDIDATE_SYMBOL],
            [summary["symbol"] for summary in self.listed["symbol_summaries"]],
        )

    def test_field_incompleteness_does_not_make_the_trade_order_ambiguous(self):
        summary = self.listed["symbol_summaries"][0]
        self.assertFalse(summary["complete"])
        self.assertEqual("历史数据不完整", summary["incomplete_label"])
        self.assertEqual("ORDER_KNOWN", summary["order_status"])
        self.assertEqual("顺序明确", summary["order_status_label"])
        self.assertEqual("scenario-reduce-0003", summary["basis_record_ref"])


class TimeOrderAmbiguousTests(unittest.TestCase):
    """功能 1.2 增量场景：完整链路中只有同日成交顺序无法确认。"""

    @classmethod
    def setUpClass(cls):
        cls._context = tempfile.TemporaryDirectory()
        cls.scenario = build_time_order_ambiguous(cls._context.name)
        cls.root = cls.scenario["root"]
        cls.listed = list_trade_records(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls._context.cleanup()

    def test_both_original_trade_facts_are_saved_and_have_unique_refs(self):
        same_day = [
            record
            for record in self.listed["records"]
            if record["trade_date"] == TIME_ORDER_AMBIGUOUS_DATE
        ]
        self.assertEqual(2, len(same_day))
        self.assertEqual(
            {trade["request_id"] for trade in TIME_ORDER_AMBIGUOUS_TRADES},
            {record["record_ref"] for record in same_day},
        )
        self.assertEqual(2, len({record["record_ref"] for record in same_day}))
        self.assertEqual(
            {"09:40", "未记录"},
            {record["trade_time_label"] for record in same_day},
        )
        self.assertEqual({"BUY", "SELL"}, {record["side"] for record in same_day})
        self.assertEqual({51.2, 52.1}, {record["price"] for record in same_day})
        self.assertEqual({100, 50}, {record["shares"] for record in same_day})

    def test_summary_marks_order_ambiguous_and_selects_no_basis(self):
        summary = self.listed["symbol_summaries"][0]
        self.assertEqual(TIME_ORDER_AMBIGUOUS, self.scenario["scenario"])
        self.assertEqual("ORDER_AMBIGUOUS", summary["order_status"])
        self.assertEqual("顺序待核对", summary["order_status_label"])
        self.assertIsNone(summary["basis_record_ref"])
        self.assertIsNone(summary["remaining_shares"])
        self.assertIsNone(summary["avg_cost"])
        self.assertIsNone(summary["last_trade_date"])
        self.assertIsNone(summary["last_trade_time"])
        self.assertIsNone(summary["last_operation_label"])
        self.assertEqual(5, self.listed["count"])

    def test_screening_and_rule_results_are_unchanged(self):
        rows = screening_rows_by_symbol(self.scenario["screening"])
        self.assertEqual(
            [CANDIDATE_SYMBOL],
            [symbol for symbol, row in rows.items() if row["final_candidate"]],
        )
        self.assertTrue(RuleSetStore(self.root).active())

    def test_scenario_repeats_identically_in_a_fresh_root(self):
        def result(scenario: dict) -> dict:
            listed = list_trade_records(scenario["root"])
            summary = listed["symbol_summaries"][0]
            return {
                "refs": sorted(record["record_ref"] for record in listed["records"]),
                "order": summary["order_status"],
                "basis": summary["basis_record_ref"],
                "count": listed["count"],
            }

        first = result(self.scenario)
        with tempfile.TemporaryDirectory() as temp_dir:
            second = result(build_time_order_ambiguous(temp_dir))
        self.assertEqual(first, second)


class FeatureOneScenarioSuiteTests(unittest.TestCase):
    """Feature 1 收口：五个场景可独立构建、互不共享状态、不污染开发仓库。"""

    BUILDERS = (
        (EMPTY_FIRST_RUN, build_empty_first_run),
        (READY_CURRENT_FLOW, build_ready_current_flow),
        (ZERO_CANDIDATE, build_zero_candidate),
        (LEDGER_INCOMPLETE, build_ledger_incomplete),
        (TIME_ORDER_AMBIGUOUS, build_time_order_ambiguous),
    )

    @staticmethod
    def _repo_snapshot() -> dict[str, list[str]]:
        """开发仓库里那些「正常运行数据」目录的当前内容。"""
        snapshot: dict[str, list[str]] = {}
        for relative in ("data", "records", "history", "rules/catalog/user",
                         "rules/rule_sets", "config"):
            target = SEED_SOURCE_ROOT / relative
            snapshot[relative] = sorted(
                str(path.relative_to(SEED_SOURCE_ROOT))
                for path in target.rglob("*")
            ) if target.exists() else []
        return snapshot

    def test_all_five_scenarios_build_independently(self):
        for name, builder in self.BUILDERS:
            with self.subTest(scenario=name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    scenario = builder(temp_dir)
                    self.assertEqual(name, scenario["scenario"])
                    self.assertTrue(Path(scenario["root"]).is_dir())

    def test_scenarios_share_no_state_with_each_other(self):
        roots = []
        contexts = [tempfile.TemporaryDirectory() for _ in self.BUILDERS]
        try:
            for (name, builder), context in zip(self.BUILDERS, contexts):
                roots.append(Path(builder(context.name)["root"]).resolve())
            # 五个根两两不同，且互不为对方的子目录。
            self.assertEqual(len(roots), len({str(root) for root in roots}))
            for first in roots:
                for second in roots:
                    if first is not second:
                        self.assertNotIn(first, second.parents)
            # 规则组合、筛选库、成交台账各自独立。
            ready, zero = roots[1], roots[2]
            self.assertNotEqual(
                RuleSetStore(ready).active()["name"],
                RuleSetStore(zero).active()["name"],
            )
            self.assertEqual(3, list_trade_records(roots[1])["count"])
            self.assertEqual(0, list_trade_records(roots[2])["count"])
        finally:
            for context in contexts:
                context.cleanup()

    def test_building_every_scenario_leaves_the_repository_untouched(self):
        before = self._repo_snapshot()
        for _name, builder in self.BUILDERS:
            with tempfile.TemporaryDirectory() as temp_dir:
                builder(temp_dir)
        self.assertEqual(before, self._repo_snapshot())

    def test_scenarios_are_discovered_by_the_full_test_run(self):
        # 收口检查：五个场景都在本模块里有专属测试类，不会被遗漏。
        module = sys.modules[__name__]
        covered = {
            EMPTY_FIRST_RUN: PanelScenarioIsolationTests,
            READY_CURRENT_FLOW: ReadyCurrentFlowTests,
            ZERO_CANDIDATE: ZeroCandidateTests,
            LEDGER_INCOMPLETE: LedgerIncompleteTests,
            TIME_ORDER_AMBIGUOUS: TimeOrderAmbiguousTests,
        }
        for name, case in covered.items():
            self.assertTrue(issubclass(case, unittest.TestCase), name)
            self.assertTrue(
                any(attr.startswith("test_") for attr in dir(case)), name
            )
        self.assertEqual({name for name, _ in self.BUILDERS}, set(covered))
