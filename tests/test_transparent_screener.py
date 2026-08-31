from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from http import HTTPStatus
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from src.rules.registry import RuleRegistry
from src.rules.version_store import RuleSetError, RuleSetStore
from src.screening.dataset_manifest import build_dataset_manifest
from src.screening.engine import ScreeningCoordinator, ScreeningPreflightError
from src.screening_history import (
    DuplicateScreeningError,
    ScreeningHistoryStore,
)
from webapp.server import DashboardHandler


def _rule_payload(rule_id: str = "user.close_above_threshold") -> dict:
    return {
        "id": rule_id,
        "name": "收盘高于阈值",
        "description": "用于验证透明规则生命周期，不预测后续涨跌。",
        "kind": "scored",
        "group": "custom",
        "family": "test",
        "timeframe": "day",
        "lookback": 1,
        "inputs": ["close"],
        "params_schema": {
            "threshold": {
                "type": "number",
                "default": 10,
                "min": 0,
                "max": 100,
            }
        },
        "implementation": {
            "type": "expression",
            "expression": "close > threshold",
        },
        "missing_policy": "DATA_GAP",
        "plain_template": "收盘价高于 {threshold}",
        "tests": [
            {
                "name": "通过用例",
                "expected": "PASS",
                "frame": {"close": [9.0, 11.0]},
                "params": {"threshold": 10},
            },
            {
                "name": "失败用例",
                "expected": "FAIL",
                "frame": {"close": [11.0, 9.0]},
                "params": {"threshold": 10},
            },
        ],
    }


def _screen_result(
    fingerprint: str,
    *,
    computable_count: int = 1,
    data_gap_count: int = 0,
    uniform_date: bool = True,
) -> dict:
    return {
        "schema_version": 2,
        "meta": {
            "created_at": "2026-07-26T18:00:00+08:00",
            "snapshot_date": "2026-07-24",
            "as_of_trade_date": "2026-07-24",
            "candidate_for_trade_date": None,
            "candidate_for_label": "下一有效交易日",
            "universe_source": "project_symbols",
            "universe_provider": "项目本地数据",
            "is_full_market": False,
            "rule_hash": "rule-set-hash",
            "rule_set_id": "after_close",
            "rule_set_version": 1,
            "rule_set_name": "收盘规则",
            "dataset_hash": "dataset-hash",
            "engine_version": "transparent-screener-v1",
            "engine_build_hash": "engine-hash",
            "input_fingerprint": fingerprint,
            "market_context": "unknown",
        },
        "summary": {
            "universe_count": 1,
            "pass_count": 1,
            "fail_count": 0,
            "data_gap_count": data_gap_count,
            "technical_ready_count": computable_count,
            "technical_candidate_count": 0,
        },
        "dataset_manifest": {
            "discovered_count": computable_count + data_gap_count,
            "computable_count": computable_count,
            "data_gap_count": data_gap_count,
            "uniform_date": uniform_date,
            "adjust_modes": ["qfq"],
        },
        "row_results": [],
        "rows": [],
    }


def _stored_result(fingerprint: str, observed_close: float = 11.0) -> dict:
    result = _screen_result(fingerprint)
    evidence = {
        "rule_id": "user.close_above_threshold",
        "rule_version": 1,
        "name": "收盘高于阈值",
        "kind": "scored",
        "group": "custom",
        "status": "PASS",
        "boolean_result": True,
        "observed": {"close": observed_close},
        "comparisons": [{"left": observed_close, "operator": ">", "right": 10}],
        "plain_explanation": f"收盘价 {observed_close} 高于阈值 10。",
        "data_date": "2026-07-24",
        "normalized_expression": "close > threshold",
        "params": {"threshold": 10},
        "missing_policy": "DATA_GAP",
        "source_path": "rules/catalog/user/test/v1.yaml",
        "source_symbol": "user.close_above_threshold",
        "source_hash": "source-hash",
        "definition_hash": "definition-hash",
        "error": None,
    }
    row = {
        "symbol": "600001.SH",
        "stock_name": "测试股票",
        "selection_status": "PASS",
        "technical_status": "BUY_CANDIDATE",
        "matched_rule_count": 1,
        "active_rule_count": 1,
        "required_passed": True,
        "veto_triggered": False,
        "history_date": "2026-07-24",
        "failed_candidate_rules": [],
        "rule_evidence": [evidence],
    }
    result["rows"] = [deepcopy(row)]
    result["row_results"] = [deepcopy(row)]
    return result


