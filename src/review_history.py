from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from src.io_loader import validate_symbol


SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
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
    result_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed',
    review_type TEXT NOT NULL DEFAULT 'daily',
    review_group_id TEXT,
    version_no INTEGER NOT NULL DEFAULT 1,
    completed_at TEXT,
    updated_at TEXT,
    rule_version TEXT NOT NULL DEFAULT 'legacy',
    source_draft_id TEXT,
    supersedes_review_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviews_symbol_created_at ON reviews(symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reviews_review_date ON reviews(review_date DESC);
"""

ACTIVE_DRAFT_STATUSES = {"draft", "in_progress", "blocked"}
FORMAL_REVIEW_STATUSES = {"completed", "archived"}
REVIEW_STATUSES = ACTIVE_DRAFT_STATUSES | FORMAL_REVIEW_STATUSES | {"converted", "deleted"}
INTERNAL_REQUEST_KEYS = {
    "draft_version_id",
    "base_version_id",
    "review_group_id",
}


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="microseconds")


def ensure_review_history_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    columns = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in connection.execute("PRAGMA table_info(reviews)").fetchall()
    }
    additions = {
        "status": "TEXT NOT NULL DEFAULT 'completed'",
        "review_type": "TEXT NOT NULL DEFAULT 'daily'",
        "review_group_id": "TEXT",
        "version_no": "INTEGER NOT NULL DEFAULT 1",
        "completed_at": "TEXT",
        "updated_at": "TEXT",
        "rule_version": "TEXT NOT NULL DEFAULT 'legacy'",
        "source_draft_id": "TEXT",
        "supersedes_review_id": "TEXT",
    }
    for column, definition in additions.items():
        if column not in columns:
            connection.execute(f"ALTER TABLE reviews ADD COLUMN {column} {definition}")
    connection.execute(
        "UPDATE reviews SET review_group_id = id WHERE review_group_id IS NULL OR review_group_id = ''"
    )
    connection.execute(
        "UPDATE reviews SET completed_at = created_at WHERE status = 'completed' AND completed_at IS NULL"
    )
    connection.execute(
        "UPDATE reviews SET updated_at = created_at WHERE updated_at IS NULL"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_reviews_group_version "
        "ON reviews(review_group_id, version_no DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_reviews_status_updated "
        "ON reviews(status, updated_at DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_reviews_completed_at "
        "ON reviews(status, completed_at DESC, created_at DESC)"
    )
    connection.commit()


def _safe_request(payload: dict[str, Any]) -> dict[str, Any]:
    request = deepcopy(payload)
    for key in INTERNAL_REQUEST_KEYS:
        request.pop(key, None)
    csv_text = request.pop("csv_text", None)
    if csv_text:
        encoded = str(csv_text).encode("utf-8")
        request["csv_snapshot"] = {
            "name": request.get("csv_name") or "临时上传 CSV",
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
    return request


def _float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed else default


def _position_pct(payload: dict[str, Any]) -> float:
    account_asset = _float(payload.get("account_total_asset"))
    shares = _float(payload.get("position_shares"))
    current_price = _float(payload.get("current_price"))
    if account_asset > 0 and shares >= 0 and current_price >= 0:
        return shares * current_price / account_asset
    return 0.0


def _draft_progress(request: dict[str, Any]) -> dict[str, Any]:
    required: list[tuple[str, bool]] = [
        (
            "审查设置",
            all(
                request.get(key)
                for key in ["symbol", "timeframe", "review_date", "review_intent", "decision_window"]
            ),
        ),
        (
            "行情数据",
            request.get("data_source") == "local"
            or bool(request.get("csv_snapshot")),
        ),
        ("仓位信息", _float(request.get("account_total_asset")) > 0),
    ]
    intent = str(request.get("review_intent") or "daily")
    if intent in {"open", "add", "risk"}:
        plan_ready = bool(request.get("sell_condition"))
        if intent in {"open", "add"}:
            plan_ready = plan_ready and bool(request.get("buy_reason"))
        required.append(("本股买卖计划", plan_ready))
    required.append(("情绪与纪律", bool(request.get("emotion_reviewed"))))
    missing = [label for label, ready in required if not ready]
    return {
        "completed_steps": len(required) - len(missing),
        "total_steps": len(required),
        "missing_steps": missing,
    }


class ReviewHistoryStore:
    def __init__(self, project_root: str | Path, db_path: str | Path | None = None) -> None:
        self.project_root = Path(project_root)
        self.db_path = Path(db_path) if db_path else self.project_root / "history" / "reviews.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        ensure_review_history_schema(connection)
        return connection

    def _rule_version(self) -> str:
        path = self.project_root / "config" / "strategy_rules.yaml"
        if not path.exists():
            return "unavailable"
        return hashlib.sha256(path.read_bytes()).hexdigest()[:10]

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record.pop("request_json", None)
        record.pop("result_json", None)
        record["review_id"] = record["id"]
        record["version_id"] = record["id"]
        record["version_count"] = 1
        record["has_changes"] = False
        return record

    def save(self, payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        draft_version_id = str(payload.get("draft_version_id") or "").strip() or None
        base_version_id = str(payload.get("base_version_id") or "").strip() or None
        rule_version = self._rule_version()
        version_id = uuid.uuid4().hex

        connection = self._connect()
        try:
            draft = None
            if draft_version_id:
                draft = connection.execute(
                    "SELECT * FROM reviews WHERE id = ? AND status IN ('draft', 'in_progress', 'blocked')",
                    (draft_version_id,),
                ).fetchone()
                if draft is None:
                    raise ValueError("没有找到可完成的审查草稿。")
                if not base_version_id:
                    base_version_id = str(draft["supersedes_review_id"] or "").strip() or None
            if base_version_id:
                base = connection.execute(
                    "SELECT id FROM reviews WHERE id = ? AND status IN ('completed', 'archived')",
                    (base_version_id,),
                ).fetchone()
                if base is None:
                    raise ValueError("没有找到要作为来源的正式复盘记录。")

            stored_result = deepcopy(result)
            meta = stored_result.setdefault("meta", {})
            meta.update(
                {
                    "history_id": version_id,
                    "version_id": version_id,
                    "review_id": version_id,
                    "version_no": 1,
                    "status": "completed",
                    "rule_version": rule_version,
                    "created_at": now,
                    "completed_at": now,
                    "updated_at": now,
                    "source_draft_id": draft_version_id,
                    "supersedes_review_id": base_version_id,
                }
            )
            summary = stored_result["summary"]
            request_json = json.dumps(_safe_request(payload), ensure_ascii=False, allow_nan=False)
            result_json = json.dumps(stored_result, ensure_ascii=False, allow_nan=False)

            with connection:
                connection.execute(
                    """
                    INSERT INTO reviews (
                        id, created_at, review_date, symbol, stock_name, timeframe, data_date,
                        technical_status, permission, position_pct, emotion_hit_count, conclusion,
                        request_json, result_json, status, review_type, review_group_id, version_no,
                        completed_at, updated_at, rule_version, source_draft_id, supersedes_review_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        version_id,
                        now,
                        meta["review_date"],
                        meta["symbol"],
                        meta["stock_name"],
                        meta["timeframe"],
                        meta["data_date"],
                        summary["technical_status"],
                        summary["permission"],
                        summary["position_pct"],
                        summary["emotion_hit_count"],
                        summary["conclusion"],
                        request_json,
                        result_json,
                        str(payload.get("review_intent") or "daily"),
                        version_id,
                        now,
                        now,
                        rule_version,
                        draft_version_id,
                        base_version_id,
                    ),
                )
                if draft is not None:
                    connection.execute(
                        "UPDATE reviews SET status = 'converted', updated_at = ? WHERE id = ?",
                        (now, draft_version_id),
                    )
        finally:
            connection.close()
        return stored_result

    def save_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        symbol = validate_symbol(str(payload.get("symbol") or ""))
        stock_name = str(payload.get("stock_name") or symbol).strip() or symbol
        review_date = str(payload.get("review_date") or datetime.now(ZoneInfo("Asia/Shanghai")).date())
        data_date = str(payload.get("data_date") or "待生成")
        timeframe = str(payload.get("timeframe") or "day")
        review_type = str(payload.get("review_intent") or "daily")
        draft_version_id = str(payload.get("draft_version_id") or "").strip() or None
        base_version_id = str(payload.get("base_version_id") or "").strip() or None
        now = _now()
        rule_version = self._rule_version()
        request_json = json.dumps(_safe_request(payload), ensure_ascii=False, allow_nan=False)

        connection = self._connect()
        try:
            existing = None
            if draft_version_id:
                existing = connection.execute(
                    "SELECT * FROM reviews WHERE id = ? AND status IN ('draft', 'in_progress', 'blocked')",
                    (draft_version_id,),
                ).fetchone()
                if existing is None:
                    raise ValueError("没有找到可更新的审查草稿。")
                version_id = str(existing["id"])
                created_at = str(existing["created_at"])
                supersedes_review_id = str(existing["supersedes_review_id"] or "").strip() or None
            else:
                version_id = uuid.uuid4().hex
                created_at = now
                supersedes_review_id = base_version_id
                if supersedes_review_id:
                    base = connection.execute(
                        "SELECT id FROM reviews WHERE id = ? AND status IN ('completed', 'archived')",
                        (supersedes_review_id,),
                    ).fetchone()
                    if base is None:
                        raise ValueError("没有找到要作为来源的正式复盘记录。")

            values = (
                review_date,
                symbol,
                stock_name,
                timeframe,
                data_date,
                "PENDING",
                "待完成",
                _position_pct(payload),
                0,
                "尚未完成审查",
                request_json,
                "{}",
                review_type,
                version_id,
                now,
                rule_version,
                supersedes_review_id,
            )
            with connection:
                if existing is not None:
                    connection.execute(
                        """
                        UPDATE reviews SET
                            review_date = ?, symbol = ?, stock_name = ?, timeframe = ?, data_date = ?,
                            technical_status = ?, permission = ?, position_pct = ?, emotion_hit_count = ?,
                            conclusion = ?, request_json = ?, result_json = ?, status = 'draft',
                            review_type = ?, review_group_id = ?, version_no = 1, updated_at = ?,
                            rule_version = ?, supersedes_review_id = ?
                        WHERE id = ?
                        """,
                        (*values, version_id),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO reviews (
                            id, created_at, review_date, symbol, stock_name, timeframe, data_date,
                            technical_status, permission, position_pct, emotion_hit_count, conclusion,
                            request_json, result_json, status, review_type, review_group_id, version_no,
                            completed_at, updated_at, rule_version, source_draft_id, supersedes_review_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, 1, NULL, ?, ?, NULL, ?)
                        """,
                        (version_id, created_at, *values),
                    )
        finally:
            connection.close()
        detail = self.get(version_id)
        if detail is None:
            raise RuntimeError("审查草稿保存失败。")
        return detail

    def list(
        self,
        symbol: str | None = None,
        limit: int = 30,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        safe_offset = max(0, int(offset))
        query = """
            SELECT id, created_at, review_date, symbol, stock_name, timeframe, data_date,
                   technical_status, permission, position_pct, emotion_hit_count, conclusion,
                   status, review_type, review_group_id, version_no, completed_at,
                   updated_at, rule_version, source_draft_id, supersedes_review_id
            FROM reviews
            WHERE status IN ('completed', 'archived')
        """
        parameters: list[Any] = []
        if symbol:
            query += " AND symbol = ?"
            parameters.append(str(symbol).upper())
        query += " ORDER BY COALESCE(completed_at, created_at) DESC, created_at DESC, id DESC LIMIT ? OFFSET ?"
        parameters.extend([safe_limit, safe_offset])

        connection = self._connect()
        try:
            rows = connection.execute(query, parameters).fetchall()
        finally:
            connection.close()
        return [self._record_from_row(row) for row in rows]

    def list_groups(self, limit: int = 100) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), 100))
        query = """
            WITH formal_reviews AS (
                SELECT id, created_at, review_date, symbol, stock_name, timeframe, data_date,
                       technical_status, permission, conclusion, status, completed_at,
                       COALESCE(completed_at, created_at) AS completed_sort
                FROM reviews
                WHERE status IN ('completed', 'archived')
            ), grouped AS (
                SELECT symbol, COUNT(*) AS record_count, MAX(completed_sort) AS latest_completed_at
                FROM formal_reviews
                GROUP BY symbol
            )
            SELECT formal_reviews.symbol, formal_reviews.stock_name, formal_reviews.timeframe,
                   formal_reviews.data_date, formal_reviews.review_date,
                   formal_reviews.technical_status, formal_reviews.permission,
                   formal_reviews.conclusion, formal_reviews.status,
                   formal_reviews.completed_sort AS latest_completed_at,
                   grouped.record_count
            FROM formal_reviews
            JOIN grouped ON grouped.symbol = formal_reviews.symbol
            WHERE formal_reviews.id = (
                SELECT latest.id
                FROM formal_reviews AS latest
                WHERE latest.symbol = formal_reviews.symbol
                ORDER BY latest.completed_sort DESC, latest.created_at DESC, latest.id DESC
                LIMIT 1
            )
            ORDER BY formal_reviews.completed_sort DESC, formal_reviews.created_at DESC, formal_reviews.id DESC
            LIMIT ?
        """
        connection = self._connect()
        try:
            rows = connection.execute(query, [safe_limit]).fetchall()
            total = connection.execute(
                "SELECT COUNT(*) FROM reviews WHERE status IN ('completed', 'archived')"
            ).fetchone()[0]
        finally:
            connection.close()

        groups = [
            {
                "symbol": row["symbol"],
                "stock_name": row["stock_name"],
                "timeframe": row["timeframe"],
                "record_count": int(row["record_count"]),
                "latest_completed_at": row["latest_completed_at"],
                "latest_review_date": row["review_date"],
                "latest_data_date": row["data_date"],
                "latest_technical_status": row["technical_status"],
                "latest_permission": row["permission"],
                "latest_conclusion": row["conclusion"],
                "latest_status": row["status"],
            }
            for row in rows
        ]
        return {"groups": groups, "total": int(total)}

    def counts(self) -> dict[str, int]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM reviews GROUP BY status"
            ).fetchall()
        finally:
            connection.close()
        by_status = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "formal": sum(by_status.get(status, 0) for status in FORMAL_REVIEW_STATUSES),
            "drafts": sum(by_status.get(status, 0) for status in ACTIVE_DRAFT_STATUSES),
            "archived": by_status.get("archived", 0),
        }

    def list_drafts(self, symbol: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        query = """
            SELECT * FROM reviews
            WHERE status IN ('draft', 'in_progress', 'blocked')
        """
        parameters: list[Any] = []
        if symbol:
            query += " AND symbol = ?"
            parameters.append(str(symbol).upper())
        query += " ORDER BY COALESCE(updated_at, created_at) DESC, created_at DESC, id DESC LIMIT ?"
        parameters.append(safe_limit)

        connection = self._connect()
        try:
            rows = connection.execute(query, parameters).fetchall()
        finally:
            connection.close()

        records = []
        for row in rows:
            record = self._record_from_row(row)
            try:
                request = json.loads(row["request_json"] or "{}")
            except json.JSONDecodeError:
                request = {}
            record.update(_draft_progress(request))
            records.append(record)
        return records

    def delete_draft(self, draft_id: str) -> dict[str, Any]:
        now = _now()
        connection = self._connect()
        try:
            with connection:
                row = connection.execute(
                    "SELECT id, symbol FROM reviews WHERE id = ? AND status IN ('draft', 'in_progress', 'blocked')",
                    (draft_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("没有找到可删除的草稿。")
                connection.execute(
                    "UPDATE reviews SET status = 'deleted', updated_at = ? WHERE id = ?",
                    (now, draft_id),
                )
        finally:
            connection.close()
        return {"draft_id": draft_id, "symbol": row["symbol"], "status": "deleted"}

    def get(self, review_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM reviews WHERE id = ?", (review_id,)
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    SELECT * FROM reviews WHERE review_group_id = ?
                    ORDER BY version_no DESC, COALESCE(updated_at, created_at) DESC LIMIT 1
                    """,
                    (review_id,),
                ).fetchone()
            if row is None:
                return None
        finally:
            connection.close()

        result = json.loads(row["result_json"] or "{}")
        return {
            "record": self._record_from_row(row),
            "versions": [],
            "request": json.loads(row["request_json"]),
            "result": result if result else None,
        }
