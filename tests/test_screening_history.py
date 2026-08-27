from __future__ import annotations

from copy import deepcopy
import tempfile
import unittest
from pathlib import Path

from src.screening_history import ScreeningHistoryStore


class ScreeningHistoryTests(unittest.TestCase):
    def result(self) -> dict:
        return {
            "meta": {
                "created_at": "2026-07-12T18:00:00+08:00",
                "snapshot_date": "2026-07-12",
                "universe_source": "a_share_snapshot",
                "is_full_market": True,
                "rule_hash": "abc123",
            },
            "summary": {
                "universe_count": 3,
                "pass_count": 1,
                "fail_count": 1,
                "data_gap_count": 1,
                "technical_ready_count": 1,
                "technical_candidate_count": 0,
            },
            "rows": [{"symbol": "600001.SH", "selection_status": "PASS"}],
            "disclaimer": "test",
        }

    def test_saves_lists_and_restores_complete_batch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScreeningHistoryStore(root)
            saved = store.save(self.result())
            listed = store.list()
            latest = store.latest()
            restored = store.get(saved["meta"]["history_id"])
            self.assertEqual(1, len(listed))
            self.assertTrue(listed[0]["is_full_market"])
            self.assertEqual(saved, latest)
            self.assertEqual(saved, restored)

    def test_latest_can_be_scoped_to_the_local_library(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScreeningHistoryStore(root)
            local = deepcopy(self.result())
            local["schema_version"] = 2
            local["meta"].update(
                {
                    "created_at": "2026-07-12T18:00:00+08:00",
                    "universe_source": "project_symbols",
                    "universe_scope": "local_library",
                    "is_full_market": False,
                    "dataset_hash": "local-data",
                    "rule_set_id": "local-rules",
                    "rule_set_version": 1,
                    "input_fingerprint": "local-fingerprint",
                }
            )
            full_market = deepcopy(self.result())
            full_market["meta"]["created_at"] = "2026-07-12T19:00:00+08:00"
            saved_local = store.save(local)
            saved_full_market = store.save(full_market)

            self.assertEqual(saved_full_market, store.latest())
            self.assertEqual(
                saved_local,
                store.latest(universe_scope="local_library"),
            )


if __name__ == "__main__":
    unittest.main()
