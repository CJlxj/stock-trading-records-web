from __future__ import annotations

from datetime import date
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.market_data import MarketDataError, update_market_data
from src.market_screening import MIN_FULL_MARKET_ROWS
from src.stock_catalog import (
    StockCatalogError,
    StockCatalogNotReadyError,
    add_stock_from_catalog,
    load_full_stock_catalog,
    search_stock_catalog,
    stock_catalog_paths,
    stock_catalog_status,
    sync_stock_catalog,
)
from src.stock_library import (
    add_local_stock_member,
    read_local_stock_library,
)
from tests.market_fixture import sample_ohlcv
from tests.test_market_data import fake_new_listing_fetcher


def catalog_frame(row_count: int = MIN_FULL_MARKET_ROWS + 1) -> pd.DataFrame:
    """Create a deterministic SH/SZ/BJ directory without using the network."""
    full_codes = (
        [f"{value:06d}" for value in range(1, 2_001)]
        + [f"{600_000 + value:06d}" for value in range(1, 2_001)]
        + ["830001"]
    )
    codes = full_codes[:row_count]
    if row_count >= 3 and "830001" not in codes:
        codes[-1] = "830001"
    names = {
        "000001": "平安银行",
        "600760": "中航沈飞",
        "830001": "北交示例",
    }
    return pd.DataFrame(
        {
            "代码": codes,
            "名称": [names.get(code, f"测试股票{code}") for code in codes],
            "最新价": [10.0] * len(codes),
            "成交量": [10_000] * len(codes),
            "成交额": [10_000_000] * len(codes),
        }
    )


def write_day_data(root: Path, symbol: str) -> Path:
    day_dir = root / "data" / symbol / "raw" / "day"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{symbol}_day_test.csv"
    sample_ohlcv(end="2026-07-10").to_csv(path, index=False)
    return path


def successful_updater(calls: list[dict[str, object]]):
    def update(root: str | Path, **kwargs):
        project_root = Path(root)
        calls.append({"root": project_root, **kwargs})
        path = write_day_data(project_root, str(kwargs["symbol"]))
        return {
            "symbol": kwargs["symbol"],
            "path": str(path.relative_to(project_root)),
            "latest_date": "2026-07-10",
        }

    return update


