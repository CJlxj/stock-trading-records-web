from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.io_loader import find_latest_csv, validate_symbol, validate_timeframe


def market_frame(end_date: str) -> pd.DataFrame:
    dates = pd.bdate_range(end=pd.Timestamp(end_date), periods=60)
    return pd.DataFrame(
        {
            "date": dates,
            "open": range(60),
            "high": [value + 2 for value in range(60)],
            "low": [value - 1 for value in range(60)],
            "close": [value + 1 for value in range(60)],
            "volume": [10000] * 60,
        }
    )


class IoLoaderTests(unittest.TestCase):
    def test_latest_csv_uses_market_date_not_file_mtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir) / "600000.SH" / "raw" / "day"
            folder.mkdir(parents=True)
            newer_market = folder / "older_file.csv"
            older_market = folder / "newer_file.csv"
            market_frame("2026-06-30").to_csv(newer_market, index=False)
            market_frame("2026-05-30").to_csv(older_market, index=False)
            older_market.touch()
            self.assertEqual(newer_market, find_latest_csv("600000.SH", "day", temp_dir))

    def test_path_inputs_are_validated(self):
        with self.assertRaises(ValueError):
            validate_symbol("../records")
        with self.assertRaises(ValueError):
            validate_timeframe("../../day")
        self.assertEqual("minute/5m", validate_timeframe("minute/5m"))


if __name__ == "__main__":
    unittest.main()
