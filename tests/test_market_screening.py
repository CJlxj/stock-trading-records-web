from __future__ import annotations

from datetime import date
import shutil
import tempfile
import unittest
from pathlib import Path

import pandas as pd
import yaml

from src.market_screening import (
    ScreeningError,
    fetch_eastmoney_universe,
    load_universe_snapshot,
    normalize_eastmoney_universe_payload,
    normalize_universe_snapshot,
    screen_universe,
    universe_status,
)
from src.rules.simple_editor import (
    apply_named_simple_rule_set,
    ensure_rule_catalog_seeded,
    save_library_rule,
)
from src.rules.version_store import RuleSetStore
from src.screening_history import ScreeningHistoryStore
from tests.market_fixture import sample_ohlcv


def _library_rule(root: Path, rule_key: str, name: str, expression: str) -> dict:
    """Save one rule in the library and hand back the combination reference."""

    saved = save_library_rule(
        root,
        {
            "request_id": f"library_{rule_key}"[:80],
            "name": name,
            "expression": expression,
            "description": f"{name}，仅用于收盘后筛选。",
        },
    )
    return {"ref": saved["ref"]}


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def fake_spot_snapshot() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "代码": ["600001", "000002", "830001"],
            "名称": ["示例沪股", "*ST 示例", "示例北股"],
            "最新价": [10.5, 4.2, 8.8],
            "成交量": [12000, 8000, 0],
            "成交额": [126_000_000, 33_600_000, 0],
        }
    )


