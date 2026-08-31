from __future__ import annotations

import base64
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.personal_data import (
    PersonalDataError,
    TRADE_COLUMNS,
    import_personal_data,
    list_trade_records,
    personal_data_status,
)


class PersonalDataTests(unittest.TestCase):
    def test_chinese_position_preview_normalizes_without_writing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = {
                "kind": "positions",
                "mode": "preview",
                "filename": "持仓.csv",
                "csv_text": (
                    "日期,股票代码,股票名称,持仓股数,可卖数量,平均成本,最新市价,资产总值\n"
                    "2026-07-14,600760,中航沈飞,800,600,44.56,45.10,140000\n"
                ),
            }
            result = import_personal_data(root, payload)
            self.assertFalse(result["written"])
            self.assertEqual("600760.SH", result["preview"][0]["symbol"])
            self.assertEqual(800, result["preview"][0]["shares_total"])
            self.assertEqual(600, result["preview"][0]["shares_available"])
            self.assertEqual(44.56, result["preview"][0]["avg_cost"])
            self.assertEqual(45.10, result["preview"][0]["current_price"])
            self.assertEqual(36_080, result["preview"][0]["market_value"])
            self.assertFalse((root / "records" / "positions_snapshot.csv").exists())

    def test_gb18030_trade_import_keeps_unrecorded_ledger_fields_empty(self):
        # 原先这里断言导入会按零起点推算 remaining_shares / realized_pnl。
        # 那些数字的正确性依赖「导入区间覆盖了该股票从第一笔买入开始的完整历史」，
        # 导入方无从确认，因此改为断言：源文件没给就留空，绝不反写系统测算值。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            records = root / "records"
            records.mkdir()
            target = records / "my_trades.csv"
            target.write_text("trade_date,symbol\n2026-01-01,000001.SZ\n", encoding="utf-8")
            source = (
                "成交日期,成交时间,证券代码,证券名称,买卖方向,成交价格,成交数量,佣金\n"
                "2026-07-10,14:30:00,600760,中航沈飞,买入,44.5,200,5\n"
                "2026-07-11,14:40:00,600760,中航沈飞,卖出,45.5,100,5\n"
            ).encode("gb18030")
            payload = {
                "kind": "trades",
                "mode": "commit",
                "filename": "成交导出.csv",
                "content_base64": base64.b64encode(source).decode("ascii"),
            }
            result = import_personal_data(root, payload)
            imported = pd.read_csv(target, dtype=str, keep_default_na=False)

            self.assertTrue(result["written"])
            self.assertEqual("gb18030", result["encoding"])
            self.assertIsNotNone(result["backup_path"])
            self.assertTrue((root / result["backup_path"]).exists())
            # 源文件确实给的字段照实保留。
            self.assertEqual(["BUY", "SELL"], imported["side"].tolist())
            self.assertEqual(["200", "100"], imported["shares"].tolist())
            self.assertEqual([5.0, 5.0], [float(value) for value in imported["fee"]])
            # 源文件没给的三个台账字段一律留空，一个数字都不许生出来。
            for column in ("remaining_shares", "avg_cost_after_trade", "realized_pnl"):
                self.assertEqual(["", ""], imported[column].tolist(), column)
            # 依赖它们的市值与仓位比例同样留空，不得二次推算。
            for column in ("position_market_value", "position_pct"):
                self.assertEqual(["", ""], imported[column].tolist(), column)
            # 印花税/过户费缺列即未知，因此净额也无从精确，不按零费用落盘。
            self.assertEqual(["", ""], imported["net_amount"].tolist())
            self.assertIn(
                "2 笔成交没有成交后剩余股数，已保持为空并标记「历史数据不完整」，不做推算。",
                result["warnings"],
            )
            self.assertIn(
                "2 笔成交的费用不完整，已保持为空（费用未知），不按 0 处理。",
                result["warnings"],
            )

    def test_trade_import_keeps_ledger_fields_the_source_actually_provides(self):
        # 券商确实给了成交后余额/成本/盈亏时原样保留；只有缺的那些行留空。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = {
                "kind": "trades",
                "mode": "commit",
                "filename": "含余额.csv",
                "csv_text": (
                    "成交日期,证券代码,买卖方向,成交价格,成交数量,成交后余额,剩余成本价,实现盈亏\n"
                    "2026-07-10,600760,买入,44.5,200,200,44.5000,0\n"
                    "2026-07-11,600760,卖出,45.5,100,,,\n"
                ),
            }
            import_personal_data(root, payload)
            imported = pd.read_csv(
                root / "records" / "my_trades.csv", dtype=str, keep_default_na=False
            )

            self.assertEqual(["200", ""], imported["remaining_shares"].tolist())
            self.assertEqual(44.5, float(imported["avg_cost_after_trade"][0]))
            self.assertEqual("", imported["avg_cost_after_trade"][1])
            self.assertEqual(0.0, float(imported["realized_pnl"][0]))
            self.assertEqual("", imported["realized_pnl"][1])

    def test_trade_import_flags_an_incomplete_export_window_without_inventing_values(self):
        # 区间里卖出多于买入 → 起点晚于第一笔买入。只提示，不补任何数值。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = {
                "kind": "trades",
                "mode": "commit",
                "filename": "区间不全.csv",
                "csv_text": (
                    "成交日期,证券代码,买卖方向,成交价格,成交数量\n"
                    "2026-07-11,600760,卖出,45.5,300\n"
                ),
            }
            result = import_personal_data(root, payload)
            imported = pd.read_csv(
                root / "records" / "my_trades.csv", dtype=str, keep_default_na=False
            )

            self.assertIn(
                "600760.SH 在本次导入区间里卖出数量高于买入数量，导出区间可能不完整，请核对。",
                result["warnings"],
            )
            self.assertEqual([""], imported["remaining_shares"].tolist())
            self.assertEqual([""], imported["avg_cost_after_trade"].tolist())
            self.assertEqual([""], imported["realized_pnl"].tolist())

    def test_trade_import_without_a_time_column_keeps_the_time_unrecorded(self):
        # 券商导出没有成交时间时留空，绝不用收盘时间或导入时刻冒充。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = {
                "kind": "trades",
                "mode": "commit",
                "filename": "无时间.csv",
                "csv_text": (
                    "成交日期,证券代码,买卖方向,成交价格,成交数量\n"
                    "2026-07-10,600760,买入,44.5,200\n"
                    "2026-07-11,600760,卖出,45.5,100\n"
                ),
            }
            result = import_personal_data(root, payload)
            imported = pd.read_csv(
                root / "records" / "my_trades.csv", dtype=str, keep_default_na=False
            )

            self.assertEqual(["", ""], imported["trade_time"].tolist())
            self.assertIn(
                "2 笔成交没有可识别的成交时间，已按「未记录」保存。",
                result["warnings"],
            )

    def test_import_tells_an_explicit_zero_fee_apart_from_a_missing_fee_column(self):
        # 覆盖「费用为 0」与「费用未记录」在**导入路径**的区分：
        # 源文件明确写 0 → 落盘 0 且净额可算；完全没有费用列 → 留空并告警，不按 0 处理。
        warning = "1 笔成交的费用不完整，已保持为空（费用未知），不按 0 处理。"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            explicit = import_personal_data(
                root,
                {
                    "kind": "trades",
                    "mode": "commit",
                    "filename": "费用为零.csv",
                    "csv_text": (
                        "成交日期,证券代码,买卖方向,成交价格,成交数量,佣金,印花税,过户费,其他费用\n"
                        "2026-07-10,600760,买入,44.5,200,0,0,0,0\n"
                    ),
                },
            )
            zero_row = pd.read_csv(
                root / "records" / "my_trades.csv", dtype=str, keep_default_na=False
            ).iloc[0]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            missing = import_personal_data(
                root,
                {
                    "kind": "trades",
                    "mode": "commit",
                    "filename": "无费用列.csv",
                    "csv_text": (
                        "成交日期,证券代码,买卖方向,成交价格,成交数量\n"
                        "2026-07-10,600760,买入,44.5,200\n"
                    ),
                },
            )
            blank_row = pd.read_csv(
                root / "records" / "my_trades.csv", dtype=str, keep_default_na=False
            ).iloc[0]

        # 明确为 0：四项都落 0，净额算得出来，不报「费用不完整」。
        self.assertEqual(0.0, float(zero_row["fee"]))
        for column in ("stamp_tax", "transfer_fee", "other_fee"):
            self.assertEqual(0.0, float(zero_row[column]), column)
        self.assertEqual(-8900.0, float(zero_row["net_amount"]))
        self.assertNotIn(warning, explicit["warnings"])

        # 未记录：四项留空，净额也无从精确，必须告警。
        for column in ("fee", "stamp_tax", "transfer_fee", "other_fee", "net_amount"):
            self.assertEqual("", blank_row[column], column)
        self.assertIn(warning, missing["warnings"])

    def test_import_treats_an_unparsable_time_cell_as_unrecorded(self):
        # 有成交时间列但值是脏数据时，按「未记录」保存，绝不用导入时刻或收盘时间冒充。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = import_personal_data(
                root,
                {
                    "kind": "trades",
                    "mode": "commit",
                    "filename": "脏时间.csv",
                    "csv_text": (
                        "成交日期,成交时间,证券代码,买卖方向,成交价格,成交数量\n"
                        "2026-07-10,09:31:00,600760,买入,44.5,200\n"
                        "2026-07-11,--,600760,买入,45.0,100\n"
                        "2026-07-12,093500,600760,买入,46.0,100\n"
                    ),
                },
            )
            raw = (root / "records" / "my_trades.csv").read_text(encoding="utf-8-sig")
            times = pd.read_csv(
                root / "records" / "my_trades.csv", dtype=str, keep_default_na=False
            )["trade_time"].tolist()

            # 能识别的原样保留（含 HHMMSS 变体），识别不了的留空。
            self.assertEqual(["09:31:00", "", "09:35:00"], times)
            self.assertIn(
                "1 笔成交没有可识别的成交时间，已按「未记录」保存。", result["warnings"]
            )
            self.assertNotIn(datetime.now().strftime("%H:%M"), raw)
            self.assertNotIn("15:00:00", raw)

    def test_imported_history_reads_back_with_the_same_incomplete_flags(self):
        # 端到端：导入写出的形状必须能被读取侧正确判读，避免测试夹具与真实输出漂移。
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            import_personal_data(
                root,
                {
                    "kind": "trades",
                    "mode": "commit",
                    "filename": "同日无时间.csv",
                    "csv_text": (
                        "成交日期,证券代码,买卖方向,成交价格,成交数量\n"
                        "2026-04-01,600760,卖出,11.0,1000\n"
                        "2026-04-01,600760,买入,10.0,1000\n"
                    ),
                },
            )
            listed = list_trade_records(root)
            summary = listed["symbol_summaries"][0]

            self.assertEqual(2, listed["count"])
            self.assertEqual(2, listed["incomplete_record_count"])
            self.assertIsNone(summary["remaining_shares"])
            self.assertIsNone(summary["avg_cost"])
            self.assertFalse(summary["complete"])
            self.assertEqual("历史数据不完整", summary["incomplete_label"])
            self.assertIn(
                "同一天有成交没有记录成交时间，先后顺序无法确认",
                summary["order_reasons"],
            )
            self.assertEqual("ORDER_AMBIGUOUS", summary["order_status"])
            for record in listed["records"]:
                self.assertIsNone(record["remaining_shares"])
                self.assertIsNone(record["avg_cost_after_trade"])
                self.assertEqual("未归类", record["operation_label"])

    def test_trade_import_reuses_a_source_trade_number_as_record_ref(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            import_personal_data(
                root,
                {
                    "kind": "trades",
                    "mode": "commit",
                    "filename": "带成交编号.csv",
                    "csv_text": (
                        "成交编号,成交日期,成交时间,证券代码,买卖方向,成交价格,成交数量\n"
                        "broker-trade-001,2026-07-10,09:31,600760,买入,44.5,200\n"
                        "broker-trade-002,2026-07-11,14:00,600760,卖出,45.5,100\n"
                    ),
                },
            )
            stored = pd.read_csv(
                root / "records" / "my_trades.csv", dtype=str, keep_default_na=False
            )
            listed = list_trade_records(root)

            self.assertEqual(
                ["broker-trade-001", "broker-trade-002"],
                stored["record_ref"].tolist(),
            )
            self.assertEqual(
                {"broker-trade-001", "broker-trade-002"},
                {record["record_ref"] for record in listed["records"]},
            )
            self.assertTrue(all(record["request_id"] == "" for record in listed["records"]))

    def test_trade_import_without_source_numbers_persists_distinct_local_refs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = {
                "kind": "trades",
                "filename": "无成交编号.csv",
                "csv_text": (
                    "成交日期,成交时间,证券代码,买卖方向,成交价格,成交数量\n"
                    "2026-07-10,09:31,600760,买入,44.5,200\n"
                    "2026-07-10,09:31,600760,买入,44.5,200\n"
                ),
            }
            preview = import_personal_data(root, {**payload, "mode": "preview"})
            self.assertEqual([None, None], [row["record_ref"] for row in preview["preview"]])
            self.assertFalse((root / "records" / "my_trades.csv").exists())

            import_personal_data(root, {**payload, "mode": "commit"})
            stored = pd.read_csv(
                root / "records" / "my_trades.csv", dtype=str, keep_default_na=False
            )
            first_refs = stored["record_ref"].tolist()
            second_refs = [record["record_ref"] for record in list_trade_records(root)["records"]]

            self.assertEqual(2, len(set(first_refs)))
            self.assertTrue(all(ref.startswith("import-") for ref in first_refs))
            self.assertEqual(set(first_refs), set(second_refs))

    def test_trade_import_rejects_duplicate_source_trade_numbers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(PersonalDataError, "成交编号不能重复"):
                import_personal_data(
                    root,
                    {
                        "kind": "trades",
                        "mode": "commit",
                        "filename": "重复成交编号.csv",
                        "csv_text": (
                            "成交编号,成交日期,证券代码,买卖方向,成交价格,成交数量\n"
                            "duplicate-001,2026-07-10,600760,买入,44.5,200\n"
                            "duplicate-001,2026-07-11,600760,卖出,45.5,100\n"
                        ),
                    },
                )
            self.assertFalse((root / "records" / "my_trades.csv").exists())

    def test_status_reports_missing_and_empty_trade_ledger_separately(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            missing = personal_data_status(root)
            self.assertEqual("MISSING", missing["trades"]["state"])
            self.assertEqual(0, missing["trades"]["rows"])
            self.assertFalse(missing["ready"])
            self.assertFalse(missing["has_demo_data"])

            records = root / "records"
            records.mkdir()
            (records / "my_trades.csv").write_text(
                ",".join(TRADE_COLUMNS) + "\n", encoding="utf-8-sig"
            )
            empty = personal_data_status(root)
            self.assertEqual("EMPTY", empty["trades"]["state"])
            self.assertEqual(0, empty["trades"]["rows"])
            self.assertFalse(empty["ready"])

    def test_an_empty_import_is_rejected_without_touching_the_existing_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "records" / "my_trades.csv"
            target.parent.mkdir()
            original = "trade_date,symbol\n2026-01-01,000001.SZ\n"
            target.write_text(original, encoding="utf-8")
            for payload in (
                {"kind": "trades", "mode": "commit", "filename": "空.csv", "csv_text": ""},
                {
                    "kind": "trades",
                    "mode": "commit",
                    "filename": "只有表头.csv",
                    "csv_text": "成交日期,证券代码,买卖方向,成交价格,成交数量\n",
                },
            ):
                with self.assertRaises(PersonalDataError):
                    import_personal_data(root, payload)
            self.assertEqual(original, target.read_text(encoding="utf-8"))

    def test_invalid_trade_direction_does_not_replace_existing_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "records" / "my_trades.csv"
            target.parent.mkdir()
            original = "trade_date,symbol\n2026-01-01,000001.SZ\n"
            target.write_text(original, encoding="utf-8")
            payload = {
                "kind": "trades",
                "mode": "commit",
                "filename": "bad.csv",
                "csv_text": (
                    "成交日期,证券代码,买卖方向,成交价格,成交数量\n"
                    "2026-07-10,600760,换股,44.5,200\n"
                ),
            }
            with self.assertRaises(PersonalDataError):
                import_personal_data(root, payload)
            self.assertEqual(original, target.read_text(encoding="utf-8"))

    def test_status_distinguishes_demo_and_real_personal_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            records = root / "records"
            records.mkdir()
            (records / "positions_snapshot.csv").write_text(
                "snapshot_date,symbol,notes\n2026-07-14,600760.SH,示例持仓\n",
                encoding="utf-8",
            )
            (records / "my_trades.csv").write_text(
                "trade_date,symbol,source,notes\n2026-07-14,600760.SH,panel_import,真实导入\n",
                encoding="utf-8",
            )
            status = personal_data_status(root)
            self.assertTrue(status["has_demo_data"])
            self.assertEqual("DEMO", status["positions"]["state"])
            self.assertEqual("READY", status["trades"]["state"])
            self.assertFalse(status["ready"])


if __name__ == "__main__":
    unittest.main()
