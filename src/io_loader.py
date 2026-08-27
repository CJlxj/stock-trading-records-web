from pathlib import Path
import re

import pandas as pd

REQUIRED_COLUMNS = {"date", "open", "high", "low", "close", "volume"}

SAFE_SYMBOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
SAFE_TIMEFRAME_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


def validate_symbol(symbol: str) -> str:
    normalized = str(symbol).strip().upper()
    if not SAFE_SYMBOL.fullmatch(normalized):
        raise ValueError("股票代码格式不正确。")
    return normalized


def validate_timeframe(timeframe: str) -> str:
    normalized = str(timeframe).strip().replace("\\", "/")
    parts = normalized.split("/")
    if not normalized or any(part in {"", ".", ".."} or not SAFE_TIMEFRAME_PART.fullmatch(part) for part in parts):
        raise ValueError("行情周期格式不正确。")
    return "/".join(parts)


def _latest_market_date(path: Path) -> pd.Timestamp:
    try:
        header = pd.read_csv(path, nrows=0)
        date_column = next((column for column in header.columns if str(column).strip().lower() == "date"), None)
        if date_column is None:
            return pd.Timestamp.min
        dates = pd.read_csv(path, usecols=[date_column])[date_column]
        parsed = pd.to_datetime(dates, errors="coerce").dropna()
        return parsed.max() if not parsed.empty else pd.Timestamp.min
    except (OSError, ValueError, pd.errors.ParserError):
        return pd.Timestamp.min

def find_latest_csv(symbol: str, timeframe: str, base_dir: str = "data") -> Path:
    safe_symbol = validate_symbol(symbol)
    safe_timeframe = validate_timeframe(timeframe)
    path = Path(base_dir) / safe_symbol / "raw" / safe_timeframe
    if not path.exists():
        raise FileNotFoundError(f"Data folder not found: {path}")
    files = sorted(
        path.glob("*.csv"),
        key=lambda item: (_latest_market_date(item), item.stat().st_mtime),
        reverse=True,
    )
    if not files:
        raise FileNotFoundError(f"No CSV files found in: {path}")
    return files[0]

def normalize_ohlcv(
    df: pd.DataFrame,
    *,
    minimum_rows: int = 30,
) -> pd.DataFrame:
    df = df.copy()
    df.columns = [c.strip().lower() for c in df.columns]

    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")

    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    numeric_cols = ["open", "high", "low", "close", "volume"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=numeric_cols)

    if len(df) < max(1, int(minimum_rows)):
        if int(minimum_rows) <= 1:
            raise ValueError("行情中没有可保存的有效数据。")
        raise ValueError(
            f"数据少于 {int(minimum_rows)} 行，技术指标可靠性较低。"
            "请至少导入 60 行以上日线数据。"
        )

    return df


def load_ohlcv(
    csv_path: str | Path,
    *,
    minimum_rows: int = 30,
) -> pd.DataFrame:
    path = Path(csv_path)
    return normalize_ohlcv(pd.read_csv(path), minimum_rows=minimum_rows)
