from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.personal_data import (
    PersonalDataError,
    import_personal_data,
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

    def test_gb18030_trade_import_derives_values_and_backs_up_existing_file(self):
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
            imported = pd.read_csv(target)
            self.assertTrue(result["written"])
            self.assertEqual("gb18030", result["encoding"])
            self.assertIsNotNone(result["backup_path"])
            self.assertTrue((root / result["backup_path"]).exists())
            self.assertEqual(["BUY", "SELL"], imported["side"].tolist())
            self.assertEqual([200, 100], imported["remaining_shares"].tolist())
            self.assertEqual(92.5, imported.iloc[-1]["realized_pnl"])

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
