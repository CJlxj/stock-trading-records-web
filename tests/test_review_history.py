from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.review_history import ReviewHistoryStore


class ReviewHistoryTests(unittest.TestCase):
    @staticmethod
    def _result(
        review_date: str = "2026-07-11",
        symbol: str = "600760.SH",
        stock_name: str = "中航沈飞",
    ) -> dict:
        return {
            "meta": {
                "review_date": review_date,
                "symbol": symbol,
                "stock_name": stock_name,
                "timeframe": "day",
                "data_date": "2026-07-10",
            },
            "summary": {
                "technical_status": "WATCH",
                "permission": "继续观察",
                "position_pct": 0.2,
                "emotion_hit_count": 0,
                "conclusion": "继续观察。",
            },
        }

    def test_save_list_and_get_without_storing_csv_body(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewHistoryStore(temp_dir, Path(temp_dir) / "reviews.sqlite3")
            payload = {"symbol": "600760.SH", "csv_name": "quote.csv", "csv_text": "date,close\n2026-07-10,44"}
            result = {
                "meta": {
                    "review_date": "2026-07-11",
                    "symbol": "600760.SH",
                    "stock_name": "中航沈飞",
                    "timeframe": "day",
                    "data_date": "2026-07-10",
                },
                "summary": {
                    "technical_status": "WATCH",
                    "permission": "继续观察",
                    "position_pct": 0.2,
                    "emotion_hit_count": 0,
                    "conclusion": "继续观察。",
                },
            }
            stored = store.save(payload, result)
            review_id = stored["meta"]["history_id"]
            self.assertEqual(1, len(store.list()))
            detail = store.get(review_id)
            self.assertNotIn("csv_text", detail["request"])
            self.assertEqual("quote.csv", detail["request"]["csv_snapshot"]["name"])
            self.assertEqual(review_id, detail["result"]["meta"]["history_id"])

    def test_draft_is_separate_and_completion_inserts_formal_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewHistoryStore(temp_dir, Path(temp_dir) / "reviews.sqlite3")
            payload = {
                "symbol": "600760.SH",
                "stock_name": "中航沈飞",
                "review_date": "2026-07-11",
                "timeframe": "day",
                "review_intent": "daily",
            }
            draft = store.save_draft(payload)
            draft_id = draft["record"]["version_id"]
            self.assertEqual([], store.list())
            self.assertEqual("draft", store.list_drafts()[0]["status"])
            self.assertEqual("待生成", draft["record"]["data_date"])
            self.assertIsNone(store.get(draft_id)["result"])

            completed = store.save(
                {**payload, "draft_version_id": draft_id},
                self._result(),
            )
            records = store.list()
            self.assertEqual(1, len(records))
            self.assertEqual("completed", records[0]["status"])
            self.assertNotEqual(draft_id, completed["meta"]["version_id"])
            self.assertEqual(draft_id, records[0]["source_draft_id"])
            self.assertEqual([], store.list_drafts())
            self.assertEqual("converted", store.get(draft_id)["record"]["status"])
            self.assertEqual(1, records[0]["version_count"])

    def test_each_completion_is_an_independent_history_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewHistoryStore(temp_dir, Path(temp_dir) / "reviews.sqlite3")
            payload = {
                "symbol": "600760.SH",
                "stock_name": "中航沈飞",
                "review_date": "2026-07-11",
                "review_intent": "daily",
            }
            first = store.save(payload, self._result())
            second = store.save(
                {**payload, "base_version_id": first["meta"]["version_id"]},
                self._result("2026-07-12"),
            )
            third = store.save(payload, self._result("2026-07-10"))
            records = store.list()
            self.assertEqual(3, len(records))
            self.assertEqual(
                [third["meta"]["history_id"], second["meta"]["history_id"], first["meta"]["history_id"]],
                [record["id"] for record in records],
            )
            self.assertNotEqual(first["meta"]["review_id"], second["meta"]["review_id"])
            self.assertEqual(first["meta"]["history_id"], records[1]["supersedes_review_id"])
            self.assertEqual([], store.get(second["meta"]["version_id"])["versions"])

    def test_history_groups_keep_stocks_separate_and_records_page_by_ten(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewHistoryStore(temp_dir, Path(temp_dir) / "reviews.sqlite3")
            for day in range(1, 12):
                date_text = f"2026-07-{day:02d}"
                store.save(
                    {"symbol": "600760.SH", "stock_name": "中航沈飞", "review_date": date_text},
                    self._result(date_text),
                )
            store.save(
                {"symbol": "605507.SH", "stock_name": "国邦医药", "review_date": "2026-07-12"},
                self._result("2026-07-12", symbol="605507.SH", stock_name="国邦医药"),
            )

            grouped = store.list_groups()
            self.assertEqual(12, grouped["total"])
            self.assertEqual(["605507.SH", "600760.SH"], [group["symbol"] for group in grouped["groups"]])
            self.assertEqual(11, grouped["groups"][1]["record_count"])

            first_page = store.list(symbol="600760.SH", limit=10, offset=0)
            second_page = store.list(symbol="600760.SH", limit=10, offset=10)
            self.assertEqual(10, len(first_page))
            self.assertEqual(1, len(second_page))
            self.assertEqual("2026-07-11", first_page[0]["review_date"])
            self.assertEqual("2026-07-01", second_page[0]["review_date"])

    def test_draft_progress_and_soft_delete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewHistoryStore(temp_dir, Path(temp_dir) / "reviews.sqlite3")
            draft = store.save_draft(
                {
                    "symbol": "600760.SH",
                    "stock_name": "中航沈飞",
                    "review_date": "2026-07-11",
                    "timeframe": "day",
                    "review_intent": "daily",
                    "decision_window": "after_close",
                    "data_source": "local",
                    "account_total_asset": 140000,
                }
            )
            draft_id = draft["record"]["id"]
            listed = store.list_drafts()
            self.assertEqual(3, listed[0]["completed_steps"])
            self.assertEqual(["情绪与纪律"], listed[0]["missing_steps"])

            deleted = store.delete_draft(draft_id)
            self.assertEqual("deleted", deleted["status"])
            self.assertEqual([], store.list_drafts())
            self.assertEqual("deleted", store.get(draft_id)["record"]["status"])

    def test_open_plan_draft_uses_core_reason_and_exit_condition(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ReviewHistoryStore(temp_dir, Path(temp_dir) / "reviews.sqlite3")
            payload = {
                "symbol": "600760.SH",
                "stock_name": "中航沈飞",
                "review_date": "2026-07-11",
                "timeframe": "day",
                "review_intent": "open",
                "decision_window": "after_close",
                "data_source": "local",
                "account_total_asset": 140000,
                "planned_position_pct": 0.2,
                "planned_add_amount": 0,
                "buy_reason": "可复核的买入理由",
                "sell_condition": "可复核的退出条件",
                "emotion_reviewed": True,
            }
            store.save_draft(payload)
            self.assertNotIn("本股买卖计划", store.list_drafts()[0]["missing_steps"])

    def test_legacy_schema_is_migrated_without_changing_saved_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "reviews.sqlite3"
            result = self._result()
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE reviews (
                        id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        review_date TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        stock_name TEXT NOT NULL,
                        timeframe TEXT NOT NULL,
                        data_date TEXT NOT NULL,
                        technical_status TEXT NOT NULL,
                        permission TEXT NOT NULL,
                        position_pct REAL NOT NULL,
                        emotion_hit_count INTEGER NOT NULL,
                        conclusion TEXT NOT NULL,
                        request_json TEXT NOT NULL,
                        result_json TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy-review",
                        "2026-07-11T17:18:28+08:00",
                        "2026-07-11",
                        "600760.SH",
                        "中航沈飞",
                        "day",
                        "2026-07-10",
                        "WATCH",
                        "继续观察",
                        0.2,
                        0,
                        "继续观察。",
                        json.dumps({"symbol": "600760.SH"}, ensure_ascii=False),
                        json.dumps(result, ensure_ascii=False),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            store = ReviewHistoryStore(temp_dir, db_path)
            records = store.list()
            detail = store.get("legacy-review")
            self.assertEqual(1, len(records))
            self.assertEqual("completed", records[0]["status"])
            self.assertEqual("legacy-review", records[0]["review_group_id"])
            self.assertEqual(1, records[0]["version_no"])
            self.assertEqual("legacy", records[0]["rule_version"])
            self.assertEqual(result, detail["result"])


if __name__ == "__main__":
    unittest.main()
