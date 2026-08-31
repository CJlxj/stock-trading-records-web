from __future__ import annotations

from copy import deepcopy
from datetime import date
import threading
import time
from pathlib import Path
from typing import Any
import uuid

from src.market_screening import screen_universe
from src.screening_history import ScreeningHistoryStore


class ScreeningPreflightError(ValueError):
    def __init__(self, message: str, *, code: str = "PREFLIGHT_ERROR") -> None:
        super().__init__(message)
        self.code = code


class ScreeningCoordinator:
    def __init__(self, project_root: str | Path, *, ttl_seconds: int = 900) -> None:
        self.root = Path(project_root)
        self.ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._preflights: dict[str, dict[str, Any]] = {}

    def _prune(self) -> None:
        threshold = time.monotonic() - self.ttl_seconds
        for key in [
            key
            for key, value in self._preflights.items()
            if value["created_monotonic"] < threshold
        ]:
            self._preflights.pop(key, None)

    def preflight(
        self,
        payload: dict[str, Any] | None = None,
        *,
        reference_date: date | None = None,
    ) -> dict[str, Any]:
        request = deepcopy(payload or {})
        request["refresh_universe"] = False
        effective_reference_date = reference_date or date.today()
        result = screen_universe(
            self.root,
            request,
            today=effective_reference_date,
        )
        meta = result["meta"]
        manifest = result["dataset_manifest"]
        blocking_errors = []
        warnings = []
        if manifest["computable_count"] <= 0:
            blocking_errors.append("没有股票覆盖目标收盘日，不能生成正式候选批次。")
        if manifest["data_gap_count"]:
            warnings.append(
                f"{manifest['data_gap_count']} 只股票数据日期不一致或缺少日线，"
                "将明确列为数据待补。"
            )
        if not manifest["uniform_date"]:
            warnings.append("本地数据并非全部同一截止日；落后股票不会参与正常候选。")
        duplicate = ScreeningHistoryStore(self.root).find_by_fingerprint(
            meta["input_fingerprint"]
        )
        preflight_id = uuid.uuid4().hex
        with self._lock:
            self._prune()
            self._preflights[preflight_id] = {
                "created_monotonic": time.monotonic(),
                "request": request,
                "input_fingerprint": meta["input_fingerprint"],
                "reference_date": effective_reference_date,
            }
        return {
            "preflight_id": preflight_id,
            "as_of_trade_date": meta["as_of_trade_date"],
            "candidate_for_trade_date": meta.get("candidate_for_trade_date"),
            "candidate_for_label": meta["candidate_for_label"],
            "dataset_hash": meta["dataset_hash"],
            "rule_set_id": meta["rule_set_id"],
            "rule_set_version": meta["rule_set_version"],
            "rule_set_name": meta["rule_set_name"],
            "rule_set_hash": meta["rule_hash"],
            "engine_version": meta["engine_version"],
            "engine_build_hash": meta["engine_build_hash"],
            "input_fingerprint": meta["input_fingerprint"],
            "dataset": {
                "discovered_count": manifest["discovered_count"],
                "computable_count": manifest["computable_count"],
                "data_gap_count": manifest["data_gap_count"],
                "uniform_date": manifest["uniform_date"],
                "adjust_modes": manifest["adjust_modes"],
            },
            "blocking_errors": blocking_errors,
            "warnings": warnings,
            "can_run": not blocking_errors,
            "duplicate_run": {
                "exists": duplicate is not None,
                "run_id": None if duplicate is None else duplicate["id"],
                "created_at": None if duplicate is None else duplicate["created_at"],
            },
        }

    def run(
        self,
        preflight_id: str,
        *,
        confirm_duplicate: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            self._prune()
            cached = deepcopy(self._preflights.get(str(preflight_id)))
        if cached is None:
            raise ScreeningPreflightError(
                "预检已失效，请重新检查数据和规则。",
                code="PREFLIGHT_EXPIRED",
            )
        result = screen_universe(
            self.root,
            cached["request"],
            today=cached["reference_date"],
        )
        if result["meta"]["input_fingerprint"] != cached["input_fingerprint"]:
            with self._lock:
                self._preflights.pop(str(preflight_id), None)
            raise ScreeningPreflightError(
                "预检后数据、规则或执行代码已经变化，请重新预检。",
                code="PREFLIGHT_STALE",
            )
        if result["dataset_manifest"]["computable_count"] <= 0:
            with self._lock:
                self._preflights.pop(str(preflight_id), None)
            raise ScreeningPreflightError(
                "没有股票覆盖目标收盘日，不能生成正式候选批次。",
                code="PREFLIGHT_BLOCKED",
            )
        stored = ScreeningHistoryStore(self.root).save(
            result,
            confirm_duplicate=confirm_duplicate,
        )
        with self._lock:
            self._preflights.pop(str(preflight_id), None)
        return stored
