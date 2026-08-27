from __future__ import annotations

from copy import deepcopy
import csv
import io
import json
from pathlib import Path
import sqlite3
from typing import Any
import uuid


SCHEMA = """
CREATE TABLE IF NOT EXISTS screening_runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    snapshot_date TEXT,
    universe_source TEXT NOT NULL,
    is_full_market INTEGER NOT NULL,
    universe_count INTEGER NOT NULL,
    pass_count INTEGER NOT NULL,
    fail_count INTEGER NOT NULL,
    data_gap_count INTEGER NOT NULL,
    technical_ready_count INTEGER NOT NULL,
    technical_candidate_count INTEGER NOT NULL,
    rule_hash TEXT NOT NULL,
    result_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_screening_runs_created_at ON screening_runs(created_at DESC);
"""


MIGRATION_COLUMNS = {
    "schema_version": "INTEGER NOT NULL DEFAULT 1",
    "as_of_trade_date": "TEXT",
    "candidate_for_trade_date": "TEXT",
    "dataset_hash": "TEXT",
    "rule_set_id": "TEXT",
    "rule_set_version": "INTEGER",
    "rule_set_hash": "TEXT",
    "engine_version": "TEXT",
    "engine_build_hash": "TEXT",
    "input_fingerprint": "TEXT",
}


class DuplicateScreeningError(ValueError):
    def __init__(self, run_id: str) -> None:
        super().__init__("相同数据、规则方案和执行代码已经生成过正式批次。")
        self.run_id = run_id
        self.code = "DUPLICATE_RUN"


