from __future__ import annotations

from datetime import date
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from src.market_data import (
    EASTMONEY_COLUMNS,
    MarketDataError,
    fetch_eastmoney_history,
    fetch_tencent_history,
    update_market_data,
)


def fake_akshare_fetcher(**kwargs) -> pd.DataFrame:
    dates = pd.bdate_range(end=pd.Timestamp("2026-07-10"), periods=120)
    values = pd.Series(range(120), dtype="float64")
    return pd.DataFrame(
        {
            "日期": dates,
            "股票代码": [kwargs["symbol"]] * 120,
            "开盘": 40 + values * 0.02,
            "收盘": 40.1 + values * 0.02,
            "最高": 40.4 + values * 0.02,
            "最低": 39.8 + values * 0.02,
            "成交量": [1000] * 120,
            "成交额": [4_000_000] * 120,
        }
    )


def fake_new_listing_fetcher(**kwargs) -> pd.DataFrame:
    dates = pd.bdate_range(end=pd.Timestamp("2026-07-10"), periods=8)
    values = pd.Series(range(8), dtype="float64")
    return pd.DataFrame(
        {
            "日期": dates,
            "股票代码": [kwargs["symbol"]] * 8,
            "开盘": 10 + values * 0.02,
            "收盘": 10.1 + values * 0.02,
            "最高": 10.4 + values * 0.02,
            "最低": 9.8 + values * 0.02,
            "成交量": [1000] * 8,
            "成交额": [1_000_000] * 8,
        }
    )


class MarketDataTests(unittest.TestCase):
    def test_eastmoney_fetcher_returns_akshare_compatible_columns(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self):
                return json.dumps(
                    {
                        "rc": 0,
                        "data": {
                            "klines": [
                                "2026-07-09,14.00,14.20,14.30,13.90,12345,17500000.00,2.86,1.43,0.20,0.42"
                            ]
                        },
                    }
                ).encode("utf-8")

        with patch("src.market_data.urlopen", return_value=FakeResponse()) as mocked_open:
            frame = fetch_eastmoney_history(
                symbol="605507",
                exchange="SH",
                period="daily",
                start_date="20250701",
                end_date="20260710",
                adjust="qfq",
            )

        self.assertEqual(EASTMONEY_COLUMNS, list(frame.columns))
        self.assertEqual("2026-07-09", frame.iloc[0]["日期"])
        self.assertEqual("12345", frame.iloc[0]["成交量"])
        request = mocked_open.call_args.args[0]
        self.assertIn("quote.eastmoney.com/sh605507.html", request.headers["Referer"])

    def test_tencent_fetcher_appends_latest_closed_quote(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self):
                quote = [""] * 37
                quote[3] = "15.69"
                quote[5] = "15.40"
                quote[30] = "20260715161457"
                quote[33] = "15.92"
                quote[34] = "15.33"
                quote[36] = "69651"
                return json.dumps(
                    {
                        "data": {
                            "sh605507": {
                                "qfqday": [["2026-07-14", "15.00", "15.40", "15.55", "14.88", "56972"]],
                                "qt": {"sh605507": quote},
                            }
                        }
                    }
                ).encode("utf-8")

        with patch("src.market_data.urlopen", return_value=FakeResponse()):
            frame = fetch_tencent_history(
                symbol="605507",
                exchange="SH",
                period="daily",
                start_date="20260113",
                end_date="20260715",
                adjust="qfq",
            )

        self.assertEqual(2, len(frame))
        self.assertEqual("2026-07-15", frame.iloc[-1]["日期"])
        self.assertEqual("15.69", frame.iloc[-1]["收盘"])

    def test_tencent_new_listing_uses_raw_day_when_qfq_is_not_created(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self):
                return json.dumps(
                    {
                        "data": {
                            "sh688825": {
                                "day": [
                                    ["2026-07-27", "40.00", "41.00", "42.00", "39.00", "1000"],
                                    ["2026-07-28", "41.00", "42.00", "43.00", "40.00", "1200"],
                                ],
                                "qt": {},
                            }
                        }
                    }
                ).encode("utf-8")

        with patch("src.market_data.urlopen", return_value=FakeResponse()):
            frame = fetch_tencent_history(
                symbol="688825",
                exchange="SH",
                period="daily",
                start_date="20260701",
                end_date="20260730",
                adjust="qfq",
            )

        self.assertEqual(2, len(frame))
        self.assertEqual("2026-07-28", frame.iloc[-1]["日期"])
        self.assertEqual("42.00", frame.iloc[-1]["收盘"])

    def test_update_normalizes_and_writes_project_csv(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = update_market_data(
                temp_dir,
                symbol="600760.SH",
                timeframe="day",
                adjust="qfq",
                history_years=3,
                fetcher=fake_akshare_fetcher,
                today=date(2026, 7, 11),
            )
            csv_path = Path(temp_dir) / result["path"]
            metadata_path = Path(temp_dir) / result["metadata_path"]
            frame = pd.read_csv(csv_path)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(120, len(frame))
            self.assertEqual(100000, frame.iloc[-1]["volume"])
            self.assertEqual("2026-07-10", result["latest_date"])
            self.assertEqual("shares", metadata["volume_unit"])

    def test_daily_update_accepts_half_year_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = update_market_data(
                temp_dir,
                symbol="600172.SH",
                timeframe="day",
                adjust="qfq",
                history_years=0.5,
                fetcher=fake_akshare_fetcher,
                today=date(2026, 7, 11),
            )
            self.assertEqual(0.5, result["history_years"])
            self.assertEqual("2026-07-10", result["latest_date"])

    def test_new_listing_with_fewer_than_60_rows_is_saved_normally(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = update_market_data(
                temp_dir,
                symbol="920000.BJ",
                timeframe="day",
                adjust="qfq",
                history_years=3,
                fetcher=fake_new_listing_fetcher,
                today=date(2026, 7, 11),
            )
            csv_path = Path(temp_dir) / result["path"]
            metadata_path = Path(temp_dir) / result["metadata_path"]
            frame = pd.read_csv(csv_path)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

            self.assertEqual(8, len(frame))
            self.assertEqual(8, result["rows"])
            self.assertEqual(8, metadata["rows"])
            self.assertEqual("2026-07-10", result["latest_date"])

    def test_monthly_update_requires_longer_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(MarketDataError):
                update_market_data(
                    temp_dir,
                    symbol="600760.SH",
                    timeframe="month",
                    history_years=3,
                    fetcher=fake_akshare_fetcher,
                )


if __name__ == "__main__":
    unittest.main()
