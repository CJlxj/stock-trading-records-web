from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.review_cases import ReviewCaseStore
from src.review_history import ReviewHistoryStore


class ReviewCaseTests(unittest.TestCase):
    def test_manual_case_is_persisted_and_can_be_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "reviews.sqlite3"
            store = ReviewCaseStore(temp_dir, db_path)
            store.upsert(
                {
                    "symbol": "600519.SH",
                    "stock_name": "贵州茅台",
                    "lifecycle_status": "WATCHING",
                    "source": "manual",
                }
            )
            watching = store.list()
            self.assertEqual("WATCHING", watching[0]["status"])

            store.upsert(
                {
                    "symbol": "600519.SH",
                    "stock_name": "贵州茅台",
                    "lifecycle_status": "CLOSED",
                    "source": "manual",
                }
            )
            self.assertEqual("CLOSED", store.list()[0]["status"])

    def test_position_overrides_manual_lifecycle_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "reviews.sqlite3"
            store = ReviewCaseStore(temp_dir, db_path)
            store.upsert(
                {
                    "symbol": "600760.SH",
                    "stock_name": "中航沈飞",
                    "lifecycle_status": "CLOSED",
                    "source": "project",
                }
            )
            cases = store.list(
                [
                    {
                        "symbol": "600760.SH",
                        "stock_name": "中航沈飞",
                        "defaults": {
                            "position_shares": 800,
                            "account_total_asset": 140000,
                            "current_price": 44.38,
                        },
                        "has_trades": True,
                        "in_watchlist": True,
                        "has_local_data": True,
                    }
                ]
            )
            self.assertEqual("HOLDING", cases[0]["status"])
            self.assertEqual(800, cases[0]["position_shares"])

    def test_latest_review_populates_summary_and_discipline_counts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "reviews.sqlite3"
            history = ReviewHistoryStore(temp_dir, db_path)
            cases = ReviewCaseStore(temp_dir, db_path)
            payload = {
                "symbol": "000001.SZ",
                "stock_name": "平安银行",
                "position_shares": 1000,
                "avg_cost": 10.2,
                "current_price": 10.8,
                "take_profit_price": 12.5,
                "stop_loss_price": 9.7,
                "buy_reason": "可复核理由",
                "sell_condition": "可复核退出条件",
            }
            result = {
                "meta": {
                    "review_date": "2026-07-12",
                    "symbol": "000001.SZ",
                    "stock_name": "平安银行",
                    "timeframe": "day",
                    "data_date": "2026-07-11",
                },
                "summary": {
                    "technical_status": "WATCH",
                    "permission": "不允许临时操作",
                    "position_pct": 0.18,
                    "emotion_hit_count": 1,
                    "conclusion": "继续观察。",
                },
            }
            history.save(payload, result)
            cases.ensure_from_review(payload)

            item = cases.list()[0]
            self.assertEqual("HOLDING", item["status"])
            self.assertEqual(10.2, item["avg_cost"])
            self.assertEqual(12.5, item["target_price"])
            self.assertTrue(item["plan_ready"])
            self.assertEqual(1, item["review_count"])
            self.assertEqual(1, item["discipline_block_count"])
            self.assertEqual(0, item["discipline_clear_count"])

    def test_backfilled_history_does_not_replace_latest_business_date(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "reviews.sqlite3"
            history = ReviewHistoryStore(temp_dir, db_path)
            cases = ReviewCaseStore(temp_dir, db_path)

            def save_review(review_date: str, shares: int, permission: str) -> None:
                payload = {
                    "symbol": "000001.SZ",
                    "stock_name": "平安银行",
                    "position_shares": shares,
                    "avg_cost": 10.2,
                    "current_price": 10.8,
                }
                result = {
                    "meta": {
                        "review_date": review_date,
                        "symbol": "000001.SZ",
                        "stock_name": "平安银行",
                        "timeframe": "day",
                        "data_date": review_date,
                    },
                    "summary": {
                        "technical_status": "WATCH",
                        "permission": permission,
                        "position_pct": 0.18 if shares else 0,
                        "emotion_hit_count": 0,
                        "conclusion": "继续观察。",
                    },
                }
                history.save(payload, result)

            save_review("2026-07-12", 1000, "继续观察")
            save_review("2026-06-01", 0, "继续观察")

            item = cases.list()[0]
            self.assertEqual("2026-07-12", item["last_review_date"])
            self.assertEqual(1000, item["position_shares"])
            self.assertEqual("HOLDING", item["status"])
            self.assertEqual(2, item["review_count"])

    def test_holding_status_cannot_be_submitted_directly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewCaseStore(temp_dir, Path(temp_dir) / "reviews.sqlite3")
            with self.assertRaisesRegex(ValueError, "持仓中由持仓股数自动判断"):
                store.upsert(
                    {
                        "symbol": "600519.SH",
                        "lifecycle_status": "HOLDING",
                    }
                )

    def test_candidate_provenance_survives_case_and_review_updates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewCaseStore(temp_dir, Path(temp_dir) / "reviews.sqlite3")
            context = {
                "screening_run_id": "screen-20260714",
                "screening_created_at": "2026-07-14T16:30:00+08:00",
                "snapshot_date": "2026-07-14",
                "candidate_priority": "HIGH",
                "candidate_score_ratio": 0.92,
            }
            store.upsert(
                {
                    "symbol": "600760.SH",
                    "stock_name": "中航沈飞",
                    "lifecycle_status": "WATCHING",
                    "source": "candidate",
                    "candidate_context": context,
                }
            )
            store.ensure_from_review(
                {
                    "symbol": "600760.SH",
                    "stock_name": "中航沈飞",
                    "screening_context": context,
                }
            )

            item = store.list()[0]
            self.assertEqual("candidate", item["source"])
            self.assertEqual("screen-20260714", item["candidate_context"]["screening_run_id"])
            self.assertEqual(0.92, item["candidate_context"]["candidate_score_ratio"])


if __name__ == "__main__":
    unittest.main()
