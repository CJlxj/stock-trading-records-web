from __future__ import annotations

import base64
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from src.market_data import MarketDataError, import_market_data
from src.stock_library import read_local_stock_library


def payload_for(
    text: str,
    *,
    symbol: str = "600760.SH",
    encoding: str = "utf-8",
    volume_unit: str = "shares",
) -> dict[str, str]:
    return {
        "request_id": "manual-test",
        "symbol": symbol,
        "filename": "../../manual.csv",
        "content_base64": base64.b64encode(text.encode(encoding)).decode("ascii"),
        "volume_unit": volume_unit,
        "adjust": "qfq",
    }


class ManualMarketDataImportTests(unittest.TestCase):
    def test_chinese_columns_are_standardized_and_short_history_is_saved(self):
        csv_text = "\n".join(
            [
                "交易日期,开盘价,最高价,最低价,收盘价,成交量(手),成交额,备注",
                "2026/07/30,10.00,10.80,9.90,10.50,123,129150,保留事实",
                "20260729,9.80,10.20,9.70,10.00,100,100000,忽略",
                "2026/07/30,10.00,10.80,9.90,10.50,123,129150,重复",
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            result = import_market_data(
                temp_dir,
                payload_for(csv_text, volume_unit="shares"),
                today=date(2026, 7, 31),
            )

            frame = pd.read_csv(Path(temp_dir) / result["path"])
            metadata = json.loads(
                (Path(temp_dir) / result["metadata_path"]).read_text(
                    encoding="utf-8"
                )
            )
            library = read_local_stock_library(temp_dir)

        self.assertEqual(
            ["date", "open", "high", "low", "close", "volume", "amount"],
            frame.columns.tolist(),
        )
        self.assertEqual(["2026-07-29", "2026-07-30"], frame["date"].tolist())
        self.assertEqual(12_300, int(frame.iloc[-1]["volume"]))
        self.assertEqual(2, result["rows"])
        self.assertEqual(1, result["exact_duplicates_removed"])
        self.assertEqual("lots", metadata["input_volume_unit"])
        self.assertEqual("manual.csv", metadata["source_filename"])
        self.assertEqual(["600760.SH"], library["symbol"].tolist())
        self.assertEqual("manual_import", library.iloc[0]["source"])

    def test_gb18030_and_six_digit_symbol_are_supported(self):
        csv_text = "\n".join(
            [
                "日期,开盘,最高,最低,收盘,成交量",
                "2026-07-30,8.10,8.30,8.00,8.20,5000",
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            result = import_market_data(
                temp_dir,
                payload_for(csv_text, symbol="000001", encoding="gb18030"),
                today=date(2026, 7, 31),
            )
            frame = pd.read_csv(Path(temp_dir) / result["path"])

        self.assertEqual("000001.SZ", result["symbol"])
        self.assertEqual(1, result["rows"])
        self.assertEqual(5000, int(frame.iloc[0]["volume"]))

    def test_invalid_ohlc_is_rejected_without_writing_data(self):
        csv_text = "\n".join(
            [
                "date,open,high,low,close,volume",
                "2026-07-30,10,9,8,9.5,1000",
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(MarketDataError, "最高价或最低价"):
                import_market_data(
                    temp_dir,
                    payload_for(csv_text),
                    today=date(2026, 7, 31),
                )
            self.assertFalse((Path(temp_dir) / "data").exists())

    def test_conflicting_duplicate_date_is_rejected(self):
        csv_text = "\n".join(
            [
                "date,open,high,low,close,volume",
                "2026-07-30,10,11,9,10.5,1000",
                "2026-07-30,10,11,9,10.6,1000",
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(MarketDataError, "内容不一致的重复记录"):
                import_market_data(
                    temp_dir,
                    payload_for(csv_text),
                    today=date(2026, 7, 31),
                )

    def test_missing_required_columns_and_future_dates_are_rejected(self):
        missing = "date,open,high,low,close\n2026-07-30,10,11,9,10.5\n"
        future = (
            "date,open,high,low,close,volume\n"
            "2026-08-01,10,11,9,10.5,1000\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(MarketDataError, "volume"):
                import_market_data(
                    temp_dir,
                    payload_for(missing),
                    today=date(2026, 7, 31),
                )
            with self.assertRaisesRegex(MarketDataError, "未来日期"):
                import_market_data(
                    temp_dir,
                    payload_for(future),
                    today=date(2026, 7, 31),
                )

    def test_identical_content_is_deduplicated_and_changed_content_keeps_both_files(self):
        original = (
            "date,open,high,low,close,volume\n"
            "2026-07-30,10,11,9,10.5,1000\n"
        )
        changed = (
            "date,open,high,low,close,volume\n"
            "2026-07-30,10,11,9,10.6,1000\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            first = import_market_data(
                temp_dir,
                payload_for(original),
                today=date(2026, 7, 31),
            )
            repeated = import_market_data(
                temp_dir,
                payload_for(original),
                today=date(2026, 7, 31),
            )
            second = import_market_data(
                temp_dir,
                payload_for(changed),
                today=date(2026, 7, 31),
            )
            files = list(
                (
                    Path(temp_dir)
                    / "data"
                    / "600760.SH"
                    / "raw"
                    / "day"
                ).glob("*.csv")
            )

        self.assertFalse(first["deduplicated"])
        self.assertTrue(repeated["deduplicated"])
        self.assertFalse(second["deduplicated"])
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(2, len(files))


if __name__ == "__main__":
    unittest.main()