class ScreeningHistoryStore:
    def __init__(self, project_root: str | Path, db_path: str | Path | None = None) -> None:
        root = Path(project_root)
        self.db_path = Path(db_path) if db_path else root / "history" / "screenings.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(screening_runs)").fetchall()
        }
        for name, declaration in MIGRATION_COLUMNS.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE screening_runs ADD COLUMN {name} {declaration}"
                )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_screening_runs_input_fingerprint
            ON screening_runs(input_fingerprint)
            """
        )
        connection.commit()
        return connection

    def save(
        self,
        result: dict[str, Any],
        *,
        confirm_duplicate: bool = False,
    ) -> dict[str, Any]:
        run_id = uuid.uuid4().hex
        stored = deepcopy(result)
        stored.setdefault("meta", {})["history_id"] = run_id
        meta = stored["meta"]
        summary = stored["summary"]
        result_json = json.dumps(stored, ensure_ascii=False, allow_nan=False)

        connection = self._connect()
        try:
            with connection:
                fingerprint = meta.get("input_fingerprint")
                if fingerprint and not confirm_duplicate:
                    existing = connection.execute(
                        """
                        SELECT id FROM screening_runs
                        WHERE input_fingerprint = ?
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (fingerprint,),
                    ).fetchone()
                    if existing is not None:
                        raise DuplicateScreeningError(str(existing["id"]))
                connection.execute(
                    """
                    INSERT INTO screening_runs (
                        id, created_at, snapshot_date, universe_source, is_full_market,
                        universe_count, pass_count, fail_count, data_gap_count,
                        technical_ready_count, technical_candidate_count, rule_hash, result_json,
                        schema_version, as_of_trade_date, candidate_for_trade_date,
                        dataset_hash, rule_set_id, rule_set_version, rule_set_hash,
                        engine_version, engine_build_hash, input_fingerprint
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        meta["created_at"],
                        meta.get("snapshot_date"),
                        meta.get("universe_source") or "unknown",
                        int(bool(meta.get("is_full_market"))),
                        summary["universe_count"],
                        summary["pass_count"],
                        summary["fail_count"],
                        summary["data_gap_count"],
                        summary["technical_ready_count"],
                        summary["technical_candidate_count"],
                        meta["rule_hash"],
                        result_json,
                        int(stored.get("schema_version", 1)),
                        meta.get("as_of_trade_date"),
                        meta.get("candidate_for_trade_date"),
                        meta.get("dataset_hash"),
                        meta.get("rule_set_id"),
                        meta.get("rule_set_version"),
                        meta.get("rule_hash"),
                        meta.get("engine_version"),
                        meta.get("engine_build_hash"),
                        meta.get("input_fingerprint"),
                    ),
                )
        finally:
            connection.close()
        return self._legacy_compatible(stored)

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT id, created_at, snapshot_date, universe_source, is_full_market,
                       universe_count, pass_count, fail_count, data_gap_count,
                       technical_ready_count, technical_candidate_count, rule_hash,
                       schema_version, as_of_trade_date, candidate_for_trade_date,
                       dataset_hash, rule_set_id, rule_set_version, rule_set_hash,
                       engine_version, engine_build_hash, input_fingerprint
                FROM screening_runs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        finally:
            connection.close()
        results = [dict(row) for row in rows]
        for result in results:
            result["is_full_market"] = bool(result["is_full_market"])
            result["legacy_snapshot"] = int(result.get("schema_version") or 1) < 2
        return results

    def find_by_fingerprint(self, fingerprint: str) -> dict[str, Any] | None:
        if not fingerprint:
            return None
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT id, created_at, as_of_trade_date, rule_set_id, rule_set_version
                FROM screening_runs
                WHERE input_fingerprint = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else dict(row)

    @staticmethod
    def _legacy_compatible(payload: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(payload)
        if int(result.get("schema_version", 1)) < 2:
            result.setdefault("schema_version", 1)
            result.setdefault("meta", {})["legacy_snapshot"] = True
            result["meta"].setdefault(
                "legacy_message", "旧批次 · 无完整数据清单和代码快照"
            )
        else:
            result.setdefault("meta", {})["legacy_snapshot"] = False
        return result

    def get(self, run_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT result_json FROM screening_runs WHERE id = ?", (run_id,)
            ).fetchone()
        finally:
            connection.close()
        return (
            None
            if row is None
            else self._legacy_compatible(json.loads(row["result_json"]))
        )

    def latest(self, *, universe_scope: str | None = None) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            if universe_scope is None:
                rows = connection.execute(
                    "SELECT result_json FROM screening_runs ORDER BY created_at DESC LIMIT 1"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT result_json
                    FROM screening_runs
                    WHERE is_full_market = 0
                    ORDER BY created_at DESC
                    LIMIT 100
                    """
                ).fetchall()
        finally:
            connection.close()
        if not rows:
            return None
        fallback = None
        for row in rows:
            result = self._legacy_compatible(json.loads(row["result_json"]))
            if universe_scope is None:
                return result
            if result.get("meta", {}).get("universe_scope") == universe_scope:
                return result
            if fallback is None and not result.get("meta", {}).get("universe_scope"):
                fallback = result
        return fallback

    def evidence(self, run_id: str, symbol: str) -> dict[str, Any] | None:
        screening = self.get(run_id)
        if screening is None:
            return None
        normalized = str(symbol or "").strip().upper()
        rows = screening.get("row_results") or screening.get("rows") or []
        row = next(
            (item for item in rows if str(item.get("symbol", "")).upper() == normalized),
            None,
        )
        if row is None:
            return None
        return {
            "run_id": run_id,
            "symbol": normalized,
            "meta": screening.get("meta", {}),
            "row": row,
            "evidence": row.get("rule_evidence") or [],
        }

    def export_csv(self, run_id: str) -> str | None:
        screening = self.get(run_id)
        if screening is None:
            return None
        output = io.StringIO()
        fieldnames = [
            "symbol",
            "stock_name",
            "candidate_status",
            "primary_candidate",
            "final_candidate",
            "rank_position",
            "ranking_score",
            "selection_status",
            "technical_status",
            "secondary_status",
            "secondary_passed_rules",
            "secondary_total_rules",
            "passed_rules",
            "total_rules",
            "required_passed",
            "veto_triggered",
            "data_date",
            "main_unmet",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in screening.get("row_results") or screening.get("rows") or []:
            technical_status = row.get("technical_status")
            secondary_status = str(row.get("secondary_status") or "INACTIVE")
            primary_candidate = (
                row.get("primary_candidate") is True
                or technical_status == "BUY_CANDIDATE"
            )
            final_candidate = bool(
                row.get(
                    "final_candidate",
                    primary_candidate and secondary_status in {"INACTIVE", "PASS"},
                )
            )
            candidate_status = {
                "WATCH": "NEAR",
                "DATA_GAP": "DATA_GAP",
            }.get(str(technical_status), "NOT_SELECTED")
            if final_candidate:
                candidate_status = "FINAL"
            elif primary_candidate and secondary_status == "FAIL":
                candidate_status = "SECONDARY_FILTERED"
            elif primary_candidate and secondary_status == "DATA_GAP":
                candidate_status = "SECONDARY_DATA_GAP"
            writer.writerow(
                {
                    "symbol": row.get("symbol"),
                    "stock_name": row.get("stock_name"),
                    "candidate_status": candidate_status,
                    "primary_candidate": primary_candidate,
                    "final_candidate": final_candidate,
                    "rank_position": row.get("rank_position"),
                    "ranking_score": row.get("ranking_score"),
                    "selection_status": row.get("selection_status"),
                    "technical_status": technical_status,
                    "secondary_status": secondary_status,
                    "secondary_passed_rules": row.get(
                        "secondary_matched_rule_count"
                    ),
                    "secondary_total_rules": row.get("secondary_rule_count"),
                    "passed_rules": row.get("matched_rule_count"),
                    "total_rules": row.get("active_rule_count"),
                    "required_passed": row.get("required_passed"),
                    "veto_triggered": row.get("veto_triggered"),
                    "data_date": row.get("history_date"),
                    "main_unmet": "；".join(row.get("failed_candidate_rules") or []),
                }
            )
        return output.getvalue()

    def audit_json(self, run_id: str) -> str | None:
        screening = self.get(run_id)
        return (
            None
            if screening is None
            else json.dumps(screening, ensure_ascii=False, indent=2, allow_nan=False)
        )

    def count(self) -> int:
        connection = self._connect()
        try:
            return int(connection.execute("SELECT COUNT(*) FROM screening_runs").fetchone()[0])
        finally:
            connection.close()
