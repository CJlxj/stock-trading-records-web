from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from src.io_loader import validate_symbol
from src.stock_library import local_stock_name_map


SYMBOL_SOURCE_PATHS = (
    Path("watchlist.csv"),
    Path("records") / "positions_snapshot.csv",
    Path("records") / "my_trades.csv",
)


def _read_optional_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _web_symbol_names(project_root: Path) -> dict[str, str]:
    symbol_names = dict(local_stock_name_map(project_root))
    for relative_path in SYMBOL_SOURCE_PATHS:
        frame = _read_optional_csv(project_root / relative_path)
        if frame.empty or "symbol" not in frame.columns:
            continue
        for _, row in frame.iterrows():
            try:
                symbol = validate_symbol(str(row.get("symbol", "")))
            except ValueError:
                continue
            stock_name = row.get("stock_name", "")
            symbol_names[symbol] = (
                symbol
                if pd.isna(stock_name) or not str(stock_name).strip()
                else str(stock_name).strip()
            )

    data_root = project_root / "data"
    if data_root.exists():
        for directory in data_root.iterdir():
            if not directory.is_dir() or directory.name == "universe":
                continue
            try:
                symbol = validate_symbol(directory.name)
            except ValueError:
                continue
            symbol_names.setdefault(symbol, symbol)
    return symbol_names


def build_web_bootstrap(
    project_root: str | Path,
    *,
    csrf_token: str,
) -> dict[str, Any]:
    root = Path(project_root)
    symbol_names = _web_symbol_names(root)
    return {
        "today": str(date.today()),
        "symbols": [
            {"symbol": symbol, "stock_name": symbol_names[symbol]}
            for symbol in sorted(symbol_names)
        ],
        "csrf_token": csrf_token,
    }