class MarketScreeningTests(unittest.TestCase):
    def make_project(self, temp_dir: str) -> Path:
        root = Path(temp_dir)
        (root / "config").mkdir(parents=True)
        rules_path = root / "config" / "strategy_rules.yaml"
        shutil.copy(PROJECT_ROOT / "config" / "strategy_rules.yaml", rules_path)
        rules = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
        rules["selection_rules"].update(
            {
                "allowed_markets": ["SH", "SZ"],
                "exclude_st": True,
                "exclude_suspended": True,
            }
        )
        rules_path.write_text(
            yaml.safe_dump(rules, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        shutil.copytree(
            PROJECT_ROOT / "rules" / "catalog" / "builtin",
            root / "rules" / "catalog" / "builtin",
        )
        ensure_rule_catalog_seeded(root)
        return root

    def test_normalizes_a_share_codes_and_volume_units(self):
        frame = normalize_universe_snapshot(fake_spot_snapshot(), snapshot_date=date(2026, 7, 12))
        self.assertEqual(["000002.SZ", "600001.SH", "830001.BJ"], frame["symbol"].tolist())
        sh_row = frame[frame["symbol"] == "600001.SH"].iloc[0]
        self.assertEqual(1_200_000, sh_row["latest_volume"])
        self.assertEqual("2026-07-12", sh_row["data_date"])

    def test_normalizes_eastmoney_full_market_payload(self):
        payload = {
            "data": {
                "total": 2,
                "diff": [
                    {"f12": "600760", "f14": "中航沈飞", "f2": 45.1, "f5": 1234, "f6": 5_000_000, "f124": 0},
                    {"f12": "920943", "f14": "优机股份", "f2": 18.7, "f5": 800, "f6": 1_000_000, "f124": 0},
                ],
            }
        }
        raw = normalize_eastmoney_universe_payload(payload, snapshot_date=date(2026, 7, 14))
        normalized = normalize_universe_snapshot(raw, snapshot_date=date(2026, 7, 14))
        self.assertEqual(["600760.SH", "920943.BJ"], normalized["symbol"].tolist())
        self.assertEqual(123_400, normalized.iloc[0]["latest_volume"])

    def test_eastmoney_full_market_fetcher_reads_multiple_pages(self):
        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                import json

                return json.dumps(self.body, ensure_ascii=False).encode("utf-8")

        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            page = len(calls)
            code = "600760" if page == 1 else "000001"
            return Response(
                {
                    "data": {
                        "total": 2,
                        "diff": [{"f12": code, "f14": code, "f2": 10, "f5": 1, "f6": 100, "f124": 0}],
                    }
                }
            )

        result = fetch_eastmoney_universe(page_size=500, opener=opener, minimum_rows=2)
        self.assertEqual(2, len(result))
        self.assertEqual(2, len(calls))
        self.assertIn("pz=100", calls[0])
        self.assertIn("np=2", calls[0])
        self.assertIn("ut=bd1d9ddb04089700cf9c27f6f7426281", calls[0])

    def test_eastmoney_partial_market_response_is_rejected(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b'{"data":{"total":1,"diff":[{"f12":"600760","f14":"test"}]}}'

        with self.assertRaisesRegex(ScreeningError, "不会覆盖本地快照"):
            fetch_eastmoney_universe(opener=lambda *_args, **_kwargs: Response(), minimum_rows=2)

    def test_cached_partial_snapshot_is_not_labeled_full_market(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            universe_dir = root / "data" / "universe"
            universe_dir.mkdir(parents=True)
            pd.DataFrame(
                [{"symbol": "600760.SH", "stock_name": "中航沈飞"}]
            ).to_csv(root / "watchlist.csv", index=False)
            normalize_universe_snapshot(fake_spot_snapshot(), date(2026, 7, 12)).to_csv(
                universe_dir / "a_share_snapshot.csv", index=False
            )
            (universe_dir / "a_share_snapshot.meta.json").write_text(
                '{"is_full_market": true, "rows": 3, "snapshot_date": "2026-07-12"}',
                encoding="utf-8",
            )

            frame, metadata = load_universe_snapshot(root)
            status = universe_status(root)
            self.assertFalse(metadata["is_full_market"])
            self.assertEqual("project_symbols", metadata["source"])
            self.assertEqual(["600760.SH"], frame["symbol"].tolist())
            self.assertFalse(status["is_full_market"])
            self.assertTrue(status["is_partial_market"])

    def test_full_snapshot_runs_batch_admission_rules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_project(temp_dir)
            result = screen_universe(
                root,
                {"refresh_universe": True},
                fetcher=fake_spot_snapshot,
                today=date(2026, 7, 12),
            )
            statuses = {row["symbol"]: row["selection_status"] for row in result["rows"]}
            self.assertTrue(result["meta"]["is_full_market"])
            self.assertEqual(3, result["summary"]["universe_count"])
            self.assertEqual("PASS", statuses["600001.SH"])
            self.assertEqual("FAIL", statuses["000002.SZ"])
            self.assertEqual("FAIL", statuses["830001.BJ"])
            self.assertEqual(1, result["summary"]["pass_count"])
            self.assertEqual(0, result["summary"]["candidate_count"])
            self.assertTrue(result["summary"]["zero_candidate_is_valid"])
            sh_row = next(row for row in result["rows"] if row["symbol"] == "600001.SH")
            self.assertEqual("DATA", sh_row["priority_level"])
            self.assertEqual("unknown", result["meta"]["market_context"])

    def test_formal_screen_rejects_noncurrent_rule_set_id_or_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_project(temp_dir)
            active = apply_named_simple_rule_set(
                root,
                {
                    "request_id": "formal_active_rule",
                    "name": "正式启用组合",
                    "rules": [
                        _library_rule(root, "formal_active", "正式规则", "close > ma20")
                    ],
                },
            )
            inactive = apply_named_simple_rule_set(
                root,
                {
                    "request_id": "formal_inactive_rule",
                    "name": "未启用组合",
                    "rules": [
                        _library_rule(root, "formal_inactive", "未启用规则", "rsi14 <= 70")
                    ],
                },
            )
            RuleSetStore(root).activate(active["version"]["id"], 1)

            for request in (
                {"rule_set_id": inactive["version"]["id"]},
                {
                    "rule_set_id": active["version"]["id"],
                    "rule_set_version": 2,
                },
            ):
                with self.subTest(request=request), self.assertRaises(
                    ScreeningError
                ) as raised:
                    screen_universe(
                        root,
                        {**request, "refresh_universe": True},
                        fetcher=fake_spot_snapshot,
                        today=date(2026, 7, 12),
                    )
                self.assertEqual(
                    "FORMAL_SCREEN_ACTIVE_RULE_SET_REQUIRED",
                    raised.exception.code,
                )

    def test_simple_two_stage_runs_secondary_only_after_primary_and_requires_all(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_project(temp_dir)
            pd.DataFrame(
                [{"symbol": "600001.SH", "stock_name": "两级筛选样本"}]
            ).to_csv(root / "watchlist.csv", index=False)
            day_dir = root / "data" / "600001.SH" / "raw" / "day"
            day_dir.mkdir(parents=True)
            history = sample_ohlcv()
            dates = pd.to_datetime(history["date"])
            history["date"] = dates + (pd.Timestamp("2026-07-12") - dates.max())
            history.to_csv(day_dir / "600001.SH_day_two_stage.csv", index=False)

            created = apply_named_simple_rule_set(
                root,
                {
                    "request_id": "market_two_stage_rules",
                    "name": "正式两级组合",
                    "rules": [
                        _library_rule(root, "market_primary_pass", "一级恒真样本", "close > 0")
                    ],
                    "secondary_rules": [
                        _library_rule(
                            root, "market_secondary_pass", "二级通过样本", "close > 0"
                        ),
                        _library_rule(
                            root, "market_secondary_fail", "二级失败样本", "close < 0"
                        ),
                    ],
                },
            )
            RuleSetStore(root).activate(created["version"]["id"], 1)

            result = screen_universe(
                root,
                {"universe_scope": "local_library"},
                today=date(2026, 7, 12),
            )
            row = result["rows"][0]
            history_store = ScreeningHistoryStore(root)
            saved = history_store.save(result)
            saved_id = saved["meta"]["history_id"]
            apply_named_simple_rule_set(
                root,
                {
                    "request_id": "market_two_stage_edit_after_run",
                    "base_version": 1,
                    "expected_rule_set_hash": created["version"]["rule_set_hash"],
                    "name": "正式两级组合",
                    "rules": [{"ref": created["rule_refs"][0]}],
                    "secondary_rules": [],
                },
                rule_set_id=created["version"]["id"],
            )
            frozen = history_store.get(saved_id)

        self.assertTrue(row["primary_candidate"])
        self.assertEqual("FAIL", row["secondary_status"])
        self.assertEqual(1, row["secondary_matched_rule_count"])
        self.assertEqual(2, row["secondary_rule_count"])
        self.assertFalse(row["final_candidate"])
        self.assertEqual(0, result["summary"]["candidate_count"])
        self.assertEqual(2, result["summary"]["secondary_minimum_match"])
        self.assertEqual("ALL", result["summary"]["secondary_match_mode"])
        self.assertEqual(
            created["secondary_rule_refs"],
            frozen["rule_set_snapshot"]["rule_set"]["secondary"]["rules"],
        )
        self.assertEqual("FAIL", frozen["rows"][0]["secondary_status"])
        self.assertEqual(
            result["meta"]["rule_hash"], frozen["meta"]["rule_hash"]
        )

    def test_missing_history_is_data_gap_when_average_amount_is_required(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_project(temp_dir)
            rules_path = root / "config" / "strategy_rules.yaml"
            rules = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
            rules["selection_rules"]["min_avg_amount_20d"] = 10_000_000
            rules_path.write_text(yaml.safe_dump(rules, allow_unicode=True, sort_keys=False), encoding="utf-8")
            result = screen_universe(
                root,
                {"refresh_universe": True},
                fetcher=fake_spot_snapshot,
                today=date(2026, 7, 12),
            )
            sh_row = next(row for row in result["rows"] if row["symbol"] == "600001.SH")
            self.assertEqual("DATA_GAP", sh_row["selection_status"])
            self.assertIn("20 日平均成交额", sh_row["reason"])

    def test_zero_final_candidates_keeps_near_miss_diagnostics_without_relaxing_rules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_project(temp_dir)
            day_dir = root / "data" / "600001.SH" / "raw" / "day"
            day_dir.mkdir(parents=True)
            history = sample_ohlcv()
            dates = pd.to_datetime(history["date"])
            history["date"] = dates + (pd.Timestamp("2026-07-12") - dates.max())
            history.loc[history.index[-1], "volume"] = 1_000_000
            history.to_csv(day_dir / "600001.SH_day_test.csv", index=False)
            rules_path = root / "config" / "strategy_rules.yaml"
            rules = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
            rules["candidate_framework"]["pass_score_ratio"] = 0.90
            rules["candidate_framework"]["priority_high_ratio"] = 0.95
            rules_path.write_text(
                yaml.safe_dump(rules, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )

            result = screen_universe(
                root,
                {"refresh_universe": True},
                fetcher=fake_spot_snapshot,
                today=date(2026, 7, 12),
            )
            sh_row = next(row for row in result["rows"] if row["symbol"] == "600001.SH")
            self.assertEqual("PASS", sh_row["selection_status"])
            self.assertEqual("WATCH", sh_row["technical_status"])
            self.assertEqual(0, result["summary"]["candidate_count"])
            self.assertEqual(1, result["summary"]["near_miss_count"])
            self.assertTrue(result["summary"]["bottleneck_rules"])

    def test_stale_history_never_enters_final_candidate_pool(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_project(temp_dir)
            day_dir = root / "data" / "600001.SH" / "raw" / "day"
            day_dir.mkdir(parents=True)
            sample_ohlcv().to_csv(day_dir / "600001.SH_day_stale.csv", index=False)
            result = screen_universe(
                root,
                {"refresh_universe": True},
                fetcher=fake_spot_snapshot,
                today=date(2026, 7, 12),
            )
            sh_row = next(row for row in result["rows"] if row["symbol"] == "600001.SH")
            self.assertEqual("DATA_GAP", sh_row["technical_status"])
            self.assertEqual("STALE", sh_row["history_status"])
            self.assertEqual(30, sh_row["data_age_days"])
            self.assertEqual(0, result["summary"]["candidate_count"])

    def test_local_library_scope_ignores_cached_full_catalog(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_project(temp_dir)
            pd.DataFrame(
                [{"symbol": "600001.SH", "stock_name": "本地样本"}]
            ).to_csv(root / "watchlist.csv", index=False)
            day_dir = root / "data" / "600001.SH" / "raw" / "day"
            day_dir.mkdir(parents=True)
            history = sample_ohlcv()
            dates = pd.to_datetime(history["date"])
            history["date"] = dates + (
                pd.Timestamp("2026-07-12") - dates.max()
            )
            history.to_csv(day_dir / "600001.SH_day_local.csv", index=False)

            universe_dir = root / "data" / "universe"
            universe_dir.mkdir(parents=True)
            normalize_universe_snapshot(
                fake_spot_snapshot(),
                date(2026, 7, 12),
            ).to_csv(universe_dir / "a_share_snapshot.csv", index=False)
            (universe_dir / "a_share_snapshot.meta.json").write_text(
                '{"is_full_market": true, "rows": 5534, "snapshot_date": "2026-07-12"}',
                encoding="utf-8",
            )

            result = screen_universe(
                root,
                {"universe_scope": "local_library"},
                today=date(2026, 7, 12),
            )

        self.assertEqual("local_library", result["meta"]["universe_scope"])
        self.assertEqual("project_symbols", result["meta"]["universe_source"])
        self.assertEqual(1, result["summary"]["universe_count"])
        self.assertEqual("600001.SH", result["rows"][0]["symbol"])

    def test_short_new_listing_is_data_gap_with_per_rule_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self.make_project(temp_dir)
            day_dir = root / "data" / "688001.SH" / "raw" / "day"
            day_dir.mkdir(parents=True)
            history = sample_ohlcv().tail(8).reset_index(drop=True)
            dates = pd.to_datetime(history["date"])
            history["date"] = dates + (
                pd.Timestamp("2026-07-12") - dates.max()
            )
            history.to_csv(day_dir / "688001.SH_day_new.csv", index=False)

            result = screen_universe(
                root,
                {"universe_scope": "local_library"},
                today=date(2026, 7, 12),
            )
            row = result["rows"][0]
            rule_evidence = [
                item
                for item in row["rule_evidence"]
                if item["kind"] != "base_gate"
            ]

        self.assertEqual("DATA_GAP", row["technical_status"])
        self.assertEqual("INCOMPLETE", row["history_status"])
        self.assertEqual("2026-07-12", row["history_date"])
        self.assertEqual(1, result["summary"]["result_data_gap_count"])
        self.assertEqual(0, result["summary"]["not_selected_count"])
        self.assertGreater(row["active_rule_count"], 0)
        self.assertEqual(row["active_rule_count"], len(rule_evidence))
        self.assertTrue(all(item["status"] == "DATA_GAP" for item in rule_evidence))
        self.assertTrue(
            all(item["observed"]["available_rows"] == 8 for item in rule_evidence)
        )


if __name__ == "__main__":
    unittest.main()
