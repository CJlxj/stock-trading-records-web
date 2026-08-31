from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from src.personal_data import (
    PersonalDataError,
    TRADE_COLUMNS,
    append_trade_record,
    list_trade_records,
)


def write_trade_history(root: Path, rows: list[dict[str, str]]) -> Path:
    """直接落一份历史台账，用来模拟手改过或早期格式的 records/my_trades.csv。"""
    target = root / "records" / "my_trades.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRADE_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in TRADE_COLUMNS})
    return target


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
                {**common, "request_id": "operation-point-0001", "trade_time": "09:30", "side": "BUY", "shares": 200},
            )
            add = append_trade_record(
                root,
                {**common, "request_id": "operation-point-0002", "trade_time": "10:00", "side": "BUY", "shares": 100},
            )
            reduce = append_trade_record(
                root,
                {**common, "request_id": "operation-point-0003", "trade_time": "13:00", "side": "SELL", "shares": 50},
            )
            close = append_trade_record(
                root,
                {**common, "request_id": "operation-point-0004", "trade_time": "14:30", "side": "SELL", "shares": 250},
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
                    "trade_time": "09:30",
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
                        "trade_time": "14:00",
                        "side": "SELL",
                        "shares": 200,
                    },
                )
            self.assertEqual(1, list_trade_records(root)["count"])


