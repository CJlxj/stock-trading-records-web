from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from src.io_loader import find_latest_csv, validate_symbol
from src.rules.storage import canonical_hash, file_sha256, relative_project_path
from src.stock_library import local_stock_name_map


REQUIRED_COLUMNS = {"date", "open", "high", "low", "close", "volume"}


class DatasetManifestError(ValueError):
    pass


def _symbols(project_root: Path) -> list[str]:
    values: set[str] = set(local_stock_name_map(project_root))
    watchlist = project_root / "watchlist.csv"
    if watchlist.exists():
        try:
            frame = pd.read_csv(watchlist)
            for raw in frame.get("symbol", []):
                try:
                    values.add(validate_symbol(raw))
                except ValueError:
                    continue
        except (OSError, pd.errors.ParserError):
            pass
    data_root = project_root / "data"
    if data_root.exists():
        for path in data_root.iterdir():
            if path.is_dir() and path.name != "universe":
                try:
                    values.add(validate_symbol(path.name))
                except ValueError:
                    continue
    return sorted(values)


def local_day_paths(
    project_root: str | Path,
    symbols: Iterable[str] | None = None,
) -> dict[str, Path]:
    root = Path(project_root)
    paths: dict[str, Path] = {}
    for symbol in symbols or _symbols(root):
        try:
            normalized = validate_symbol(symbol)
            paths[normalized] = find_latest_csv(
                normalized,
                "day",
                base_dir=str(root / "data"),
            )
        except (ValueError, FileNotFoundError):
            continue
    return paths


def _metadata(path: Path) -> dict[str, Any]:
    metadata_path = path.with_suffix(".meta.json")
    if not metadata_path.exists():
        return {}
    try:
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _effective_hash(frame: pd.DataFrame) -> str:
    normalized = frame.copy()
    if "date" in normalized:
        normalized["date"] = pd.to_datetime(normalized["date"]).dt.strftime("%Y-%m-%d")
    encoded = normalized.to_csv(
        index=False,
        lineterminator="\n",
        float_format="%.12g",
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _empty_manifest(reference: date) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "current_date": str(reference),
        "as_of_trade_date": None,
        "actual_common_trade_date": None,
        "alignment_status": "EMPTY",
        "is_aligned_to_current_date": False,
        "candidate_for_trade_date": None,
        "candidate_for_label": "下一有效交易日",
        "dataset_hash": canonical_hash({"as_of_trade_date": None, "entries": []}),
        "uniform_date": True,
        "discovered_count": 0,
        "computable_count": 0,
        "data_gap_count": 0,
        "status_counts": {},
        "adjust_modes": [],
        "entries": [],
    }


def _inspect_file(path: Path, root: Path) -> dict[str, Any]:
    metadata = _metadata(path)
    try:
        frame = pd.read_csv(path)
    except (OSError, pd.errors.ParserError) as exc:
        return {
            "path": path,
            "relative_path": relative_project_path(root, path),
            "file_hash": file_sha256(path),
            "read_error": str(exc),
            "frame": None,
            "metadata": metadata,
            "last_trade_date": None,
        }
    columns = [str(column) for column in frame.columns]
    dates = (
        pd.to_datetime(frame["date"], errors="coerce")
        if "date" in frame
        else pd.Series(dtype="datetime64[ns]")
    )
    valid_dates = dates.dropna()
    return {
        "path": path,
        "relative_path": relative_project_path(root, path),
        "file_hash": file_sha256(path),
        "frame": frame,
        "dates": dates,
        "metadata": metadata,
        "columns": columns,
        "last_trade_date": (
            None if valid_dates.empty else str(valid_dates.max().date())
        ),
    }


