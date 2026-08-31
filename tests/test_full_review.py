from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
import tempfile
import shutil
import yaml

import pandas as pd

from src.full_review import _record_summary, get_bootstrap, run_full_review
from src.personal_data import list_trade_records
from src.rules.simple_editor import ensure_rule_catalog_seeded
from src.trade_analyzer import summarize_symbol
from tests.market_fixture import sample_ohlcv
from tests.panel_scenario_fixture import (
    CANDIDATE_SYMBOL,
    build_empty_first_run,
    build_ledger_incomplete,
    build_time_order_ambiguous,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FullReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        frame = sample_ohlcv()
        dates = pd.to_datetime(frame["date"])
        frame["date"] = dates + (pd.Timestamp(date.today()) - dates.iloc[-1])
        cls.fresh_csv = frame.to_csv(index=False)

        cls.review_root_context = tempfile.TemporaryDirectory()
        cls.review_root = Path(cls.review_root_context.name)
        shutil.copytree(
            PROJECT_ROOT / "rules" / "catalog" / "builtin",
            cls.review_root / "rules" / "catalog" / "builtin",
        )
        ensure_rule_catalog_seeded(cls.review_root)
        config_dir = cls.review_root / "config"
        config_dir.mkdir(parents=True)
        rules = yaml.safe_load((PROJECT_ROOT / "config" / "strategy_rules.yaml").read_text(encoding="utf-8"))
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
        (config_dir / "strategy_rules.yaml").write_text(
            yaml.safe_dump(rules, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        stale_data_dir = cls.review_root / "data" / "600760.SH" / "raw" / "day"
        stale_data_dir.mkdir(parents=True)
        sample_ohlcv().to_csv(stale_data_dir / "600760.SH_day_demo.csv", index=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.review_root_context.cleanup()

    def fresh_payload(self, **overrides):
        payload = {
            "symbol": "600760.SH",
            "stock_name": "中航沈飞",
            "timeframe": "day",
            "data_source": "upload",
            "csv_name": "fresh_market_data.csv",
            "csv_text": self.fresh_csv,
            "use_local_records": False,
            "review_intent": "daily",
            "decision_window": "after_close",
            "account_total_asset": 140000,
            "position_shares": 0,
            "current_price": 44.38,
        }
        payload.update(overrides)
        return payload

    def test_bootstrap_starts_without_packaged_personal_symbols(self):
        # 用共享空实例断言「首发不自带个人股票」。原先直接读真实 PROJECT_ROOT，
        # 开发 worktree 里 Git 忽略的本地行情会让它失败——那是测试隔离问题，
        # 不是产品行为问题。空实例与本地数据是否存在无关。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = build_empty_first_run(temp_dir)["root"]
            bootstrap = get_bootstrap(root)
            self.assertEqual([], bootstrap["symbols"])

    def test_bootstrap_prefers_latest_day_close_and_trade_fallbacks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data" / "600000.SH" / "raw" / "day"
            records_dir = root / "records"
            data_dir.mkdir(parents=True)
            records_dir.mkdir(parents=True)
            frame = sample_ohlcv()
            frame["close"] = frame["close"].where(frame.index != frame.index[-1], 51.23)
            frame.to_csv(data_dir / "600000.SH_day_20260612.csv", index=False)
            pd.DataFrame(
                [
                    {
                        "trade_date": "2026-06-12",
                        "symbol": "600000.SH",
                        "stock_name": "浦发银行",
                        "remaining_shares": 500,
                        "avg_cost_after_trade": 48.5,
                        "account_total_asset": 200000,
                    }
                ]
            ).to_csv(records_dir / "my_trades.csv", index=False)
            bootstrap = get_bootstrap(root)
            config = bootstrap["symbols"][0]
            self.assertEqual(500, config["defaults"]["position_shares"])
            self.assertEqual(48.5, config["defaults"]["avg_cost"])
            self.assertEqual(51.23, config["defaults"]["current_price"])
            self.assertEqual("2026-06-12", config["latest_day"]["date"])

    def test_trade_summary_never_uses_csv_order_as_business_order(self):
        rows = [
            {
                "trade_date": "2026-04-01",
                "trade_time": "",
                "symbol": "600000.SH",
                "side": "BUY",
                "price": 10,
                "shares": 1000,
                "gross_amount": 10000,
                "remaining_shares": 1000,
                "avg_cost_after_trade": 10,
                "realized_pnl": 0,
                "record_ref": "order-buy-0001",
            },
            {
                "trade_date": "2026-04-01",
                "trade_time": "",
                "symbol": "600000.SH",
                "side": "SELL",
                "price": 11,
                "shares": 1000,
                "gross_amount": 11000,
                "remaining_shares": 0,
                "avg_cost_after_trade": 0,
                "realized_pnl": 1000,
                "record_ref": "order-sell-0002",
            },
        ]
        seen = []
        for ordered in (rows, list(reversed(rows))):
            summary = summarize_symbol(
                pd.DataFrame(ordered), "600000.SH", latest_price=12
            )
            seen.append(summary)
            self.assertEqual("ORDER_AMBIGUOUS", summary["order_status"])
            self.assertFalse(summary["ledger_complete"])
            self.assertIsNone(summary["remaining_shares"])
            self.assertIsNone(summary["avg_cost"])
            self.assertIsNone(summary["basis_record_ref"])
        self.assertEqual(seen[0]["remaining_shares"], seen[1]["remaining_shares"])
        self.assertEqual(seen[0]["avg_cost"], seen[1]["avg_cost"])

    def test_closed_trade_summary_needs_no_fake_price_for_zero_position(self):
        summary = summarize_symbol(
            pd.DataFrame(
                [
                    {
                        "trade_date": "2026-04-01",
                        "trade_time": "09:30",
                        "symbol": "600000.SH",
                        "side": "BUY",
                        "price": 10,
                        "shares": 1000,
                        "gross_amount": 10000,
                        "remaining_shares": 1000,
                        "avg_cost_after_trade": 10,
                        "realized_pnl": 0,
                        "record_ref": "close-buy-0001",
                    },
                    {
                        "trade_date": "2026-04-02",
                        "trade_time": "14:30",
                        "symbol": "600000.SH",
                        "side": "SELL",
                        "price": 11,
                        "shares": 1000,
                        "gross_amount": 11000,
                        "remaining_shares": 0,
                        "avg_cost_after_trade": 0,
                        "realized_pnl": 1000,
                        "record_ref": "close-sell-0002",
                    },
                ]
            ),
            "600000.SH",
        )
        self.assertEqual("ORDER_KNOWN", summary["order_status"])
        self.assertEqual(0, summary["remaining_shares"])
        self.assertEqual(0.0, summary["avg_cost"])
        self.assertEqual(0.0, summary["market_value"])
        self.assertIsNone(summary["latest_price_used"])
        self.assertIsNone(summary["capacity"])

    def test_bootstrap_keeps_ambiguous_trade_position_unknown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            build_time_order_ambiguous(temp_dir)
            config = next(
                item
                for item in get_bootstrap(temp_dir)["symbols"]
                if item["symbol"] == CANDIDATE_SYMBOL
            )
            self.assertIsNone(config["defaults"]["position_shares"])
            self.assertIsNone(config["defaults"]["avg_cost"])

    def test_full_review_requires_independent_position_for_ambiguous_trades(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            build_time_order_ambiguous(temp_dir)
            payload = self.fresh_payload(
                symbol=CANDIDATE_SYMBOL,
                stock_name="测试甲",
                use_local_records=True,
            )
            payload.pop("position_shares")
            with self.assertRaisesRegex(ValueError, "成交顺序待核对"):
                run_full_review(payload, temp_dir)

            result = run_full_review(
                {
                    **payload,
                    "position_shares": 900,
                    "avg_cost": 49.5,
                },
                temp_dir,
            )
            records = next(
                section for section in result["sections"] if section["id"] == "records"
            )
            self.assertTrue(
                any("成交顺序待核对" in item["text"] for item in records["items"])
            )

    def test_incomplete_ledger_never_becomes_zero_position_or_watchlist_cost(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = build_ledger_incomplete(temp_dir)["root"]
            pd.DataFrame(
                [
                    {
                        "symbol": CANDIDATE_SYMBOL,
                        "stock_name": "测试甲",
                        "entry_price": 88.88,
                    }
                ]
            ).to_csv(root / "watchlist.csv", index=False)

            config = next(
                item
                for item in get_bootstrap(root)["symbols"]
                if item["symbol"] == CANDIDATE_SYMBOL
            )
            self.assertIsNone(config["defaults"]["position_shares"])
            self.assertIsNone(config["defaults"]["avg_cost"])

            payload = self.fresh_payload(
                symbol=CANDIDATE_SYMBOL,
                stock_name="测试甲",
                use_local_records=True,
            )
            payload.pop("position_shares")
            with self.assertRaisesRegex(ValueError, "历史数据不完整"):
                run_full_review(payload, root)

            result = run_full_review(
                {**payload, "position_shares": 900},
                root,
            )
            records = next(
                section for section in result["sections"] if section["id"] == "records"
            )
            position = next(
                section for section in result["sections"] if section["id"] == "position"
            )
            self.assertTrue(
                any("历史数据不完整" in item["text"] for item in records["items"])
            )
            self.assertTrue(
                any("持仓 900 股" in item["text"] for item in position["items"])
            )
            self.assertFalse(
                any("成本 88.8800" in item["text"] for item in position["items"])
            )

    def test_legacy_basis_reference_is_identical_across_read_only_consumers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            records_dir = root / "records"
            records_dir.mkdir(parents=True)
            target = records_dir / "my_trades.csv"
            target.write_text(
                "trade_date,trade_time,symbol,stock_name,side,price,shares,"
                "gross_amount,fee,avg_cost_after_trade,realized_pnl,"
                "remaining_shares,account_total_asset,source,notes\n"
                "2026-04-01,09:30,600000.SH,测试股票,BUY,45.0000,100,"
                "4500.00,5.00,45.0000,0.00,100,140000,legacy,\n",
                encoding="utf-8",
            )
            before = target.read_bytes()

            canonical = list_trade_records(
                root, symbol="600000.SH", limit=1
            )["symbol_summaries"][0]
            first = _record_summary(root, "600000.SH", 46.0, True)
            second = _record_summary(root, "600000.SH", 46.0, True)

            self.assertEqual(canonical["basis_record_ref"], first["basis_record_ref"])
            self.assertEqual(first["basis_record_ref"], second["basis_record_ref"])
            self.assertEqual(before, target.read_bytes())

    def test_complete_review_has_required_ten_sections(self):
        result = run_full_review(self.fresh_payload(), self.review_root)
        self.assertEqual(10, len(result["sections"]))
        self.assertEqual("BUY_CANDIDATE", result["summary"]["technical_status"])
        self.assertEqual("允许进入计划讨论", result["summary"]["permission"])
        self.assertEqual(0, result["summary"]["data_blocker_count"])
        self.assertEqual(4, len(result["candidate_groups"]))

    def test_screening_provenance_is_visible_in_review_result(self):
        screening_context = {
            "screening_run_id": "screen-20260714",
            "screening_created_at": "2026-07-14T16:30:00+08:00",
            "snapshot_date": "2026-07-14",
            "candidate_priority": "HIGH",
            "candidate_score_ratio": 0.92,
        }
        result = run_full_review(
            self.fresh_payload(screening_context=screening_context),
            self.review_root,
        )
        identity = next(section for section in result["sections"] if section["id"] == "identity")
        self.assertEqual(screening_context, result["meta"]["screening_context"])
        self.assertTrue(any("候选筛选来源" in item["text"] for item in identity["items"]))

    def test_target_and_fundamental_context_are_reviewed_separately(self):
        result = run_full_review(
            self.fresh_payload(
                market_context="neutral",
                stop_loss_price=40,
                take_profit_price=53.14,
                fundamental_source_type="manual",
                fundamental_source="公司定期报告",
                fundamental_as_of=str(date.today()),
                fundamental_source_url="https://example.com/report",
                fundamental_summary="主营业务和订单情况可从定期报告复核",
            ),
            self.review_root,
        )
        self.assertIn(result["summary"]["target_status"], {"CONSISTENT", "REVIEW"})
        self.assertEqual("READY", result["summary"]["fundamental_status"])
        self.assertEqual("neutral", result["meta"]["market_context"])

    def test_structured_plan_text_is_visible_in_review_sections(self):
        buy_reason = "核心逻辑（行业与基本面逻辑）：订单变化可复核；技术位置（均线修复）：收盘站上 MA20；确认触发（收盘确认）：收盘后量价条件同时满足"
        sell_condition = "失效对象（技术结构失效）：收盘跌破关键位置；确认方式（收盘确认跌破）：收盘价低于事前位置；后续复盘：进入风险处理审查"
        result = run_full_review(
            self.fresh_payload(buy_reason=buy_reason, sell_condition=sell_condition),
            self.review_root,
        )
        identity = next(section for section in result["sections"] if section["id"] == "identity")
        early = next(section for section in result["sections"] if section["id"] == "early")
        self.assertTrue(any(buy_reason in item["text"] for item in identity["items"]))
        self.assertTrue(any(sell_condition in item["text"] for item in early["items"]))

    def test_emotion_rule_blocks_temporary_action(self):
        result = run_full_review(self.fresh_payload(watched_over_20m=True), self.review_root)
        self.assertEqual("不允许临时操作", result["summary"]["permission"])
        self.assertIn("交易行为规则不允许立即操作", result["summary"]["conclusion"])

    def test_high_position_blocks_temporary_action(self):
        result = run_full_review(self.fresh_payload(position_shares=1800), self.review_root)
        self.assertEqual("不允许临时操作", result["summary"]["permission"])
        self.assertEqual("重仓", result["summary"]["position_level"])

    def test_projected_high_risk_position_blocks_plan(self):
        result = run_full_review(
            self.fresh_payload(position_shares=800, planned_add_amount=30000, planned_position_pct=0.30),
            self.review_root,
        )
        self.assertEqual("不允许临时操作", result["summary"]["permission"])
        self.assertEqual(30000, result["summary"]["planned_add_amount"])
        self.assertGreater(result["summary"]["projected_position_pct"], result["summary"]["position_pct"])

    def test_high_risk_planned_total_position_blocks_plan(self):
        result = run_full_review(self.fresh_payload(planned_position_pct=0.45), self.review_root)
        self.assertEqual("不允许临时操作", result["summary"]["permission"])

    def test_zero_planned_position_is_not_replaced_by_default(self):
        result = run_full_review(self.fresh_payload(planned_position_pct=0), self.review_root)
        self.assertEqual(0, result["summary"]["planned_position_pct"])

    def test_historical_review_uses_review_date_as_freshness_reference(self):
        frame = sample_ohlcv()
        result = run_full_review(
            self.fresh_payload(
                csv_name="historical_snapshot.csv",
                csv_text=frame.to_csv(index=False),
                review_date="2026-06-13",
            ),
            self.review_root,
        )
        self.assertEqual(0, result["summary"]["data_blocker_count"])

    def test_less_than_sixty_rows_is_a_data_blocker(self):
        frame = sample_ohlcv().tail(45).copy()
        dates = pd.to_datetime(frame["date"])
        frame["date"] = dates + (pd.Timestamp(date.today()) - dates.iloc[-1])
        result = run_full_review(
            self.fresh_payload(csv_text=frame.to_csv(index=False), csv_name="short_history.csv"),
            self.review_root,
        )
        self.assertGreater(result["summary"]["data_blocker_count"], 0)
        self.assertEqual("继续观察", result["summary"]["permission"])

    def test_review_intent_must_match_position_state(self):
        with_position = run_full_review(self.fresh_payload(review_intent="open", position_shares=100), self.review_root)
        without_position = run_full_review(self.fresh_payload(review_intent="add", position_shares=0), self.review_root)
        self.assertEqual("不允许临时操作", with_position["summary"]["permission"])
        self.assertEqual("不允许临时操作", without_position["summary"]["permission"])

    def test_selection_rules_gate_plan_discussion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            # 用共享空实例做底：它带齐正式启动必需的配置与规则库。
            # 以前这里只放一份 config 就能跑通，仅仅是因为 evaluate_latest 在缺
            # registry 时会回退到代码所在目录的规则库——那正是本次重构消除的串根读取。
            root = build_empty_first_run(temp_dir)["root"]
            config_path = root / "config" / "strategy_rules.yaml"
            rules = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            rules["selection_rules"]["max_price"] = 10
            config_path.write_text(
                yaml.safe_dump(rules, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            result = run_full_review(self.fresh_payload(), root)
            self.assertEqual("FAIL", result["summary"]["selection_status"])
            self.assertEqual("继续观察", result["summary"]["permission"])
            self.assertIn("选股前置规则", result["summary"]["conclusion"])

    def test_demo_or_stale_data_cannot_enter_plan_discussion(self):
        result = run_full_review(
            {
                "symbol": "600760.SH",
                "timeframe": "day",
                "data_source": "local",
                "use_local_records": True,
                "review_intent": "daily",
                "decision_window": "after_close",
            },
            self.review_root,
        )
        self.assertEqual("继续观察", result["summary"]["permission"])
        self.assertGreater(result["summary"]["data_blocker_count"], 0)

    def test_explicit_stop_line_is_early_rule(self):
        result = run_full_review(
            self.fresh_payload(position_shares=800, avg_cost=44.5, current_price=40, stop_loss_price=41.5),
            self.review_root,
        )
        self.assertEqual("触发提前规则", result["summary"]["permission"])
        self.assertEqual(1, result["summary"]["early_trigger_count"])

    def test_no_position_does_not_trigger_planned_stop_line(self):
        result = run_full_review(
            self.fresh_payload(position_shares=0, avg_cost=44.5, current_price=40, stop_loss_price=41.5),
            self.review_root,
        )
        self.assertEqual(0, result["summary"]["early_trigger_count"])

    def test_trailing_stop_uses_holding_peak_and_saved_rule(self):
        result = run_full_review(
            self.fresh_payload(position_shares=800, current_price=45, holding_peak_price=51),
            self.review_root,
        )
        self.assertEqual("触发提前规则", result["summary"]["permission"])
        self.assertEqual(1, result["summary"]["early_trigger_count"])

    def test_holding_peak_cannot_be_below_current_price(self):
        with self.assertRaisesRegex(ValueError, "最高价不能低于当前价格"):
            run_full_review(
                self.fresh_payload(position_shares=800, current_price=45, holding_peak_price=44),
                self.review_root,
            )

    def test_single_planned_amount_respects_account_limit(self):
        result = run_full_review(
            self.fresh_payload(review_intent="add", planned_position_pct=0.30, planned_add_amount=15000),
            self.review_root,
        )
        self.assertEqual("不允许临时操作", result["summary"]["permission"])

    def test_single_planned_amount_respects_tranche_limit(self):
        result = run_full_review(
            self.fresh_payload(review_intent="open", planned_position_pct=0.20, planned_add_amount=12000),
            self.review_root,
        )
        self.assertEqual("不允许临时操作", result["summary"]["permission"])


if __name__ == "__main__":
    unittest.main()