class TradeLedgerFactTests(unittest.TestCase):
    """事实台账：成交时间、成交后余额与成本、四类归类、按股票汇总、缺字段标记。"""

    def test_unknown_trade_time_is_kept_unknown_instead_of_submit_time(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            created = append_trade_record(
                root,
                {
                    "request_id": "unknown-time-0001",
                    "trade_date": date.today().isoformat(),
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 45,
                    "shares": 100,
                },
            )
            stored = (root / "records" / "my_trades.csv").read_text(
                encoding="utf-8-sig"
            )
            row = next(iter(csv.DictReader(stored.splitlines())))

            self.assertEqual("", row["trade_time"])
            self.assertFalse(created["record"]["trade_time_known"])
            self.assertEqual("未记录", created["record"]["trade_time_label"])
            summary = list_trade_records(root)["symbol_summaries"][0]
            self.assertEqual("ORDER_KNOWN", summary["order_status"])
            self.assertEqual("顺序明确", summary["order_status_label"])
            self.assertEqual("unknown-time-0001", summary["basis_record_ref"])
            # 落盘内容里不允许出现提交时刻；台账里唯一带冒号的字段就是成交时间。
            self.assertNotIn(datetime.now().strftime("%H:%M"), stored)
            self.assertNotIn(":", row["trade_time"])

    def test_backfilled_trade_time_is_saved_exactly_as_reported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            created = append_trade_record(
                root,
                {
                    "request_id": "backfill-time-0001",
                    "trade_date": date.today().isoformat(),
                    "trade_time": "09:35",
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 45,
                    "shares": 100,
                },
            )
            record = created["record"]

            self.assertEqual("09:35:00", record["trade_time"])
            self.assertEqual("09:35", record["trade_time_label"])
            self.assertTrue(record["trade_time_known"])

    def test_unknown_time_trade_can_follow_a_timed_trade_on_the_same_day(self):
        # 时间未知不阻止保存。但同一天里有的记了时间、有的没记时，
        # 「没记的那笔排在当日最后」只是内部排序约定，不是事实，
        # 所以不能据此算出剩余股数与成本——否则先前那笔已记录的成交会被整笔跳过。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            today = date.today().isoformat()
            append_trade_record(
                root,
                {
                    "request_id": "same-day-timed-0001",
                    "trade_date": today,
                    "trade_time": "14:30",
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 45,
                    "shares": 100,
                    "fee": 0,
                },
            )
            later = append_trade_record(
                root,
                {
                    "request_id": "same-day-unknown-0002",
                    "trade_date": today,
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 46,
                    "shares": 100,
                    "fee": 0,
                },
            )

            # 已经发生的成交照样保存。
            self.assertFalse(later["deduplicated"])
            listed = list_trade_records(root)
            self.assertEqual(2, listed["count"])
            self.assertEqual(100, later["record"]["shares"])
            self.assertEqual(46.0, later["record"]["price"])
            # 但先后顺序无从确认，所以不给出看似精确的数字。
            self.assertEqual("未归类", later["record"]["operation_label"])
            self.assertIsNone(later["record"]["remaining_shares"])
            self.assertIsNone(later["record"]["avg_cost_after_trade"])
            self.assertEqual(
                ["", "14:30:00"],
                [record["trade_time"] for record in listed["records"]],
            )
            summary = listed["symbol_summaries"][0]
            self.assertEqual("ORDER_AMBIGUOUS", summary["order_status"])
            self.assertIsNone(summary["basis_record_ref"])
            self.assertIsNone(summary["last_operation_label"])

    def test_each_trade_reports_remaining_average_cost_and_realized_pnl(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            today = date.today().isoformat()
            bought = append_trade_record(
                root,
                {
                    "request_id": "ledger-buy-0001",
                    "trade_date": today,
                    "trade_time": "09:30",
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 45,
                    "shares": 200,
                    "fee": 20,
                },
            )
            sold = append_trade_record(
                root,
                {
                    "request_id": "ledger-sell-0002",
                    "trade_date": today,
                    "trade_time": "14:00",
                    "symbol": "600760",
                    "side": "SELL",
                    "price": 50,
                    "shares": 50,
                    "fee": 5,
                },
            )

            # 买入费用计入成本：(200 × 45 + 20) / 200 = 45.1
            self.assertEqual(200, bought["record"]["remaining_shares"])
            self.assertEqual(45.1, bought["record"]["avg_cost_after_trade"])
            self.assertEqual(0.0, bought["record"]["realized_pnl"])
            # 卖出不动平均成本，费用计入已实现盈亏：(50 − 45.1) × 50 − 5 = 240
            self.assertEqual(150, sold["record"]["remaining_shares"])
            self.assertEqual(45.1, sold["record"]["avg_cost_after_trade"])
            self.assertEqual(240.0, sold["record"]["realized_pnl"])
            for record in list_trade_records(root)["records"]:
                self.assertTrue(record["complete"])
                self.assertEqual([], record["missing_fields"])
                self.assertEqual("", record["incomplete_label"])

    def test_four_operation_classes_come_from_the_recorded_share_counts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            today = date.today().isoformat()
            steps = [
                ("class-0001", "09:30", "BUY", 200, "首次买入", "FIRST_BUY", 200),
                ("class-0002", "10:00", "BUY", 100, "加仓", "ADD", 300),
                ("class-0003", "11:00", "SELL", 100, "减仓", "REDUCE", 200),
                ("class-0004", "14:00", "SELL", 200, "清仓", "CLOSE", 0),
            ]
            for request_id, moment, side, shares, label, code, remaining in steps:
                created = append_trade_record(
                    root,
                    {
                        "request_id": request_id,
                        "trade_date": today,
                        "trade_time": moment,
                        "symbol": "600760",
                        "side": side,
                        "price": 45,
                        "shares": shares,
                    },
                )
                self.assertEqual(label, created["record"]["operation_label"], request_id)
                self.assertEqual(code, created["record"]["operation_class"], request_id)
                self.assertEqual(
                    remaining, created["record"]["remaining_shares"], request_id
                )

    def test_imported_history_is_classified_from_facts_not_broker_wording(self):
        # 券商摘要只写「证券买入 / 证券卖出」；四类归类必须由股数事实推出。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_trade_history(
                root,
                [
                    {
                        "trade_date": "2026-08-20",
                        "trade_time": "09:31:00",
                        "symbol": "600760.SH",
                        "side": "BUY",
                        "operation": "证券买入",
                        "price": "45",
                        "shares": "200",
                        "remaining_shares": "200",
                        "avg_cost_after_trade": "45.0000",
                        "realized_pnl": "0.00",
                    },
                    {
                        "trade_date": "2026-08-21",
                        "trade_time": "10:05:00",
                        "symbol": "600760.SH",
                        "side": "BUY",
                        "operation": "证券买入",
                        "price": "48",
                        "shares": "100",
                        "remaining_shares": "300",
                        "avg_cost_after_trade": "46.0000",
                        "realized_pnl": "0.00",
                    },
                    {
                        "trade_date": "2026-08-24",
                        "trade_time": "10:05:00",
                        "symbol": "600760.SH",
                        "side": "SELL",
                        "operation": "证券卖出",
                        "price": "50",
                        "shares": "100",
                        "remaining_shares": "200",
                        "avg_cost_after_trade": "46.0000",
                        "realized_pnl": "400.00",
                    },
                    {
                        "trade_date": "2026-08-25",
                        "trade_time": "10:05:00",
                        "symbol": "600760.SH",
                        "side": "SELL",
                        "operation": "证券卖出",
                        "price": "52",
                        "shares": "200",
                        "remaining_shares": "0",
                        "avg_cost_after_trade": "0.0000",
                        "realized_pnl": "1200.00",
                    },
                ],
            )
            listed = list_trade_records(root)
            records = listed["records"]

            # 列表是最新在前，所以四类倒序出现。
            self.assertEqual(
                ["清仓", "减仓", "加仓", "首次买入"],
                [record["operation_label"] for record in records],
            )
            self.assertEqual(
                ["CLOSE", "REDUCE", "ADD", "FIRST_BUY"],
                [record["operation_class"] for record in records],
            )
            self.assertEqual(
                ["证券卖出", "证券卖出", "证券买入", "证券买入"],
                [record["operation"] for record in records],
            )
            self.assertEqual("清仓", listed["symbol_summaries"][0]["last_operation_label"])

    def test_symbol_summary_says_how_much_is_left_and_from_which_trade(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            today = date.today().isoformat()
            for request_id, moment, side, price, shares in [
                ("summary-0001", "09:30", "BUY", 45, 200),
                ("summary-0002", "10:00", "BUY", 48, 100),
                ("summary-0003", "14:00", "SELL", 50, 150),
            ]:
                append_trade_record(
                    root,
                    {
                        "request_id": request_id,
                        "trade_date": today,
                        "trade_time": moment,
                        "symbol": "600760",
                        "side": side,
                        "price": price,
                        "shares": shares,
                    },
                )
            summaries = list_trade_records(root)["symbol_summaries"]

            self.assertEqual(1, len(summaries))
            summary = summaries[0]
            self.assertEqual("600760.SH", summary["symbol"])
            self.assertEqual(3, summary["trade_count"])
            self.assertEqual(150, summary["remaining_shares"])
            # (200 × 45 + 100 × 48) / 300 = 46
            self.assertEqual(46.0, summary["avg_cost"])
            self.assertEqual(today, summary["last_trade_date"])
            self.assertEqual("14:00", summary["last_trade_time_label"])
            self.assertEqual("减仓", summary["last_operation_label"])
            self.assertEqual("ORDER_KNOWN", summary["order_status"])
            self.assertEqual("顺序明确", summary["order_status_label"])
            self.assertEqual("summary-0003", summary["basis_record_ref"])
            self.assertTrue(summary["complete"])
            self.assertEqual("", summary["incomplete_label"])

    def test_summary_covers_every_trade_even_beyond_the_record_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_trade_history(
                root,
                [
                    {
                        "trade_date": f"2026-08-{day:02d}",
                        "trade_time": "09:31:00",
                        "symbol": "600760.SH",
                        "side": "BUY",
                        "price": "45",
                        "shares": "100",
                        "remaining_shares": str(100 * index),
                        "avg_cost_after_trade": "45.0000",
                        "realized_pnl": "0.00",
                    }
                    for index, day in enumerate(range(10, 14), start=1)
                ],
            )
            listed = list_trade_records(root, limit=1)

            self.assertEqual(1, len(listed["records"]))
            self.assertEqual(4, listed["count"])
            self.assertEqual(400, listed["symbol_summaries"][0]["remaining_shares"])
            self.assertEqual(4, listed["symbol_summaries"][0]["trade_count"])

    def test_missing_history_fields_are_flagged_without_any_backfill(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_trade_history(
                root,
                [
                    {
                        "trade_date": "2026-08-20",
                        "trade_time": "",
                        "symbol": "600760.SH",
                        "side": "BUY",
                        "price": "45",
                        "shares": "200",
                        "remaining_shares": "",
                        "avg_cost_after_trade": "",
                        "realized_pnl": "",
                    }
                ],
            )
            listed = list_trade_records(root)
            record = listed["records"][0]
            summary = listed["symbol_summaries"][0]

            self.assertIsNone(record["remaining_shares"])
            self.assertIsNone(record["avg_cost_after_trade"])
            self.assertIsNone(record["realized_pnl"])
            self.assertFalse(record["complete"])
            self.assertEqual("历史数据不完整", record["incomplete_label"])
            self.assertEqual(
                ["成交后剩余股数", "成交后平均成本", "该笔已实现盈亏"],
                record["missing_fields"],
            )
            self.assertEqual("未归类", record["operation_label"])
            self.assertEqual("未记录", record["trade_time_label"])
            self.assertEqual(1, listed["incomplete_record_count"])

            # 汇总受影响时同样必须标记，不能拿买入股数当剩余股数。
            self.assertIsNone(summary["remaining_shares"])
            self.assertIsNone(summary["avg_cost"])
            self.assertFalse(summary["complete"])
            self.assertEqual("历史数据不完整", summary["incomplete_label"])
            self.assertIn(
                "最后一笔没有记录成交后剩余股数", summary["incomplete_reasons"]
            )

    def test_summary_stays_flagged_when_only_an_earlier_trade_is_incomplete(self):
        # 最后一笔完整也不代表汇总可信：中间缺字段就说不清持仓怎么变过来的。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_trade_history(
                root,
                [
                    {
                        "trade_date": "2026-08-20",
                        "trade_time": "09:31:00",
                        "symbol": "600760.SH",
                        "side": "BUY",
                        "price": "45",
                        "shares": "200",
                        "remaining_shares": "",
                        "avg_cost_after_trade": "",
                        "realized_pnl": "",
                    },
                    {
                        "trade_date": "2026-08-21",
                        "trade_time": "10:05:00",
                        "symbol": "600760.SH",
                        "side": "BUY",
                        "price": "46",
                        "shares": "100",
                        "remaining_shares": "300",
                        "avg_cost_after_trade": "45.3333",
                        "realized_pnl": "0.00",
                    },
                ],
            )
            listed = list_trade_records(root)
            summary = listed["symbol_summaries"][0]

            self.assertEqual(300, summary["remaining_shares"])
            self.assertEqual(45.3333, summary["avg_cost"])
            self.assertFalse(summary["complete"])
            self.assertEqual("历史数据不完整", summary["incomplete_label"])
            self.assertIn("1 笔成交缺少必要字段", summary["incomplete_reasons"])
            self.assertEqual(1, listed["incomplete_record_count"])

    def test_emotion_risk_record_still_reports_its_ledger_facts(self):
        # 纪律不完整或有情绪风险，成交事实照样如实保存和显示。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            created = append_trade_record(
                root,
                {
                    "request_id": "risk-ledger-0001",
                    "trade_date": date.today().isoformat(),
                    "trade_time": "13:45",
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 45,
                    "shares": 100,
                    "reason_tags": [],
                    "discipline_checks": [],
                    "emotion_flags": ["FOMO"],
                    "emotion_clear": False,
                    "checklist_version": "1",
                },
            )
            record = created["record"]
            summary = list_trade_records(root)["symbol_summaries"][0]

            self.assertEqual("情绪风险已记录", record["rule_status"])
            self.assertEqual("首次买入", record["operation_label"])
            self.assertEqual(100, record["remaining_shares"])
            self.assertEqual(45.0, record["avg_cost_after_trade"])
            self.assertTrue(record["complete"])
            self.assertEqual(100, summary["remaining_shares"])
            self.assertTrue(summary["complete"])


class TradeLedgerIntegrityTests(unittest.TestCase):
    """数据完整性收口：前序不完整不得生成数字，费用三态，不完整状态传到汇总。"""

    TODAY = date.today().isoformat()

    def _incomplete_history(self, root: Path) -> None:
        """一笔来自「无成交后字段」券商导出的历史行——正是导入路径现在会写出的形状。"""
        write_trade_history(
            root,
            [
                {
                    "trade_date": "2026-08-05",
                    "trade_time": "09:31:00",
                    "symbol": "600760.SH",
                    "stock_name": "中航沈飞",
                    "side": "BUY",
                    "operation": "证券买入",
                    "price": "44.5",
                    "shares": "200",
                    "gross_amount": "8900.00",
                    "source": "panel_import",
                }
            ],
        )

    def test_incomplete_history_still_saves_the_trade_but_invents_no_numbers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._incomplete_history(root)

            created = append_trade_record(
                root,
                {
                    "request_id": "after-gap-buy-0001",
                    "trade_date": self.TODAY,
                    "trade_time": "10:20",
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 50,
                    "shares": 100,
                    "fee": 6,
                },
            )
            record = created["record"]

            # 已经发生的成交必须如实保存这六项。
            self.assertEqual(self.TODAY, record["trade_date"])
            self.assertEqual("10:20", record["trade_time_label"])
            self.assertEqual("600760.SH", record["symbol"])
            self.assertEqual("BUY", record["side"])
            self.assertEqual(50.0, record["price"])
            self.assertEqual(100, record["shares"])
            # 但不得假定前序成本为 0，也不得生成看似精确的剩余股数与成本。
            self.assertIsNone(record["remaining_shares"])
            self.assertIsNone(record["avg_cost_after_trade"])
            self.assertEqual("未归类", record["operation_label"])
            summary = list_trade_records(root)["symbol_summaries"][0]
            self.assertEqual("ORDER_KNOWN", summary["order_status"])
            self.assertEqual("after-gap-buy-0001", summary["basis_record_ref"])
            self.assertEqual("未归类", summary["last_operation_label"])
            self.assertEqual("", record["operation_class"])
            self.assertFalse(record["complete"])
            self.assertEqual("历史数据不完整", record["incomplete_label"])
            # 落盘的单元格必须真的是空，不是 "0"。
            stored = next(
                row
                for row in csv.DictReader(
                    (root / "records" / "my_trades.csv")
                    .read_text(encoding="utf-8-sig")
                    .splitlines()
                )
                if row["source"] == "panel_manual:after-gap-buy-0001"
            )
            self.assertEqual("", stored["remaining_shares"])
            self.assertEqual("", stored["avg_cost_after_trade"])
            self.assertEqual("", stored["operation"])
            self.assertEqual("", stored["position_market_value"])

    def test_incomplete_history_does_not_fabricate_realized_pnl_on_a_sell(self):
        # 假定前序成本为 0 会把整笔卖出金额当成盈利，这是最危险的一处。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._incomplete_history(root)

            created = append_trade_record(
                root,
                {
                    "request_id": "after-gap-sell-0001",
                    "trade_date": self.TODAY,
                    "trade_time": "14:05",
                    "symbol": "600760",
                    "side": "SELL",
                    "price": 50,
                    "shares": 100,
                    "fee": 6,
                },
            )
            record = created["record"]

            self.assertEqual(100, record["shares"])
            self.assertIsNone(record["realized_pnl"])
            self.assertIsNone(record["avg_cost_after_trade"])
            self.assertIsNone(record["remaining_shares"])
            self.assertEqual("未归类", record["operation_label"])
            # 5000 元就是「假定前序成本为 0」会算出来的盈亏。成交金额确实是 5000，
            # 所以必须精确检查 realized_pnl 这一格，而不是全文搜索。
            stored = next(
                row
                for row in csv.DictReader(
                    (root / "records" / "my_trades.csv")
                    .read_text(encoding="utf-8-sig")
                    .splitlines()
                )
                if row["source"] == "panel_manual:after-gap-sell-0001"
            )
            self.assertEqual("5000.00", stored["gross_amount"])
            self.assertEqual("", stored["realized_pnl"])
            self.assertEqual("", stored["avg_cost_after_trade"])
            self.assertEqual("", stored["remaining_shares"])

    def test_sell_is_not_blocked_by_a_holding_number_that_was_never_recorded(self):
        # 前序未知时不得用一个从未被记录的数字否决一笔已经发生的成交。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._incomplete_history(root)

            created = append_trade_record(
                root,
                {
                    "request_id": "after-gap-oversell-0001",
                    "trade_date": self.TODAY,
                    "symbol": "600760",
                    "side": "SELL",
                    "price": 50,
                    "shares": 900,
                    "fee": 0,
                },
            )

            self.assertFalse(created["deduplicated"])
            self.assertEqual(900, created["record"]["shares"])
            self.assertIsNone(created["record"]["remaining_shares"])

    def test_known_history_still_blocks_a_sell_that_the_ledger_contradicts(self):
        # 前序确实已知时，原有的两道数量校验必须照旧生效。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(PersonalDataError, "历史持仓"):
                append_trade_record(
                    root,
                    {
                        "request_id": "known-empty-sell-0001",
                        "trade_date": self.TODAY,
                        "symbol": "600760",
                        "side": "SELL",
                        "price": 50,
                        "shares": 100,
                    },
                )
            append_trade_record(
                root,
                {
                    "request_id": "known-buy-0001",
                    "trade_date": self.TODAY,
                    "trade_time": "09:31",
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 45,
                    "shares": 100,
                    "fee": 5,
                },
            )
            with self.assertRaisesRegex(PersonalDataError, "超过记录中的持仓 100 股"):
                append_trade_record(
                    root,
                    {
                        "request_id": "known-oversell-0001",
                        "trade_date": self.TODAY,
                        "trade_time": "14:00",
                        "symbol": "600760",
                        "side": "SELL",
                        "price": 50,
                        "shares": 200,
                    },
                )

    def test_blank_fee_is_unknown_and_never_silently_treated_as_zero(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            created = append_trade_record(
                root,
                {
                    "request_id": "fee-blank-0001",
                    "trade_date": self.TODAY,
                    "trade_time": "09:31",
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 45,
                    "shares": 200,
                    "fee": "",
                },
            )
            record = created["record"]
            stored = next(
                iter(
                    csv.DictReader(
                        (root / "records" / "my_trades.csv")
                        .read_text(encoding="utf-8-sig")
                        .splitlines()
                    )
                )
            )

            self.assertEqual("", stored["fee"])
            self.assertIsNone(record["fee"])
            self.assertFalse(record["fee_reported"])
            self.assertFalse(record["fee_complete"])
            self.assertEqual("不含未知费用", record["fee_note"])
            self.assertFalse(record["exact"])
            # 数值仍然给出，但它是不含费用的：(200 × 45) / 200 = 45
            self.assertEqual(45.0, record["avg_cost_after_trade"])
            self.assertEqual(200, record["remaining_shares"])
            # 缺字段与费用未知是两件事，不得混为一谈。
            self.assertTrue(record["complete"])
            self.assertEqual("", record["incomplete_label"])
            # 未知费用不得让净额显示成精确清算金额。
            self.assertEqual("", stored["net_amount"])
            # 三项明细从未采集过，一律留空，不再硬写 0.00。
            for column in ("stamp_tax", "transfer_fee", "other_fee"):
                self.assertEqual("", stored[column], column)

    def test_fee_explicitly_zero_means_there_really_was_no_fee(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            created = append_trade_record(
                root,
                {
                    "request_id": "fee-zero-0001",
                    "trade_date": self.TODAY,
                    "trade_time": "09:31",
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 45,
                    "shares": 200,
                    "fee": 0,
                },
            )
            record = created["record"]
            stored = next(
                iter(
                    csv.DictReader(
                        (root / "records" / "my_trades.csv")
                        .read_text(encoding="utf-8-sig")
                        .splitlines()
                    )
                )
            )

            # 明确的 0 落盘为 "0"（不是空），与「未填」清楚区分开。
            self.assertEqual("0", stored["fee"])
            self.assertEqual(0.0, record["fee"])
            self.assertTrue(record["fee_reported"])
            self.assertTrue(record["fee_complete"])
            self.assertEqual("", record["fee_note"])
            self.assertTrue(record["exact"])
            self.assertEqual(45.0, record["avg_cost_after_trade"])
            self.assertEqual("-9000.00", stored["net_amount"])

    def test_fee_with_an_amount_is_folded_into_cost_by_the_existing_rule(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            created = append_trade_record(
                root,
                {
                    "request_id": "fee-amount-0001",
                    "trade_date": self.TODAY,
                    "trade_time": "09:31",
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 45,
                    "shares": 200,
                    "fee": 20,
                },
            )
            record = created["record"]

            # 沿用既有口径：买入费用计入成本 → (200 × 45 + 20) / 200 = 45.1
            self.assertEqual(20.0, record["fee"])
            self.assertTrue(record["fee_reported"])
            self.assertTrue(record["fee_complete"])
            self.assertEqual(45.1, record["avg_cost_after_trade"])
            self.assertTrue(record["exact"])

    def test_unknown_fee_marks_every_later_trade_of_that_symbol(self):
        # 早先一笔费用未知，后续摊薄成本同样不含它——标记必须沿链条传递。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            append_trade_record(
                root,
                {
                    "request_id": "fee-chain-blank-0001",
                    "trade_date": self.TODAY,
                    "trade_time": "09:31",
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 45,
                    "shares": 200,
                },
            )
            later = append_trade_record(
                root,
                {
                    "request_id": "fee-chain-known-0002",
                    "trade_date": self.TODAY,
                    "trade_time": "14:00",
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 48,
                    "shares": 100,
                    "fee": 5,
                },
            )

            # 这一笔自己的费用是已知的，但它的成本基数来自费用未知的那一笔。
            self.assertTrue(later["record"]["fee_reported"])
            self.assertFalse(later["record"]["fee_complete"])
            self.assertEqual("不含未知费用", later["record"]["fee_note"])
            self.assertFalse(later["record"]["exact"])

            listed = list_trade_records(root)
            summary = listed["symbol_summaries"][0]
            self.assertFalse(summary["fee_complete"])
            self.assertEqual("费用未知", summary["fee_label"])
            self.assertIn(
                "成交账本平均成本与已实现盈亏不含未知费用", summary["fee_reasons"]
            )
            self.assertFalse(summary["exact"])
            # 费用未知不等于缺字段：汇总的完整性标记仍然是干净的。
            self.assertTrue(summary["complete"])
            self.assertEqual("", summary["incomplete_label"])
            self.assertEqual(2, listed["fee_unknown_record_count"])
            self.assertEqual(0, listed["incomplete_record_count"])

    def test_incomplete_state_reaches_the_symbol_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._incomplete_history(root)
            append_trade_record(
                root,
                {
                    "request_id": "summary-gap-0001",
                    "trade_date": self.TODAY,
                    "trade_time": "10:20",
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 50,
                    "shares": 100,
                    "fee": 6,
                },
            )
            listed = list_trade_records(root)
            summary = listed["symbol_summaries"][0]

            self.assertEqual(2, summary["trade_count"])
            self.assertIsNone(summary["remaining_shares"])
            self.assertIsNone(summary["avg_cost"])
            self.assertFalse(summary["complete"])
            self.assertFalse(summary["exact"])
            self.assertEqual("历史数据不完整", summary["incomplete_label"])
            self.assertIn("2 笔成交缺少必要字段", summary["incomplete_reasons"])
            self.assertIn(
                "最后一笔没有记录成交后剩余股数", summary["incomplete_reasons"]
            )
            self.assertEqual(2, listed["incomplete_record_count"])

    def test_a_timed_trade_is_not_rejected_by_an_unknown_time_on_the_same_day(self):
        # 未知时间没有业务时刻，不能拿展示位置去否决已发生成交。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            append_trade_record(
                root,
                {
                    "request_id": "order-unknown-first-0001",
                    "trade_date": self.TODAY,
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 45,
                    "shares": 200,
                    "fee": 0,
                },
            )
            created = append_trade_record(
                root,
                {
                    "request_id": "order-timed-second-0002",
                    "trade_date": self.TODAY,
                    "trade_time": "14:00",
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 46,
                    "shares": 100,
                    "fee": 0,
                },
            )
            record = created["record"]

            self.assertFalse(created["deduplicated"])
            listed = list_trade_records(root)
            self.assertEqual(2, listed["count"])
            # 同一天内先后顺序无从确认，所以不给出看似精确的剩余股数与成本。
            self.assertIsNone(record["remaining_shares"])
            self.assertIsNone(record["avg_cost_after_trade"])
            self.assertEqual("未归类", record["operation_label"])
            summary = listed["symbol_summaries"][0]
            self.assertEqual("ORDER_AMBIGUOUS", summary["order_status"])
            self.assertIsNone(summary["basis_record_ref"])
            self.assertIsNone(summary["last_operation_label"])

    def test_a_definitely_earlier_trade_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            append_trade_record(
                root,
                {
                    "request_id": "order-late-0001",
                    "trade_date": self.TODAY,
                    "trade_time": "14:00",
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 45,
                    "shares": 200,
                    "fee": 0,
                },
            )
            with self.assertRaisesRegex(PersonalDataError, "只能追加该股票最新的一次操作"):
                append_trade_record(
                    root,
                    {
                        "request_id": "order-early-0002",
                        "trade_date": self.TODAY,
                        "trade_time": "09:31",
                        "symbol": "600760",
                        "side": "BUY",
                        "price": 44,
                        "shares": 100,
                        "fee": 0,
                    },
                )

    def test_the_fee_caveat_lands_on_exactly_the_fields_fees_affect(self):
        # 买入费用计入成本 → 成本带限定语；买入的已实现盈亏恒为 0，与费用无关 → 不带。
        # 卖出费用计入已实现盈亏 → 卖出那一格必须带。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bought = append_trade_record(
                root,
                {
                    "request_id": "caveat-buy-0001",
                    "trade_date": self.TODAY,
                    "trade_time": "09:31",
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 45,
                    "shares": 200,
                },
            )
            sold = append_trade_record(
                root,
                {
                    "request_id": "caveat-sell-0002",
                    "trade_date": self.TODAY,
                    "trade_time": "14:00",
                    "symbol": "600760",
                    "side": "SELL",
                    "price": 50,
                    "shares": 100,
                },
            )

            self.assertEqual("不含未知费用", bought["record"]["cost_fee_note"])
            self.assertEqual(0.0, bought["record"]["realized_pnl"])
            self.assertEqual("", bought["record"]["realized_fee_note"])

            self.assertEqual("不含未知费用", sold["record"]["cost_fee_note"])
            self.assertEqual("不含未知费用", sold["record"]["realized_fee_note"])
            # (50 − 45) × 100 = 500，不含未知费用
            self.assertEqual(500.0, sold["record"]["realized_pnl"])
            self.assertEqual("减仓", sold["record"]["operation_label"])

    def test_a_reverse_order_broker_export_cannot_flip_the_summary(self):
        # 券商常按倒序导出、且不带成交时间列。同日一买一卖时，
        # 「最后一笔」会随 CSV 行序翻转——绝不能据此断言剩余股数和成本。
        def rows(order: list[tuple[str, str, str, str]]) -> list[dict[str, str]]:
            return [
                {
                    "trade_date": "2026-04-01",
                    "trade_time": "",
                    "symbol": "600000.SH",
                    "side": side,
                    "price": price,
                    "shares": shares,
                    "remaining_shares": remaining,
                    "avg_cost_after_trade": "10.0000" if remaining != "0" else "0.0000",
                    "realized_pnl": "0.00" if side == "BUY" else "989.98",
                    "source": "panel_import",
                }
                for side, price, shares, remaining in order
            ]

        newest_first = [("SELL", "11", "1000", "0"), ("BUY", "10", "1000", "1000")]
        oldest_first = list(reversed(newest_first))
        seen = []
        for order in (newest_first, oldest_first):
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                write_trade_history(root, rows(order))
                summary = list_trade_records(root)["symbol_summaries"][0]
                seen.append(summary)

                self.assertFalse(summary["complete"])
                self.assertFalse(summary["exact"])
                self.assertEqual("", summary["incomplete_label"])
                self.assertEqual([], summary["incomplete_reasons"])
                self.assertEqual("ORDER_AMBIGUOUS", summary["order_status"])
                self.assertIn(
                    "同一天有成交没有记录成交时间，先后顺序无法确认",
                    summary["order_reasons"],
                )
                self.assertIsNone(summary["remaining_shares"])
                self.assertIsNone(summary["avg_cost"])
                self.assertIsNone(summary["last_operation_label"])
        # 行序不同也必须给出同一个答案。
        self.assertEqual(
            seen[0]["remaining_shares"], seen[1]["remaining_shares"]
        )
        self.assertEqual(seen[0]["complete"], seen[1]["complete"])

    def test_a_trade_left_blank_for_ambiguity_is_never_skipped_afterwards(self):
        # 一笔因顺序无从确认而留空的成交，不得在之后被当作不存在：
        # 否则它的股数会凭空消失，而后续行还会自称精确完整。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            append_trade_record(
                root,
                {
                    "request_id": "skip-first-0001",
                    "trade_date": "2026-08-20",
                    "symbol": "600519",
                    "side": "BUY",
                    "price": 10,
                    "shares": 1000,
                    "fee": 6,
                },
            )
            blanked = append_trade_record(
                root,
                {
                    "request_id": "skip-second-0002",
                    "trade_date": "2026-08-20",
                    "trade_time": "10:00",
                    "symbol": "600519",
                    "side": "BUY",
                    "price": 12,
                    "shares": 500,
                    "fee": 6,
                },
            )
            self.assertIsNone(blanked["record"]["remaining_shares"])

            later = append_trade_record(
                root,
                {
                    "request_id": "skip-third-0003",
                    "trade_date": "2026-08-21",
                    "trade_time": "10:00",
                    "symbol": "600519",
                    "side": "SELL",
                    "price": 15,
                    "shares": 200,
                    "fee": 6,
                },
            )
            record = later["record"]

            # 台账里有一笔的剩余股数是空的，链条就断了：之后不得再给出精确数字。
            self.assertIsNone(record["remaining_shares"])
            self.assertIsNone(record["avg_cost_after_trade"])
            self.assertIsNone(record["realized_pnl"])
            self.assertEqual("未归类", record["operation_label"])
            self.assertFalse(record["complete"])
            # 尤其不能算出 800（= 1000 − 200，把中间那 500 股整笔跳过）。
            stored = (root / "records" / "my_trades.csv").read_text(encoding="utf-8-sig")
            self.assertNotIn(",800,", stored)

    def test_a_sub_cent_fee_is_not_recorded_as_an_explicit_zero(self):
        # 四舍五入成 "0.00" 会把「有一点费用」说成「明确没有费用」。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            created = append_trade_record(
                root,
                {
                    "request_id": "fee-subcent-0001",
                    "trade_date": self.TODAY,
                    "trade_time": "09:31",
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 45,
                    "shares": 200,
                    "fee": 0.004,
                },
            )
            stored = next(
                iter(
                    csv.DictReader(
                        (root / "records" / "my_trades.csv")
                        .read_text(encoding="utf-8-sig")
                        .splitlines()
                    )
                )
            )

            self.assertEqual("0.004", stored["fee"])
            self.assertEqual(0.004, created["record"]["fee"])
            self.assertTrue(created["record"]["fee_reported"])

    def test_a_rejected_value_cannot_still_drive_the_classification(self):
        # 源文件给了负数剩余股数：读取侧已判它为缺字段，
        # 那它就绝不能再拿去推「减仓」——被拒绝的值不许在别处继续当事实用。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_trade_history(
                root,
                [
                    {
                        "trade_date": "2026-08-20",
                        "trade_time": "10:05:00",
                        "symbol": "600760.SH",
                        "side": "SELL",
                        "price": "50",
                        "shares": "200",
                        "remaining_shares": "-500",
                        "source": "panel_import",
                    }
                ],
            )
            listed = list_trade_records(root)
            record = listed["records"][0]
            summary = listed["symbol_summaries"][0]

            self.assertIsNone(record["remaining_shares"])
            self.assertIn("成交后剩余股数", record["missing_fields"])
            self.assertEqual("未归类", record["operation_label"])
            self.assertEqual("", record["operation_class"])
            self.assertEqual("未归类", summary["last_operation_label"])
            self.assertFalse(summary["complete"])

    def test_an_empty_ledger_reports_nothing_instead_of_zeroes(self):
        # 覆盖「空数据」的读取路径：没有文件和只有表头必须给出同一个干净的空结果，
        # 绝不出现 0 股 / 0 成本这种「看起来已经核对过」的数字。
        with tempfile.TemporaryDirectory() as temp_dir:
            no_file = list_trade_records(Path(temp_dir))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_trade_history(root, [])
            header_only = list_trade_records(root)

        for label, listed in (("无文件", no_file), ("只有表头", header_only)):
            self.assertEqual(0, listed["count"], label)
            self.assertEqual([], listed["records"], label)
            self.assertEqual([], listed["symbol_summaries"], label)
            self.assertEqual(0, listed["incomplete_record_count"], label)
            self.assertEqual(0, listed["fee_unknown_record_count"], label)
        self.assertEqual(no_file, header_only)

    def test_a_fully_recorded_symbol_summary_is_exact(self):
        # 覆盖「完整数据」：全部字段齐全且费用已知时，汇总必须敢说自己是精确完整的。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for rid, moment, side, price, shares, fee in [
                ("exact-0001", "09:30", "BUY", 45, 200, 5),
                ("exact-0002", "10:00", "BUY", 48, 100, 5),
                ("exact-0003", "14:00", "SELL", 50, 150, 6),
            ]:
                append_trade_record(
                    root,
                    {
                        "request_id": rid,
                        "trade_date": date.today().isoformat(),
                        "trade_time": moment,
                        "symbol": "600760",
                        "side": side,
                        "price": price,
                        "shares": shares,
                        "fee": fee,
                    },
                )
            listed = list_trade_records(root)
            summary = listed["symbol_summaries"][0]

            self.assertTrue(summary["complete"])
            self.assertTrue(summary["fee_complete"])
            self.assertTrue(summary["exact"])
            self.assertEqual("", summary["incomplete_label"])
            self.assertEqual("", summary["fee_label"])
            self.assertEqual([], summary["fee_reasons"])
            self.assertEqual([], summary["incomplete_reasons"])
            self.assertEqual("ORDER_KNOWN", summary["order_status"])
            self.assertEqual("exact-0003", summary["basis_record_ref"])
            self.assertEqual(0, listed["incomplete_record_count"])
            self.assertEqual(0, listed["fee_unknown_record_count"])
            self.assertTrue(all(record["exact"] for record in listed["records"]))

    def test_buying_again_after_a_close_is_a_fresh_first_buy(self):
        # 覆盖「四类操作」里最容易漏的一步：清仓之后重新买入要重新算作首次买入，
        # 且新的成本只由这一笔算出，不得掺入清仓前的旧成本。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = [
                ("reopen-0001", "2026-08-10", "BUY", 45, 200, "首次买入", "FIRST_BUY", 200),
                ("reopen-0002", "2026-08-11", "SELL", 50, 200, "清仓", "CLOSE", 0),
                ("reopen-0003", "2026-08-12", "BUY", 30, 100, "首次买入", "FIRST_BUY", 100),
                ("reopen-0004", "2026-08-13", "BUY", 32, 100, "加仓", "ADD", 200),
            ]
            records = {}
            for rid, day, side, price, shares, label, cls, remaining in plan:
                created = append_trade_record(
                    root,
                    {
                        "request_id": rid,
                        "trade_date": day,
                        "trade_time": "10:00",
                        "symbol": "600760",
                        "side": side,
                        "price": price,
                        "shares": shares,
                        "fee": 0,
                    },
                )
                records[rid] = created["record"]
                self.assertEqual(label, created["record"]["operation_label"], rid)
                self.assertEqual(cls, created["record"]["operation_class"], rid)
                self.assertEqual(remaining, created["record"]["remaining_shares"], rid)

            # 重开那一笔的成本只能是它自己的价格，绝不是清仓前的 45。
            self.assertEqual(30.0, records["reopen-0003"]["avg_cost_after_trade"])
            self.assertEqual(31.0, records["reopen-0004"]["avg_cost_after_trade"])

    def test_a_close_resets_the_unknown_fee_chain(self):
        # 清仓后成本基数归零，之后重新买入的成本与清仓前那些未知费用再无关系。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = append_trade_record(
                root,
                {
                    "request_id": "feereset-0001",
                    "trade_date": "2026-08-10",
                    "trade_time": "10:00",
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 45,
                    "shares": 200,
                },
            )
            closed = append_trade_record(
                root,
                {
                    "request_id": "feereset-0002",
                    "trade_date": "2026-08-11",
                    "trade_time": "10:00",
                    "symbol": "600760",
                    "side": "SELL",
                    "price": 50,
                    "shares": 200,
                    "fee": 6,
                },
            )
            reopened = append_trade_record(
                root,
                {
                    "request_id": "feereset-0003",
                    "trade_date": "2026-08-12",
                    "trade_time": "10:00",
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 30,
                    "shares": 100,
                    "fee": 3,
                },
            )

            self.assertFalse(first["record"]["fee_complete"])
            # 清仓这一笔自己的已实现盈亏仍受那笔未知费用影响，所以仍带标记。
            self.assertFalse(closed["record"]["fee_complete"])
            # 重开之后的基数是干净的。
            self.assertTrue(reopened["record"]["fee_complete"])
            self.assertEqual("", reopened["record"]["cost_fee_note"])
            self.assertTrue(reopened["record"]["exact"])
            summary = list_trade_records(root)["symbol_summaries"][0]
            self.assertTrue(summary["fee_complete"])
            self.assertEqual("", summary["fee_label"])

    def test_broker_supplied_numbers_stay_exact_even_with_no_fee_column(self):
        # 刻意保留的不对称：券商直接给出剩余股数与平均成本时，那是券商的口径，
        # 不是面板用费用算出来的，所以不加「不含未知费用」。
        # 但这个例外不得外溢到之后由面板计算的手工成交。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_trade_history(
                root,
                [
                    {
                        "trade_date": "2026-08-10",
                        "trade_time": "09:31:00",
                        "symbol": "600760.SH",
                        "side": "BUY",
                        "price": "45",
                        "shares": "200",
                        "remaining_shares": "200",
                        "avg_cost_after_trade": "45.0000",
                        "realized_pnl": "0.00",
                        "source": "panel_import",
                    }
                ],
            )
            imported = list_trade_records(root)["records"][0]
            self.assertTrue(imported["fee_complete"])
            self.assertTrue(imported["exact"])
            self.assertEqual("", imported["cost_fee_note"])
            # 导入行没有完整的费用明细，「本笔费用合计」必须报未知而不是 0。
            self.assertIsNone(imported["fee"])
            self.assertFalse(imported["fee_reported"])

            later = append_trade_record(
                root,
                {
                    "request_id": "spill-0001",
                    "trade_date": "2026-08-11",
                    "trade_time": "10:00",
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 50,
                    "shares": 100,
                },
            )
            self.assertFalse(later["record"]["fee_complete"])
            self.assertEqual("不含未知费用", later["record"]["cost_fee_note"])

    def test_a_zero_commission_with_unknown_taxes_is_not_a_complete_fee(self):
        # 导入行的 fee 列是券商的「佣金」。只有佣金=0、印花税/过户费缺列时，
        # 本笔费用合计其实未知，界面不得显示「¥0.00」。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_trade_history(
                root,
                [
                    {
                        "trade_date": "2026-08-10",
                        "trade_time": "09:31:00",
                        "symbol": "600760.SH",
                        "side": "SELL",
                        "price": "50",
                        "shares": "200",
                        "remaining_shares": "0",
                        "avg_cost_after_trade": "0.0000",
                        "realized_pnl": "1000.00",
                        "fee": "0",
                        "source": "panel_import",
                    },
                    {
                        "trade_date": "2026-08-11",
                        "trade_time": "09:31:00",
                        "symbol": "000001.SZ",
                        "side": "SELL",
                        "price": "50",
                        "shares": "200",
                        "remaining_shares": "0",
                        "avg_cost_after_trade": "0.0000",
                        "realized_pnl": "1000.00",
                        "fee": "5",
                        "stamp_tax": "10",
                        "transfer_fee": "1",
                        "other_fee": "0",
                        "source": "panel_import",
                    },
                ],
            )
            by_symbol = {
                record["symbol"]: record
                for record in list_trade_records(root)["records"]
            }
            partial = by_symbol["600760.SH"]
            full = by_symbol["000001.SZ"]

            self.assertIsNone(partial["fee"])
            self.assertFalse(partial["fee_reported"])
            # 四项明细齐全时合计已知：5 + 10 + 1 + 0 = 16
            self.assertEqual(16.0, full["fee"])
            self.assertTrue(full["fee_reported"])

    def test_a_recorded_four_class_label_cannot_pose_as_fact_without_the_numbers(self):
        # 四个中文标签本身就是面板算出来的。数字缺失时不得反解它们，
        # 否则「剩余 —」旁边会出现「清仓」这种自相矛盾的展示。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_trade_history(
                root,
                [
                    {
                        "trade_date": "2026-08-20",
                        "trade_time": "10:05:00",
                        "symbol": "600760.SH",
                        "side": "SELL",
                        "operation": "清仓",
                        "price": "50",
                        "shares": "200",
                        "source": "panel_import",
                    }
                ],
            )
            record = list_trade_records(root)["records"][0]

            self.assertEqual("清仓", record["operation"])
            self.assertEqual("未归类", record["operation_label"])
            self.assertEqual("", record["operation_class"])
            self.assertIsNone(record["remaining_shares"])