def build_dataset_manifest(
    project_root: str | Path,
    *,
    symbols: Iterable[str] | None = None,
    as_of_trade_date: str | date | None = None,
    reference_date: str | date | None = None,
) -> dict[str, Any]:
    root = Path(project_root)
    requested_symbols = sorted(
        {validate_symbol(symbol) for symbol in (symbols or _symbols(root))}
    )
    paths = local_day_paths(root, requested_symbols)
    inspected = {
        symbol: _inspect_file(path, root) for symbol, path in paths.items()
    }
    reference = (
        pd.Timestamp(reference_date).date()
        if reference_date is not None
        else datetime.now(ZoneInfo("Asia/Shanghai")).date()
    )
    if as_of_trade_date is not None:
        parsed_as_of = pd.to_datetime(as_of_trade_date, errors="coerce")
        if pd.isna(parsed_as_of):
            raise DatasetManifestError("目标收盘日期格式不正确。")
        as_of = parsed_as_of.date()
        if as_of > reference:
            raise DatasetManifestError("目标收盘日期不能晚于当前日期。")
    else:
        completed_dates = [
            pd.Timestamp(item["last_trade_date"]).date()
            for item in inspected.values()
            if item.get("last_trade_date")
            and pd.Timestamp(item["last_trade_date"]).date() <= reference
        ]
        as_of = max(completed_dates) if completed_dates else None
    if as_of is None:
        if not requested_symbols:
            return _empty_manifest(reference)
        raise DatasetManifestError("没有发现已完成交易日的本地日线数据。")

    entries: list[dict[str, Any]] = []
    for symbol in requested_symbols:
        item = inspected.get(symbol)
        if item is None:
            entries.append(
                {
                    "symbol": symbol,
                    "relative_path": None,
                    "file_hash": None,
                    "effective_data_hash": None,
                    "last_trade_date": None,
                    "rows_total": 0,
                    "rows_used": 0,
                    "adjust": None,
                    "columns": [],
                    "missing_summary": {},
                    "anomalies": ["没有本地日线文件"],
                    "status": "MISSING",
                }
            )
            continue
        frame = item.get("frame")
        if frame is None:
            entries.append(
                {
                    "symbol": symbol,
                    "relative_path": item["relative_path"],
                    "file_hash": item["file_hash"],
                    "effective_data_hash": None,
                    "last_trade_date": None,
                    "rows_total": 0,
                    "rows_used": 0,
                    "adjust": item["metadata"].get("adjust"),
                    "columns": [],
                    "missing_summary": {},
                    "anomalies": [f"文件无法读取：{item['read_error']}"],
                    "status": "INVALID",
                }
            )
            continue
        dates = item["dates"]
        used = frame.loc[dates.notna() & (dates.dt.date <= as_of)].copy()
        used_dates = pd.to_datetime(used.get("date"), errors="coerce")
        last_used = None if used_dates.dropna().empty else str(used_dates.max().date())
        missing_columns = sorted(REQUIRED_COLUMNS - set(item["columns"]))
        future_rows = int((dates.notna() & (dates.dt.date > as_of)).sum())
        duplicate_dates = int(dates.dropna().duplicated().sum())
        missing_summary = {
            str(column): int(frame[column].isna().sum())
            for column in frame.columns
            if int(frame[column].isna().sum())
        }
        anomalies = []
        if missing_columns:
            anomalies.append("缺少字段：" + "、".join(missing_columns))
        if future_rows:
            anomalies.append(f"{future_rows} 行晚于目标收盘日，已排除")
        if duplicate_dates:
            anomalies.append(f"{duplicate_dates} 个重复日期")
        if last_used != str(as_of):
            anomalies.append(f"最后可用日期为 {last_used or '无'}，与目标收盘日不一致")
        status = (
            "INVALID"
            if missing_columns
            else "READY"
            if last_used == str(as_of)
            else "STALE_FOR_RUN"
        )
        entries.append(
            {
                "symbol": symbol,
                "relative_path": item["relative_path"],
                "file_hash": item["file_hash"],
                "effective_data_hash": _effective_hash(used) if not used.empty else None,
                "last_trade_date": item["last_trade_date"],
                "last_used_trade_date": last_used,
                "rows_total": int(len(frame)),
                "rows_used": int(len(used)),
                "adjust": item["metadata"].get("adjust") or "unknown",
                "adjust_label": item["metadata"].get("adjust_label"),
                "provider": item["metadata"].get("provider"),
                "columns": item["columns"],
                "missing_summary": missing_summary,
                "anomalies": anomalies,
                "future_rows_excluded": future_rows,
                "status": status,
            }
        )
    status_counts = Counter(entry["status"] for entry in entries)
    ready_dates = {
        entry.get("last_used_trade_date")
        for entry in entries
        if entry["status"] == "READY"
    }
    used_trade_dates = [entry.get("last_used_trade_date") for entry in entries]
    actual_common_trade_date = (
        min(used_trade_dates)
        if entries and all(used_trade_dates)
        else None
    )
    current_date = str(reference)
    all_entries_ready = (
        bool(entries) and status_counts.get("READY", 0) == len(entries)
    )
    if not entries:
        alignment_status = "EMPTY"
    elif (
        all_entries_ready
        and actual_common_trade_date == current_date
        and str(as_of) == current_date
    ):
        alignment_status = "CURRENT_DATE_ALIGNED"
    elif all_entries_ready:
        alignment_status = "ALIGNED_NOT_CURRENT"
    else:
        alignment_status = "PARTIAL"
    hash_payload = {
        "as_of_trade_date": str(as_of),
        "entries": [
            {
                key: entry.get(key)
                for key in (
                    "symbol",
                    "relative_path",
                    "file_hash",
                    "effective_data_hash",
                    "last_used_trade_date",
                    "rows_used",
                    "adjust",
                    "columns",
                    "status",
                )
            }
            for entry in entries
        ],
    }
    return {
        "schema_version": 1,
        "current_date": current_date,
        "as_of_trade_date": str(as_of),
        "actual_common_trade_date": actual_common_trade_date,
        "alignment_status": alignment_status,
        "is_aligned_to_current_date": alignment_status == "CURRENT_DATE_ALIGNED",
        "candidate_for_trade_date": None,
        "candidate_for_label": "下一有效交易日",
        "dataset_hash": canonical_hash(hash_payload),
        "uniform_date": len(ready_dates) <= 1
        and not status_counts.get("STALE_FOR_RUN", 0),
        "discovered_count": len(entries),
        "computable_count": status_counts.get("READY", 0),
        "data_gap_count": len(entries) - status_counts.get("READY", 0),
        "status_counts": dict(status_counts),
        "adjust_modes": sorted(
            {
                str(entry["adjust"])
                for entry in entries
                if entry.get("adjust") is not None
            }
        ),
        "entries": entries,
    }


def engine_build_manifest(project_root: str | Path) -> dict[str, Any]:
    root = Path(project_root)
    paths = [
        root / "src" / "indicators.py",
        root / "src" / "selection_engine.py",
        root / "src" / "signal_engine.py",
        root / "src" / "market_screening.py",
        root / "src" / "screening" / "ranking.py",
    ]
    paths.extend(sorted((root / "src" / "rules").glob("*.py")))
    files = [
        {
            "path": relative_project_path(root, path),
            "sha256": file_sha256(path),
        }
        for path in paths
        if path.is_file()
    ]
    return {
        "engine_version": "transparent-screener-v2",
        "engine_build_hash": canonical_hash(files),
        "files": files,
    }