class StockCatalogTests(unittest.TestCase):
    def sync_complete_catalog(self, root: Path) -> dict[str, object]:
        return sync_stock_catalog(
            root,
            fetcher=lambda: catalog_frame(),
            today=date(2026, 7, 30),
        )

    def test_1200_row_catalog_is_not_available_as_complete_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            csv_path, metadata_path = stock_catalog_paths(root)
            csv_path.parent.mkdir(parents=True)
            catalog_frame(1_200).to_csv(csv_path, index=False)
            metadata_path.write_text(
                '{"is_full_market": true, "rows": 1200, "snapshot_date": "2026-07-29"}',
                encoding="utf-8",
            )

            status = stock_catalog_status(root)

            self.assertFalse(status["ready"])
            self.assertFalse(status["is_full_market"])
            self.assertEqual(1_200, status["rows"])
            self.assertEqual(MIN_FULL_MARKET_ROWS, status["minimum_rows"])
            with self.assertRaises(StockCatalogNotReadyError):
                load_full_stock_catalog(root)

    def test_sync_complete_catalog_writes_validated_directory_and_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            result = self.sync_complete_catalog(root)
            frame, metadata = load_full_stock_catalog(root)
            csv_path, metadata_path = stock_catalog_paths(root)

            self.assertTrue(result["ready"])
            self.assertTrue(result["is_full_market"])
            self.assertFalse(result["unchanged"])
            self.assertEqual(MIN_FULL_MARKET_ROWS + 1, result["rows"])
            self.assertEqual({"SH", "SZ", "BJ"}, set(frame["market"]))
            self.assertEqual("2026-07-30", metadata["snapshot_date"])
            self.assertTrue(result["catalog_hash"])
            self.assertTrue(csv_path.exists())
            self.assertTrue(metadata_path.exists())

    def test_incomplete_sync_does_not_replace_existing_complete_catalog(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.sync_complete_catalog(root)
            csv_path, metadata_path = stock_catalog_paths(root)
            original_csv = csv_path.read_bytes()
            original_metadata = metadata_path.read_bytes()

            with self.assertRaises(StockCatalogNotReadyError):
                sync_stock_catalog(
                    root,
                    fetcher=lambda: catalog_frame(1_200),
                    today=date(2026, 7, 31),
                )

            self.assertEqual(original_csv, csv_path.read_bytes())
            self.assertEqual(original_metadata, metadata_path.read_bytes())
            self.assertTrue(stock_catalog_status(root)["ready"])

    def test_search_by_code_and_name_includes_local_data_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.sync_complete_catalog(root)
            add_local_stock_member(
                root,
                symbol="600760.SH",
                stock_name="中航沈飞",
                catalog_snapshot_date="2026-07-30",
                added_at="2026-07-30T12:00:00+08:00",
            )
            write_day_data(root, "600760.SH")

            by_code = search_stock_catalog(root, "600760")
            by_name = search_stock_catalog(root, "中航")

            self.assertEqual("600760.SH", by_code["results"][0]["symbol"])
            self.assertEqual("中航沈飞", by_code["results"][0]["stock_name"])
            self.assertTrue(by_code["results"][0]["in_local_library"])
            self.assertEqual("READY", by_code["results"][0]["local_data_status"])
            self.assertEqual("2026-07-10", by_code["results"][0]["local_latest_date"])
            self.assertEqual("600760.SH", by_name["results"][0]["symbol"])
            self.assertEqual(1, by_name["total_matches"])

    def test_valid_catalog_stock_is_added_and_daily_data_is_imported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.sync_complete_catalog(root)
            calls: list[dict[str, object]] = []

            result = add_stock_from_catalog(
                root,
                {"symbol": "000001.SZ"},
                updater=successful_updater(calls),
            )
            members = read_local_stock_library(root)

            self.assertEqual("ADDED", result["membership_status"])
            self.assertEqual("IMPORTED", result["import_status"])
            self.assertTrue(result["data_saved"])
            self.assertEqual(["000001.SZ"], members["symbol"].tolist())
            self.assertEqual("平安银行", members.iloc[0]["stock_name"])
            self.assertEqual(1, len(calls))
            self.assertEqual("day", calls[0]["timeframe"])
            self.assertEqual("qfq", calls[0]["adjust"])
            self.assertEqual(3, calls[0]["history_years"])
            self.assertTrue(
                (root / "data" / "000001.SZ" / "raw" / "day").is_dir()
            )

    def test_new_listing_with_short_history_is_imported_without_warning(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.sync_complete_catalog(root)

            def new_listing_updater(project_root: str | Path, **kwargs):
                return update_market_data(
                    project_root,
                    **kwargs,
                    fetcher=fake_new_listing_fetcher,
                    today=date(2026, 7, 11),
                )

            result = add_stock_from_catalog(
                root,
                {"symbol": "830001.BJ"},
                updater=new_listing_updater,
            )

            self.assertEqual("ADDED", result["membership_status"])
            self.assertEqual("IMPORTED", result["import_status"])
            self.assertTrue(result["data_saved"])
            self.assertNotIn("import_error", result)
            self.assertEqual(8, result["data"]["rows"])

    def test_remote_failure_keeps_member_and_retry_can_import_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.sync_complete_catalog(root)

            def failing_updater(*_args, **_kwargs):
                raise MarketDataError("remote unavailable")

            failed = add_stock_from_catalog(
                root,
                {"symbol": "600760.SH"},
                updater=failing_updater,
            )
            members_after_failure = read_local_stock_library(root)
            calls: list[dict[str, object]] = []
            retried = add_stock_from_catalog(
                root,
                {"symbol": "600760.SH"},
                updater=successful_updater(calls),
            )
            members_after_retry = read_local_stock_library(root)

            self.assertEqual("ADDED", failed["membership_status"])
            self.assertEqual("FAILED", failed["import_status"])
            self.assertFalse(failed["data_saved"])
            self.assertIn("稍后重试", failed["import_error"])
            self.assertEqual(["600760.SH"], members_after_failure["symbol"].tolist())
            self.assertEqual("EXISTING", retried["membership_status"])
            self.assertEqual("IMPORTED", retried["import_status"])
            self.assertEqual(1, len(calls))
            self.assertEqual(1, len(members_after_retry))

    def test_duplicate_add_is_idempotent_and_does_not_reimport_existing_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.sync_complete_catalog(root)
            calls: list[dict[str, object]] = []
            add_stock_from_catalog(
                root,
                {"symbol": "000001.SZ"},
                updater=successful_updater(calls),
            )

            def should_not_run(*_args, **_kwargs):
                raise AssertionError("duplicate add must not update existing data")

            duplicate = add_stock_from_catalog(
                root,
                {"symbol": "000001.SZ"},
                updater=should_not_run,
            )

            self.assertEqual("EXISTING", duplicate["membership_status"])
            self.assertEqual("ALREADY_PRESENT", duplicate["import_status"])
            self.assertTrue(duplicate["already_present"])
            self.assertEqual(1, len(calls))
            self.assertEqual(1, len(read_local_stock_library(root)))

    def test_stock_not_in_complete_catalog_is_rejected_before_import(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.sync_complete_catalog(root)
            calls: list[dict[str, object]] = []

            with self.assertRaisesRegex(StockCatalogError, "不在当前完整沪深北目录"):
                add_stock_from_catalog(
                    root,
                    {"symbol": "688888.SH"},
                    updater=successful_updater(calls),
                )

            self.assertEqual([], calls)
            self.assertTrue(read_local_stock_library(root).empty)


if __name__ == "__main__":
    unittest.main()