class TradeOrderAndReferenceTests(unittest.TestCase):
    """功能 1.2：业务顺序与稳定展示身份必须彼此独立。"""

    TODAY = date.today().isoformat()

    def _append(
        self,
        root: Path,
        request_id: str,
        *,
        trade_time: str = "",
        side: str = "BUY",
        price: float = 45,
        shares: int = 100,
    ) -> dict:
        return append_trade_record(
            root,
            {
                "request_id": request_id,
                "trade_date": self.TODAY,
                "trade_time": trade_time,
                "symbol": "600760",
                "side": side,
                "price": price,
                "shares": shares,
                "fee": 0,
            },
        )

    def test_same_day_all_unknown_manual_trades_are_order_ambiguous(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._append(root, "all-unknown-0001")
            second = self._append(root, "all-unknown-0002", price=46)
            listed = list_trade_records(root)
            summary = listed["symbol_summaries"][0]

            self.assertFalse(second["deduplicated"])
            self.assertEqual(2, listed["count"])
            self.assertEqual("ORDER_AMBIGUOUS", summary["order_status"])
            self.assertEqual("顺序待核对", summary["order_status_label"])
            self.assertIsNone(summary["basis_record_ref"])
            self.assertIsNone(summary["remaining_shares"])
            self.assertIsNone(summary["avg_cost"])
            self.assertIsNone(summary["last_trade_date"])
            self.assertIsNone(summary["last_trade_time"])
            self.assertIsNone(summary["last_operation_label"])
            self.assertIn(
                "同一天有成交没有记录成交时间，先后顺序无法确认",
                summary["order_reasons"],
            )
            self.assertTrue(all(r["trade_time_label"] == "未记录" for r in listed["records"]))

    def test_identical_trade_facts_have_distinct_refs_and_request_retry_is_still_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = self._append(root, "same-fact-0001", trade_time="09:30")
            second = self._append(root, "same-fact-0002", trade_time="09:30")
            retried = self._append(root, "same-fact-0002", trade_time="09:30")
            listed = list_trade_records(root)

            self.assertEqual("same-fact-0001", first["record"]["record_ref"])
            self.assertEqual("same-fact-0002", second["record"]["record_ref"])
            self.assertNotEqual(first["record"]["record_ref"], second["record"]["record_ref"])
            self.assertTrue(retried["deduplicated"])
            self.assertEqual(second["record"]["record_ref"], retried["record"]["record_ref"])
            self.assertEqual(2, listed["count"])
            self.assertEqual(
                "ORDER_AMBIGUOUS",
                listed["symbol_summaries"][0]["order_status"],
            )

    def test_existing_refs_survive_a_later_append(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._append(root, "stable-ref-0001", trade_time="09:30")
            self._append(root, "stable-ref-0002", trade_time="10:00")
            before = {
                record["request_id"]: record["record_ref"]
                for record in list_trade_records(root)["records"]
            }

            append_trade_record(
                root,
                {
                    "request_id": "stable-ref-0003",
                    "trade_date": "2026-01-01",
                    "trade_time": "14:00",
                    "symbol": "000001.SZ",
                    "side": "BUY",
                    "price": 10,
                    "shares": 100,
                    "fee": 0,
                },
            )
            after = {
                record["request_id"]: record["record_ref"]
                for record in list_trade_records(root)["records"]
                if record["request_id"] in before
            }
            self.assertEqual(before, after)

    def test_a_source_ref_collision_keeps_manual_request_idempotency_and_unique_refs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_trade_history(
                root,
                [
                    {
                        "trade_date": "2026-01-01",
                        "trade_time": "09:30:00",
                        "symbol": "600760.SH",
                        "side": "BUY",
                        "price": "45",
                        "shares": "100",
                        "remaining_shares": "100",
                        "avg_cost_after_trade": "45",
                        "realized_pnl": "0",
                        "fee": "0",
                        "stamp_tax": "0",
                        "transfer_fee": "0",
                        "other_fee": "0",
                        "record_ref": "ref-collision-0001",
                        "source": "panel_import",
                    }
                ],
            )
            created = self._append(
                root,
                "ref-collision-0001",
                trade_time="10:00",
            )
            retried = self._append(
                root,
                "ref-collision-0001",
                trade_time="10:00",
            )
            listed = list_trade_records(root)

            self.assertEqual("ref-collision-0001", created["record"]["request_id"])
            self.assertEqual("manual-ref-collision-0001", created["record"]["record_ref"])
            self.assertTrue(retried["deduplicated"])
            self.assertEqual(created["record"]["record_ref"], retried["record"]["record_ref"])
            self.assertEqual(2, len({record["record_ref"] for record in listed["records"]}))
            self.assertEqual(2, listed["count"])

    def test_legacy_duplicate_rows_get_stable_distinct_refs_and_persist_on_append(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "records" / "my_trades.csv"
            target.parent.mkdir(parents=True)
            legacy_columns = [column for column in TRADE_COLUMNS if column != "record_ref"]
            identical = {
                "trade_date": "2026-08-01",
                "trade_time": "09:30:00",
                "symbol": "600760.SH",
                "stock_name": "测试股票",
                "side": "BUY",
                "price": "45",
                "shares": "100",
                "remaining_shares": "100",
                "avg_cost_after_trade": "45",
                "realized_pnl": "0",
                "source": "panel_import",
            }
            with target.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=legacy_columns)
                writer.writeheader()
                writer.writerow({column: identical.get(column, "") for column in legacy_columns})
                writer.writerow({column: identical.get(column, "") for column in legacy_columns})
            original_bytes = target.read_bytes()

            first_read = [record["record_ref"] for record in list_trade_records(root)["records"]]
            second_read = [record["record_ref"] for record in list_trade_records(root)["records"]]
            self.assertEqual(first_read, second_read)
            self.assertEqual(2, len(set(first_read)))
            self.assertNotIn("record_ref", next(csv.DictReader(target.read_text(encoding="utf-8-sig").splitlines())))

            append_trade_record(
                root,
                {
                    "request_id": "legacy-save-0001",
                    "trade_date": "2026-08-02",
                    "trade_time": "10:00",
                    "symbol": "600760",
                    "side": "BUY",
                    "price": 46,
                    "shares": 100,
                    "fee": 0,
                },
            )
            persisted = list(
                csv.DictReader(target.read_text(encoding="utf-8-sig").splitlines())
            )
            legacy_rows = [row for row in persisted if row["source"] == "panel_import"]
            self.assertEqual(set(first_read), {row["record_ref"] for row in legacy_rows})
            expected_facts = {
                column: identical.get(column, "") for column in legacy_columns
            }
            for row in legacy_rows:
                self.assertEqual(
                    expected_facts,
                    {column: row[column] for column in legacy_columns},
                )
            backups = list((root / "history" / "trade_records").glob("trades_*.csv"))
            self.assertEqual(1, len(backups))
            self.assertEqual(original_bytes, backups[0].read_bytes())


if __name__ == "__main__":
    unittest.main()