class DatasetManifestRegressionTests(unittest.TestCase):
    @staticmethod
    def _write_day_data(root: Path, symbol: str, dates: list[str]) -> None:
        day_dir = root / "data" / symbol / "raw" / "day"
        day_dir.mkdir(parents=True)
        rows = len(dates)
        filename = f"{symbol}_day_{dates[-1].replace('-', '')}.csv"
        pd.DataFrame(
            {
                "date": dates,
                "open": [10.0] * rows,
                "high": [10.5] * rows,
                "low": [9.5] * rows,
                "close": [10.2] * rows,
                "volume": [1000] * rows,
            }
        ).to_csv(day_dir / filename, index=False)

    def test_manifest_excludes_rows_after_as_of_and_records_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            day_dir = root / "data" / "600001.SH" / "raw" / "day"
            day_dir.mkdir(parents=True)
            pd.DataFrame(
                {
                    "date": ["2026-07-23", "2026-07-24", "2026-07-25"],
                    "open": [9.8, 10.0, 99.0],
                    "high": [10.2, 10.5, 100.0],
                    "low": [9.7, 9.9, 98.0],
                    "close": [10.0, 10.3, 99.5],
                    "volume": [1000, 1200, 999999],
                }
            ).to_csv(day_dir / "600001.SH_day_20260725.csv", index=False)

            manifest = build_dataset_manifest(
                root,
                symbols=["600001.SH"],
                as_of_trade_date="2026-07-24",
                reference_date="2026-07-26",
            )

            entry = manifest["entries"][0]
            self.assertEqual("2026-07-24", manifest["as_of_trade_date"])
            self.assertEqual(2, entry["rows_used"])
            self.assertEqual(1, entry["future_rows_excluded"])
            self.assertEqual("2026-07-24", entry["last_used_trade_date"])
            self.assertEqual("READY", entry["status"])
            self.assertIn("1 行晚于目标收盘日，已排除", entry["anomalies"])

    def test_manifest_marks_all_entries_aligned_to_current_date(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for symbol in ("600001.SH", "000001.SZ"):
                self._write_day_data(root, symbol, ["2026-07-23", "2026-07-24"])

            manifest = build_dataset_manifest(
                root,
                symbols=["600001.SH", "000001.SZ"],
                reference_date="2026-07-24",
            )

            self.assertEqual("2026-07-24", manifest["current_date"])
            self.assertEqual("2026-07-24", manifest["actual_common_trade_date"])
            self.assertEqual("CURRENT_DATE_ALIGNED", manifest["alignment_status"])
            self.assertTrue(manifest["is_aligned_to_current_date"])

    def test_manifest_keeps_consistent_non_current_data_distinct_from_hash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for symbol in ("600001.SH", "000001.SZ"):
                self._write_day_data(root, symbol, ["2026-07-23", "2026-07-24"])

            current = build_dataset_manifest(
                root,
                symbols=["600001.SH", "000001.SZ"],
                reference_date="2026-07-24",
            )
            later = build_dataset_manifest(
                root,
                symbols=["600001.SH", "000001.SZ"],
                reference_date="2026-07-26",
            )

            self.assertEqual("2026-07-26", later["current_date"])
            self.assertEqual("2026-07-24", later["actual_common_trade_date"])
            self.assertEqual("ALIGNED_NOT_CURRENT", later["alignment_status"])
            self.assertFalse(later["is_aligned_to_current_date"])
            self.assertEqual(current["dataset_hash"], later["dataset_hash"])

    def test_manifest_reports_partial_alignment_for_mixed_dates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_day_data(root, "600001.SH", ["2026-07-23", "2026-07-24"])
            self._write_day_data(root, "000001.SZ", ["2026-07-22", "2026-07-23"])

            manifest = build_dataset_manifest(
                root,
                symbols=["600001.SH", "000001.SZ"],
                reference_date="2026-07-24",
            )

            self.assertEqual("2026-07-23", manifest["actual_common_trade_date"])
            self.assertEqual("PARTIAL", manifest["alignment_status"])
            self.assertFalse(manifest["is_aligned_to_current_date"])
            self.assertEqual(
                {"READY": 1, "STALE_FOR_RUN": 1},
                manifest["status_counts"],
            )

    def test_manifest_has_no_common_date_when_any_entry_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_day_data(root, "600001.SH", ["2026-07-24"])

            manifest = build_dataset_manifest(
                root,
                symbols=["600001.SH", "000001.SZ"],
                reference_date="2026-07-24",
            )

            self.assertIsNone(manifest["actual_common_trade_date"])
            self.assertEqual("PARTIAL", manifest["alignment_status"])
            self.assertFalse(manifest["is_aligned_to_current_date"])
            self.assertEqual({"MISSING": 1, "READY": 1}, manifest["status_counts"])

    def test_manifest_default_current_date_uses_shanghai_timezone(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_day_data(root, "600001.SH", ["2026-07-24"])
            observed_timezones = []

            class FixedDateTime:
                @classmethod
                def now(cls, timezone):
                    observed_timezones.append(timezone)
                    return datetime(2026, 7, 24, 0, 30, tzinfo=timezone)

            with patch("src.screening.dataset_manifest.datetime", FixedDateTime):
                manifest = build_dataset_manifest(root, symbols=["600001.SH"])

            self.assertEqual("Asia/Shanghai", observed_timezones[0].key)
            self.assertEqual("2026-07-24", manifest["current_date"])
            self.assertEqual("CURRENT_DATE_ALIGNED", manifest["alignment_status"])


class RuleLifecycleRegressionTests(unittest.TestCase):
    def test_rule_versions_follow_draft_validated_active_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            registry = RuleRegistry(root)
            payload = _rule_payload()

            draft_v1 = registry.create(payload)
            v1_path = root / draft_v1["source_path"]
            original_v1 = v1_path.read_text(encoding="utf-8")
            self.assertEqual(1, draft_v1["version"])
            self.assertEqual("DRAFT", draft_v1["status"])

            validated_v1 = registry.validate(draft_v1["id"], draft_v1["version"])
            self.assertEqual("VALIDATED", validated_v1["status"])
            self.assertEqual(2, validated_v1["validation"]["passed_case_count"])

            active_v1 = registry.activate(draft_v1["id"], draft_v1["version"])
            self.assertEqual("ACTIVE", active_v1["status"])

            draft_v2 = registry.create(payload)
            self.assertEqual(2, draft_v2["version"])
            self.assertEqual("DRAFT", draft_v2["status"])
            self.assertEqual(original_v1, v1_path.read_text(encoding="utf-8"))
            self.assertNotEqual(v1_path, root / draft_v2["source_path"])

            registry.validate(draft_v2["id"], draft_v2["version"])
            registry.activate(draft_v2["id"], draft_v2["version"])
            self.assertEqual("SUPERSEDED", registry.get(draft_v1["id"], 1)["status"])
            self.assertEqual("ACTIVE", registry.get(draft_v2["id"], 2)["status"])

    def test_rule_set_validation_rejects_draft_rule_then_accepts_active_rule(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            registry = RuleRegistry(root)
            draft = registry.create(_rule_payload())
            store = RuleSetStore(root)
            rule_set = store.create(
                {
                    "id": "after_close_test",
                    "name": "收盘规则测试",
                    "description": "验证方案只能引用已激活规则。",
                    "base_gates": {"rules": []},
                    "required": {"rules": []},
                    "veto": {"rules": []},
                    "scored": {
                        "mode": "equal_score",
                        "rules": [f"{draft['id']}@{draft['version']}"],
                        "pass_ratio": 0.65,
                        "error_policy": "block_candidate",
                    },
                    "near_policy": {
                        "score_ratio": 0.45,
                        "require_no_veto": True,
                    },
                    "group_policy": {
                        "minimum_groups": 1,
                        "group_pass_ratio": 0.5,
                    },
                }
            )

            with self.assertRaises(RuleSetError) as raised:
                store.validate(rule_set["id"], rule_set["version"])
            self.assertEqual("RULE_SET_INACTIVE_RULE", raised.exception.code)

            registry.validate(draft["id"], draft["version"])
            registry.activate(draft["id"], draft["version"])
            validated = store.validate(rule_set["id"], rule_set["version"])
            self.assertEqual("VALIDATED", validated["status"])


class ScreeningCoordinatorRegressionTests(unittest.TestCase):
    def test_preflight_and_run_reuse_the_same_reference_date(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            coordinator = ScreeningCoordinator(root)
            fixed_reference_date = date(2026, 7, 24)

            with patch("src.screening.engine.date") as clock:
                clock.today.return_value = fixed_reference_date
                with patch(
                    "src.screening.engine.screen_universe",
                    return_value=_stored_result("fixed-reference-date"),
                ) as screen:
                    preflight = coordinator.preflight({})
                    coordinator.run(preflight["preflight_id"])

            clock.today.assert_called_once_with()
            self.assertEqual(2, screen.call_count)
            self.assertTrue(
                all(
                    call.kwargs.get("today") == fixed_reference_date
                    for call in screen.call_args_list
                )
            )

    def test_preflight_reports_blocking_state_and_duplicate_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            coordinator = ScreeningCoordinator(root)
            blocked_result = _screen_result(
                "blocked-fingerprint",
                computable_count=0,
                data_gap_count=2,
                uniform_date=False,
            )
            with patch(
                "src.screening.engine.screen_universe",
                return_value=blocked_result,
            ):
                blocked = coordinator.preflight({})
            self.assertFalse(blocked["can_run"])
            self.assertTrue(blocked["blocking_errors"])
            self.assertEqual(2, blocked["dataset"]["data_gap_count"])

            existing = ScreeningHistoryStore(root).save(
                _stored_result("duplicate-fingerprint")
            )
            with patch(
                "src.screening.engine.screen_universe",
                return_value=_screen_result("duplicate-fingerprint"),
            ):
                duplicate = coordinator.preflight({})
            self.assertTrue(duplicate["duplicate_run"]["exists"])
            self.assertEqual(
                existing["meta"]["history_id"],
                duplicate["duplicate_run"]["run_id"],
            )

    def test_run_rejects_stale_preflight_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            coordinator = ScreeningCoordinator(root)
            with patch(
                "src.screening.engine.screen_universe",
                side_effect=[
                    _screen_result("fingerprint-before"),
                    _screen_result("fingerprint-after"),
                ],
            ):
                preflight = coordinator.preflight({})
                with self.assertRaises(ScreeningPreflightError) as raised:
                    coordinator.run(preflight["preflight_id"])

            self.assertEqual("PREFLIGHT_STALE", raised.exception.code)
            self.assertEqual(0, ScreeningHistoryStore(root).count())


class ScreeningHistoryRegressionTests(unittest.TestCase):
    def test_duplicate_error_maps_to_conflict_and_evidence_is_frozen(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScreeningHistoryStore(root)
            original = _stored_result("frozen-fingerprint", observed_close=11.0)
            saved = store.save(original)
            run_id = saved["meta"]["history_id"]

            original["row_results"][0]["rule_evidence"][0]["observed"]["close"] = 99.0
            original["rows"][0]["rule_evidence"][0]["observed"]["close"] = 99.0
            frozen = store.evidence(run_id, "600001.SH")
            self.assertIsNotNone(frozen)
            self.assertEqual(
                11.0,
                frozen["evidence"][0]["observed"]["close"],
            )
            self.assertEqual(
                "definition-hash",
                frozen["evidence"][0]["definition_hash"],
            )

            with self.assertRaises(DuplicateScreeningError) as raised:
                store.save(_stored_result("frozen-fingerprint"))
            self.assertEqual("DUPLICATE_RUN", raised.exception.code)
            self.assertEqual(run_id, raised.exception.run_id)

            captured: dict = {}

            class ErrorCapture:
                def _send_json(self, payload, status):
                    captured["payload"] = payload
                    captured["status"] = status

            DashboardHandler._structured_error(
                ErrorCapture(),
                raised.exception,
                status=HTTPStatus.CONFLICT,
            )
            self.assertEqual(HTTPStatus.CONFLICT, captured["status"])
            self.assertEqual("DUPLICATE_RUN", captured["payload"]["error_code"])
            self.assertEqual(run_id, captured["payload"]["duplicate_run_id"])

            duplicate = store.save(
                _stored_result("frozen-fingerprint"),
                confirm_duplicate=True,
            )
            self.assertNotEqual(run_id, duplicate["meta"]["history_id"])
            self.assertEqual(2, store.count())


if __name__ == "__main__":
    unittest.main()
