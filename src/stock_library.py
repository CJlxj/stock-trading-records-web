from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import Any

import pandas as pd

from src.io_loader import validate_symbol


LIBRARY_COLUMNS = [
    "symbol",
    "stock_name",
    "market",
    "added_at",
    "source",
    "catalog_snapshot_date",
]
LOCAL_LIBRARY_PATH = Path("config") / "local_stock_library.csv"
A_SHARE_SYMBOL = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")


class LocalStockLibraryError(ValueError):
    code = "LOCAL_STOCK_LIBRARY_ERROR"


def local_stock_library_path(project_root: str | Path) -> Path:
    return Path(project_root) / LOCAL_LIBRARY_PATH


def _clean_text(value: Any, *, fallback: str = "", limit: int = 100) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return (text or fallback)[:limit]


def normalize_library_symbol(value: Any) -> str:
    symbol = validate_symbol(str(value or ""))
    if not A_SHARE_SYMBOL.fullmatch(symbol):
        raise LocalStockLibraryError("自定义股票库只支持沪深北 6 位 A 股代码。")
    return symbol


def read_local_stock_library(project_root: str | Path) -> pd.DataFrame:
    path = local_stock_library_path(project_root)
    if not path.exists():
        return pd.DataFrame(columns=LIBRARY_COLUMNS)
    try:
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        raise LocalStockLibraryError(f"自定义股票库无法读取：{exc}") from exc

    rows: list[dict[str, str]] = []
    for _, raw in frame.iterrows():
        try:
            symbol = normalize_library_symbol(raw.get("symbol"))
        except (ValueError, LocalStockLibraryError):
            continue
        market = symbol.rsplit(".", 1)[-1]
        rows.append(
            {
                "symbol": symbol,
                "stock_name": _clean_text(
                    raw.get("stock_name"),
                    fallback=symbol,
                    limit=80,
                ),
                "market": market,
                "added_at": _clean_text(raw.get("added_at"), limit=40),
                "source": _clean_text(
                    raw.get("source"),
                    fallback="local_library",
                    limit=40,
                ),
                "catalog_snapshot_date": _clean_text(
                    raw.get("catalog_snapshot_date"),
                    limit=20,
                ),
            }
        )
    if not rows:
        return pd.DataFrame(columns=LIBRARY_COLUMNS)
    return (
        pd.DataFrame(rows, columns=LIBRARY_COLUMNS)
        .drop_duplicates(subset=["symbol"], keep="last")
        .sort_values("symbol")
        .reset_index(drop=True)
    )


def local_stock_name_map(project_root: str | Path) -> dict[str, str]:
    frame = read_local_stock_library(project_root)
    return {
        str(row["symbol"]): str(row["stock_name"] or row["symbol"])
        for _, row in frame.iterrows()
    }


def add_local_stock_member(
    project_root: str | Path,
    *,
    symbol: str,
    stock_name: str,
    catalog_snapshot_date: str | None,
    added_at: str | None = None,
    source: str = "web_catalog",
) -> tuple[dict[str, str], bool]:
    root = Path(project_root)
    normalized = normalize_library_symbol(symbol)
    frame = read_local_stock_library(root)
    existing = frame.loc[frame["symbol"] == normalized]
    if not existing.empty:
        return {
            key: str(existing.iloc[-1].get(key, ""))
            for key in LIBRARY_COLUMNS
        }, False

    record = {
        "symbol": normalized,
        "stock_name": _clean_text(stock_name, fallback=normalized, limit=80),
        "market": normalized.rsplit(".", 1)[-1],
        "added_at": added_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": _clean_text(source, fallback="local_library", limit=40),
        "catalog_snapshot_date": _clean_text(catalog_snapshot_date, limit=20),
    }
    updated = pd.concat(
        [frame, pd.DataFrame([record], columns=LIBRARY_COLUMNS)],
        ignore_index=True,
    )
    updated = (
        updated.drop_duplicates(subset=["symbol"], keep="first")
        .sort_values("symbol")
        .reset_index(drop=True)
    )
    path = local_stock_library_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".csv.tmp")
    try:
        updated.to_csv(temporary, index=False)
        temporary.replace(path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise LocalStockLibraryError(f"自定义股票库保存失败：{exc}") from exc
    return record, True
