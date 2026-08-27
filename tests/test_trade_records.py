from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.personal_data import (
    PersonalDataError,
    append_trade_record,
    list_trade_records,
)


class TradeRecordTests(unittest.TestCase):
    def test_append_and_list_minimal_trade_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = {
                "request_id": "request-buy-0001",
                "trade_date": date.today().isoformat(),
                "trade_time": "15:00",
                "symbol": "600760",
                "stock_name": "中航沈飞",
                "side": "BUY",
                "price": 44.5,
                "shares": 200,
                "notes": "按计划记录",
            }

            created = append_trade_record(root, payload)
            listed = list_trade_records(root)

            self.assertFalse(created["deduplicated"])
            self.assertEqual(1, created["count"])
            self.assertEqual(1, listed["count"])
            self.assertEqual("600760.SH", listed["records"][0]["symbol"])
            self.assertEqual("BUY", listed["records"][0]["side"])
            self.assertEqual("首次买入", listed["records"][0]["operation"])
            self.assertEqual("历史未审查", listed["records"][0]["rule_status"])
            self.assertEqual(8_900, listed["records"][0]["gross_amount"])
            self.assertEqual("按计划记录", listed["records"][0]["notes"])

    def test_structured_checklist_derives_operation_discipline_and_emotion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = {
                "request_id": "request-buy-plan-0001",
                "trade_date": date.today().isoformat(),
                "symbol": "600760",
                "stock_name": "中航沈飞",
                "side": "BUY",
                "price": 44.5,
                "shares": 200,
                "reason_tags": ["RULE_TRIGGER", "CLOSE_CONFIRMATION"],
                "discipline_checks": [
                    "EXIT_PLAN",
                    "POSITION_CHECK",
                    "NOT_IMPULSIVE",
                ],
                "emotion_flags": [],
                "emotion_clear": True,
                "checklist_version": "1",
                "notes": "按收盘计划执行",
            }

            created = append_trade_record(root, payload)
            record = created["record"]

            self.assertEqual("首次买入", record["operation"])
            self.assertEqual("纪律核对完整", record["rule_status"])
            self.assertIn("无明显情绪驱动", record["emotion"])
            self.assertIn("来自当前规则筛选候选", record["notes"])
            self.assertIn("3 / 3 已确认", record["notes"])
            self.assertIn("按收盘计划执行", record["notes"])
            self.assertEqual("1", record["checklist_version"])
            self.assertEqual(
                ["RULE_TRIGGER", "CLOSE_CONFIRMATION"],
                record["reason_tags"],
            )
            self.assertEqual(
                ["EXIT_PLAN", "POSITION_CHECK", "NOT_IMPULSIVE"],
                record["discipline_checks"],
            )
            self.assertEqual([], record["emotion_flags"])
            self.assertTrue(record["emotion_clear"])

    def test_emotion_risk_is_saved_and_highlighted_instead_of_blocking_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            created = append_trade_record(
                root,
                {
                    "request_id": "request-buy-risk-0001",
                    "trade_date": date.today().isoformat(),
                    "symbol": "000001.SZ",
                    "side": "BUY",
                    "price": 10,
                    "shares": 100,
                    "reason_tags": [],
                    "discipline_checks": ["POSITION_CHECK"],
                    "emotion_flags": ["FOMO", "FEELING_ONLY"],
                    "emotion_clear": False,
                    "checklist_version": "1",
                },
            )

            self.assertEqual("情绪风险已记录", created["record"]["rule_status"])
            self.assertIn("害怕错过或追涨", created["record"]["emotion"])
            self.assertIn("操作理由只是感觉", created["record"]["emotion"])
            self.assertIn("未勾选明确依据", created["record"]["notes"])

    def test_calm_and_risk_emotion_checkboxes_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(PersonalDataError, "不能与情绪风险同时勾选"):
                append_trade_record(
                    Path(temp_dir),
                    {
                        "request_id": "request-emotion-bad-0001",
                        "trade_date": date.today().isoformat(),
                        "symbol": "600760",
                        "side": "BUY",
                        "price": 44.5,
                        "shares": 100,
                        "reason_tags": ["RULE_TRIGGER"],
                        "discipline_checks": [],
                        "emotion_flags": ["FOMO"],
                        "emotion_clear": True,
                        "checklist_version": "1",
                    },
                )

    def test_request_id_makes_double_submit_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = {
                "request_id": "request-buy-0002",
                "trade_date": date.today().isoformat(),
                "symbol": "000001.SZ",
                "side": "买入",
                "price": 10,
                "shares": 100,
            }

            first = append_trade_record(root, payload)
            second = append_trade_record(root, payload)

            self.assertFalse(first["deduplicated"])
            self.assertTrue(second["deduplicated"])
            self.assertEqual(1, list_trade_records(root)["count"])

    def test_new_request_id_repeats_the_same_fact_so_receipts_must_be_reconciled(self):
        # 去重键只有 source == "panel_manual:{request_id}"，没有内容级去重。
        # 因此回执不确定时若换发新编号重录，同一笔已发生事实会被追加第二条。
        # 闭环只能由前端「先按 request_id 核对、再决定是否重试」承担；
        # 服务端不能改成内容去重，那会误伤同日同价同量的真实分批成交。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fact = {
                "trade_date": date.today().isoformat(),
                "trade_time": "14:30",
                "symbol": "000001.SZ",
                "side": "买入",
                "price": 10,
                "shares": 100,
            }

            first = append_trade_record(root, {**fact, "request_id": "receipt-lost-0001"})
            second = append_trade_record(root, {**fact, "request_id": "receipt-lost-0002"})
            listed = list_trade_records(root)

            self.assertFalse(first["deduplicated"])
            self.assertFalse(second["deduplicated"])
            self.assertEqual(2, listed["count"])
            # 每条记录都回传 request_id，前端据此核对是否已落库，无需新增接口或字段。
            self.assertEqual(
                {"receipt-lost-0001", "receipt-lost-0002"},
                {record["request_id"] for record in listed["records"]},
            )

    def test_operation_point_is_derived_from_recorded_position_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            common = {
                "trade_date": date.today().isoformat(),
                "symbol": "600760",
                "price": 45,
            }
            first = append_trade_record(
                root,
                {**common, "request_id": "operation-point-0001", "side": "BUY", "shares": 200},
            )
            add = append_trade_record(
                root,
                {**common, "request_id": "operation-point-0002", "side": "BUY", "shares": 100},
            )
            reduce = append_trade_record(
                root,
                {**common, "request_id": "operation-point-0003", "side": "SELL", "shares": 50},
            )
            close = append_trade_record(
                root,
                {**common, "request_id": "operation-point-0004", "side": "SELL", "shares": 250},
            )

            self.assertEqual("首次买入", first["record"]["operation"])
            self.assertEqual("加仓", add["record"]["operation"])
            self.assertEqual("减仓", reduce["record"]["operation"])
            self.assertEqual("清仓", close["record"]["operation"])

    def test_invalid_trade_does_not_write_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "records" / "my_trades.csv"
            with self.assertRaises(PersonalDataError):
                append_trade_record(
                    root,
                    {
                        "request_id": "request-bad-0001",
                        "trade_date": date.today().isoformat(),
                        "symbol": "600760",
                        "side": "BUY",
                        "price": 0,
                        "shares": 100,
                    },
                )
            self.assertFalse(target.exists())

    def test_sell_requires_and_cannot_exceed_recorded_holding(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            common = {
                "trade_date": date.today().isoformat(),
                "symbol": "600760",
                "price": 45,
            }
            with self.assertRaisesRegex(PersonalDataError, "历史持仓"):
                append_trade_record(
                    root,
                    {
                        **common,
                        "request_id": "request-sell-0001",
                        "side": "SELL",
                        "shares": 100,
                    },
                )

            append_trade_record(
                root,
                {
                    **common,
                    "request_id": "request-buy-0003",
                    "side": "BUY",
                    "shares": 100,
                },
            )
            with self.assertRaisesRegex(PersonalDataError, "超过记录中的持仓"):
                append_trade_record(
                    root,
                    {
                        **common,
                        "request_id": "request-sell-0002",
                        "side": "SELL",
                        "shares": 200,
                    },
                )
            self.assertEqual(1, list_trade_records(root)["count"])


if __name__ == "__main__":
    unittest.main()
