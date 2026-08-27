from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from src.io_loader import validate_symbol
from src.review_history import ensure_review_history_schema


CASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_cases (
    symbol TEXT PRIMARY KEY,
    stock_name TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL,
    source TEXT NOT NULL,
    candidate_context_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_cases_status_updated
ON review_cases(lifecycle_status, updated_at DESC);
"""

LIFECYCLE_STATUSES = {"WATCHING", "CLOSED"}
CASE_SOURCES = {"manual", "candidate", "project", "review"}


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int:
    parsed = _float(value)
    return int(parsed) if parsed is not None else 0


def _request(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class ReviewCaseStore:
    def __init__(self, project_root: str | Path, db_path: str | Path | None = None) -> None:
        root = Path(project_root)
        self.db_path = Path(db_path) if db_path else root / "history" / "reviews.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        ensure_review_history_schema(connection)
        connection.executescript(CASE_SCHEMA)
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(review_cases)").fetchall()
        }
        if "candidate_context_json" not in columns:
            connection.execute("ALTER TABLE review_cases ADD COLUMN candidate_context_json TEXT")
            connection.commit()
        return connection

    def upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        symbol = validate_symbol(str(payload.get("symbol") or ""))
        stock_name = str(payload.get("stock_name") or symbol).strip()
        if not stock_name:
            stock_name = symbol
        if len(stock_name) > 100:
            raise ValueError("股票名称不能超过 100 个字符。")

        lifecycle = str(payload.get("lifecycle_status") or "WATCHING").upper()
        if lifecycle not in LIFECYCLE_STATUSES:
            raise ValueError("档案状态只能是观察中或已结束；持仓中由持仓股数自动判断。")
        source = str(payload.get("source") or "manual").lower()
        if source not in CASE_SOURCES:
            raise ValueError("档案来源不正确。")
        candidate_context = payload.get("candidate_context")
        if candidate_context is not None and not isinstance(candidate_context, dict):
            raise ValueError("候选来源信息格式不正确。")
        candidate_context_json = (
            json.dumps(candidate_context, ensure_ascii=False, allow_nan=False)
            if candidate_context
            else None
        )

        now = _now()
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO review_cases (
                        symbol, stock_name, lifecycle_status, source, candidate_context_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        stock_name = excluded.stock_name,
                        lifecycle_status = excluded.lifecycle_status,
                        source = excluded.source,
                        candidate_context_json = COALESCE(
                            excluded.candidate_context_json,
                            review_cases.candidate_context_json
                        ),
                        updated_at = excluded.updated_at
                    """,
                    (
                        symbol,
                        stock_name,
                        lifecycle,
                        source,
                        candidate_context_json,
                        now,
                        now,
                    ),
                )
        finally:
            connection.close()
        return {
            "symbol": symbol,
            "stock_name": stock_name,
            "lifecycle_status": lifecycle,
            "source": source,
            "candidate_context": candidate_context or None,
            "updated_at": now,
        }

    def ensure_from_review(self, payload: dict[str, Any]) -> None:
        symbol = validate_symbol(str(payload.get("symbol") or ""))
        stock_name = str(payload.get("stock_name") or symbol).strip() or symbol
        screening_context = payload.get("screening_context")
        screening_context_json = (
            json.dumps(screening_context, ensure_ascii=False, allow_nan=False)
            if isinstance(screening_context, dict) and screening_context
            else None
        )
        now = _now()
        connection = self._connect()
        try:
            with connection:
                existing = connection.execute(
                    "SELECT symbol FROM review_cases WHERE symbol = ?", (symbol,)
                ).fetchone()
                if existing:
                    connection.execute(
                        """
                        UPDATE review_cases
                        SET stock_name = ?,
                            candidate_context_json = COALESCE(?, candidate_context_json),
                            updated_at = ?
                        WHERE symbol = ?
                        """,
                        (stock_name, screening_context_json, now, symbol),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO review_cases (
                            symbol, stock_name, lifecycle_status, source, candidate_context_json,
                            created_at, updated_at
                        ) VALUES (?, ?, 'WATCHING', 'review', ?, ?, ?)
                        """,
                        (symbol, stock_name, screening_context_json, now, now),
                    )
        finally:
            connection.close()

    def list(self, project_symbols: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            stored_rows = connection.execute(
                "SELECT * FROM review_cases ORDER BY updated_at DESC"
            ).fetchall()
            review_rows = connection.execute(
                """
                SELECT id, created_at, updated_at, review_date, symbol, stock_name, timeframe, data_date,
                       technical_status, permission, position_pct, emotion_hit_count,
                       request_json, review_group_id, version_no
                FROM reviews
                WHERE status IN ('completed', 'archived')
                ORDER BY review_date DESC, created_at DESC, id DESC
                """
            ).fetchall()
            draft_rows = connection.execute(
                """
                SELECT id, created_at, updated_at, review_date, symbol, stock_name, timeframe,
                       request_json, review_group_id, version_no, status
                FROM reviews
                WHERE status IN ('draft', 'in_progress', 'blocked')
                ORDER BY COALESCE(updated_at, created_at) DESC, version_no DESC, id DESC
                """
            ).fetchall()
            stat_rows = connection.execute(
                """
                SELECT symbol,
                       SUM(CASE WHEN status IN ('completed', 'archived') THEN 1 ELSE 0 END)
                           AS review_count,
                       SUM(CASE WHEN status IN ('completed', 'archived') THEN 1 ELSE 0 END)
                           AS completed_review_count,
                       SUM(CASE WHEN status IN ('draft', 'in_progress', 'blocked') THEN 1 ELSE 0 END)
                           AS draft_count,
                       SUM(CASE
                           WHEN status IN ('completed', 'archived')
                                AND (emotion_hit_count > 0 OR permission = '不允许临时操作')
                           THEN 1 ELSE 0 END) AS discipline_block_count,
                       SUM(CASE
                           WHEN status IN ('completed', 'archived')
                                AND emotion_hit_count = 0 AND permission <> '不允许临时操作'
                           THEN 1 ELSE 0 END) AS discipline_clear_count
                FROM reviews
                GROUP BY symbol
                """
            ).fetchall()
        finally:
            connection.close()

        stored = {row["symbol"]: dict(row) for row in stored_rows}
        latest: dict[str, dict[str, Any]] = {}
        for row in review_rows:
            latest.setdefault(row["symbol"], dict(row))
        drafts: dict[str, dict[str, Any]] = {}
        for row in draft_rows:
            drafts.setdefault(row["symbol"], dict(row))
        stats = {row["symbol"]: dict(row) for row in stat_rows}
        projects = {item["symbol"]: item for item in project_symbols or []}

        records: list[dict[str, Any]] = []
        for symbol in sorted(set(stored) | set(projects) | set(latest) | set(drafts)):
            project = projects.get(symbol, {})
            saved = stored.get(symbol, {})
            recent = latest.get(symbol, {})
            draft = drafts.get(symbol, {})
            request = _request(recent.get("request_json"))
            draft_request = _request(draft.get("request_json"))
            working_request = request or draft_request
            defaults = project.get("defaults") or {}

            def latest_or_default(key: str, default_key: str | None = None) -> Any:
                value = working_request.get(key)
                if value is not None and value != "":
                    return value
                return defaults.get(default_key or key)

            shares = _int(latest_or_default("position_shares"))
            account_asset = _float(latest_or_default("account_total_asset"))
            current_price = _float(latest_or_default("current_price"))
            avg_cost = _float(latest_or_default("avg_cost"))
            target_price = _float(latest_or_default("take_profit_price"))
            stop_price = _float(latest_or_default("stop_loss_price"))
            planned_position = _float(latest_or_default("planned_position_pct"))
            position_pct = _float(recent.get("position_pct"))
            if position_pct is None and account_asset and current_price is not None:
                position_pct = shares * current_price / account_asset

            lifecycle = str(saved.get("lifecycle_status") or "WATCHING")
            if shares > 0:
                effective_status = "HOLDING"
                status_reason = "最新记录仍有持仓"
            elif lifecycle == "CLOSED" or (
                project.get("has_trades") and not project.get("in_watchlist") and not saved
            ):
                effective_status = "CLOSED"
                status_reason = "没有持仓且档案已结束"
            else:
                effective_status = "WATCHING"
                status_reason = "没有持仓，继续观察"

            stat = stats.get(symbol, {})
            stock_name = (
                str(saved.get("stock_name") or "").strip()
                or str(recent.get("stock_name") or "").strip()
                or str(draft.get("stock_name") or "").strip()
                or str(project.get("stock_name") or symbol)
            )
            plan_ready = bool(
                working_request.get("buy_reason")
                and working_request.get("sell_condition")
                and stop_price
                and target_price
            )
            try:
                candidate_context = json.loads(saved.get("candidate_context_json") or "null")
            except json.JSONDecodeError:
                candidate_context = None
            if not isinstance(candidate_context, dict):
                candidate_context = None
            records.append(
                {
                    "symbol": symbol,
                    "stock_name": stock_name,
                    "status": effective_status,
                    "status_reason": status_reason,
                    "lifecycle_status": lifecycle,
                    "source": saved.get("source") or ("project" if project else "review"),
                    "candidate_context": candidate_context,
                    "position_shares": shares,
                    "position_pct": position_pct,
                    "avg_cost": avg_cost,
                    "current_price": current_price,
                    "target_price": target_price,
                    "stop_loss_price": stop_price,
                    "planned_position_pct": planned_position,
                    "plan_ready": plan_ready,
                    "review_count": int(stat.get("review_count") or 0),
                    "completed_review_count": int(stat.get("completed_review_count") or 0),
                    "draft_count": int(stat.get("draft_count") or 0),
                    "discipline_clear_count": int(stat.get("discipline_clear_count") or 0),
                    "discipline_block_count": int(stat.get("discipline_block_count") or 0),
                    "latest_review_id": recent.get("id"),
                    "latest_draft_id": draft.get("id"),
                    "last_review_date": recent.get("review_date"),
                    "latest_created_at": recent.get("created_at"),
                    "latest_draft_updated_at": draft.get("updated_at") or draft.get("created_at"),
                    "latest_permission": recent.get("permission"),
                    "latest_technical_status": recent.get("technical_status"),
                    "timeframe": recent.get("timeframe") or draft.get("timeframe") or "day",
                    "data_date": recent.get("data_date")
                    or (project.get("latest_day") or {}).get("date"),
                    "has_local_data": bool(project.get("has_local_data")),
                    "latest_request": request,
                    "latest_draft_request": draft_request,
                    "updated_at": max(
                        str(saved.get("updated_at") or ""),
                        str(recent.get("updated_at") or recent.get("created_at") or ""),
                        str(draft.get("updated_at") or draft.get("created_at") or ""),
                    ),
                }
            )

        status_order = {"HOLDING": 0, "WATCHING": 1, "CLOSED": 2}
        records.sort(key=lambda item: item["symbol"])
        records.sort(
            key=lambda item: str(item.get("updated_at") or item.get("latest_created_at") or ""),
            reverse=True,
        )
        records.sort(key=lambda item: status_order.get(item["status"], 9))
        return records
