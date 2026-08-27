from __future__ import annotations

import base64
from datetime import date, datetime, timedelta
import hashlib
import importlib.metadata
import importlib.util
from io import StringIO
import json
import math
from pathlib import Path
import re
from typing import Any, Callable
import unicodedata
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from src.io_loader import (
    find_latest_csv,
    normalize_ohlcv,
    validate_symbol,
    validate_timeframe,
)
from src.stock_library import add_local_stock_member


class MarketDataError(RuntimeError):
    pass


A_SHARE_SYMBOL = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")
PERIOD_MAP = {"day": "daily", "week": "weekly", "month": "monthly"}
MINIMUM_HISTORY_YEARS = {"day": 0.5, "week": 2, "month": 6}
ADJUST_LABELS = {"": "不复权", "qfq": "前复权", "hfq": "后复权"}
AKSHARE_COLUMN_MAP = {
    "日期": "date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
    "成交额": "amount",
}
EASTMONEY_HISTORY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TENCENT_HISTORY_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
EASTMONEY_PERIOD_MAP = {"daily": "101", "weekly": "102", "monthly": "103"}
EASTMONEY_ADJUST_MAP = {"": "0", "qfq": "1", "hfq": "2"}
EASTMONEY_MARKET_MAP = {"SH": "1", "SZ": "0", "BJ": "0"}
EASTMONEY_COLUMNS = [
    "日期",
    "开盘",
    "收盘",
    "最高",
    "最低",
    "成交量",
    "成交额",
    "振幅",
    "涨跌幅",
    "涨跌额",
    "换手率",
]
MANUAL_IMPORT_MAX_BYTES = 8 * 1024 * 1024
MANUAL_IMPORT_MAX_ROWS = 100_000
MANUAL_COLUMN_ALIASES = {
    "date": {"date", "trade_date", "datetime", "日期", "交易日期", "交易日", "时间"},
    "open": {"open", "open_price", "开盘", "开盘价"},
    "high": {"high", "high_price", "最高", "最高价"},
    "low": {"low", "low_price", "最低", "最低价"},
    "close": {"close", "close_price", "收盘", "收盘价"},
    "volume": {
        "volume",
        "vol",
        "成交量",
        "成交量(股)",
        "成交量(手)",
        "成交量（股）",
        "成交量（手）",
    },
    "amount": {"amount", "turnover", "成交额", "成交金额"},
    "symbol": {"symbol", "code", "代码", "股票代码", "证券代码"},
}


def provider_status() -> dict[str, Any]:
    akshare_available = importlib.util.find_spec("akshare") is not None
    version = None
    if akshare_available:
        try:
            version = importlib.metadata.version("akshare")
        except importlib.metadata.PackageNotFoundError:
            pass
    return {
        "provider": "AKShare" if akshare_available else "东方财富公开日线接口",
        "available": True,
        "version": version,
        "akshare_available": akshare_available,
        "fallback_provider": "东方财富公开日线接口；断连时回退腾讯证券公开日线接口",
        "supported_timeframes": sorted(PERIOD_MAP),
        "adjustments": [{"value": key, "label": label} for key, label in ADJUST_LABELS.items()],
        "install_file": "requirements-market-data.txt",
        "message": (
            "可直接更新沪深京 A 股行情"
            if akshare_available
            else "AKShare 未安装；优先通过东方财富公开接口更新，断连时回退腾讯证券公开日线接口。"
        ),
    }


def parse_a_share_symbol(symbol: str) -> tuple[str, str]:
    normalized = validate_symbol(symbol)
    match = A_SHARE_SYMBOL.fullmatch(normalized)
    if not match:
        raise MarketDataError("自动更新目前只支持 6 位代码加 .SH、.SZ 或 .BJ 的 A 股代码。")
    return match.group(1), match.group(2)


def normalize_akshare_frame(
    frame: pd.DataFrame,
    *,
    minimum_rows: int = 30,
) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise MarketDataError("行情提供方返回空数据。")

    normalized = frame.rename(columns=AKSHARE_COLUMN_MAP).copy()
    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required - set(normalized.columns)
    if missing:
        raise MarketDataError(f"行情提供方缺少字段：{sorted(missing)}")

    selected = ["date", "open", "high", "low", "close", "volume"]
    if "amount" in normalized.columns:
        selected.append("amount")
    normalized = normalized[selected]

    for column in ["open", "high", "low", "close", "volume", "amount"]:
        if column in normalized.columns:
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")

    # Both supported providers return volume in lots; the project stores shares.
    normalized["volume"] = normalized["volume"] * 100
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    normalized = normalized.dropna(subset=["date", "open", "high", "low", "close", "volume"])
    normalized = normalized.drop_duplicates(subset=["date"], keep="last").sort_values("date").reset_index(drop=True)
    return normalize_ohlcv(normalized, minimum_rows=minimum_rows)


def _normalized_header(value: Any) -> str:
    return (
        unicodedata.normalize("NFKC", str(value or ""))
        .replace("\ufeff", "")
        .strip()
        .lower()
    )


def _manual_column_mapping(columns: list[Any]) -> tuple[dict[str, str], list[str]]:
    aliases = {
        _normalized_header(alias): canonical
        for canonical, values in MANUAL_COLUMN_ALIASES.items()
        for alias in values
    }
    mapping: dict[str, str] = {}
    ignored: list[str] = []
    for raw_column in columns:
        source = str(raw_column)
        canonical = aliases.get(_normalized_header(source))
        if canonical is None:
            ignored.append(source)
            continue
        if canonical in mapping:
            raise MarketDataError(
                f"文件中有多个字段都对应 {canonical}："
                f"{mapping[canonical]}、{source}。请只保留一个。"
            )
        mapping[canonical] = source
    missing = sorted({"date", "open", "high", "low", "close", "volume"} - set(mapping))
    if missing:
        raise MarketDataError(f"手动导入缺少必要字段：{', '.join(missing)}。")
    return mapping, ignored


def _decode_manual_csv(content: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return content.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise MarketDataError("CSV 编码无法识别，请使用 UTF-8 或 GB18030。")


def _manual_row_numbers(mask: pd.Series) -> str:
    return "、".join(str(int(index) + 2) for index in mask.index[mask][:5])


def _parse_manual_dates(values: pd.Series) -> pd.Series:
    text = values.fillna("").astype(str).map(
        lambda value: unicodedata.normalize("NFKC", value).strip()
    )
    text = text.str.replace(r"\.0$", "", regex=True)
    valid_shape = text.str.fullmatch(
        r"(?:\d{8}|\d{4}[-/]\d{1,2}[-/]\d{1,2})"
    )
    normalized = text.where(~text.str.fullmatch(r"\d{8}"), text.str.replace(
        r"^(\d{4})(\d{2})(\d{2})$",
        r"\1-\2-\3",
        regex=True,
    )).str.replace("/", "-", regex=False)
    parsed = pd.to_datetime(normalized, format="%Y-%m-%d", errors="coerce")
    invalid = ~valid_shape | parsed.isna()
    if invalid.any():
        raise MarketDataError(
            "日期格式不正确，CSV 行号："
            f"{_manual_row_numbers(invalid)}。请使用 YYYY-MM-DD、YYYY/MM/DD 或 YYYYMMDD。"
        )
    return parsed


def _parse_manual_number(values: pd.Series, field: str) -> pd.Series:
    text = values.fillna("").astype(str).map(
        lambda value: unicodedata.normalize("NFKC", value)
        .replace(",", "")
        .replace("，", "")
        .strip()
    )
    parsed = pd.to_numeric(text, errors="coerce")
    invalid = parsed.isna() | ~parsed.map(math.isfinite)
    if invalid.any():
        raise MarketDataError(
            f"{field} 存在无法识别的数值，CSV 行号：{_manual_row_numbers(invalid)}。"
        )
    return parsed.astype(float)


def _manual_stock_identity(
    project_root: Path,
    symbol: str,
    requested_name: Any,
) -> tuple[str, str]:
    stock_name = str(requested_name or "").replace("\r", " ").replace("\n", " ").strip()
    catalog_date = ""
    catalog_path = project_root / "data" / "universe" / "a_share_snapshot.csv"
    if catalog_path.exists():
        try:
            catalog = pd.read_csv(catalog_path, dtype=str, keep_default_na=False)
            if "symbol" in catalog:
                matched = catalog.loc[catalog["symbol"] == symbol]
                if not matched.empty:
                    stock_name = str(
                        matched.iloc[-1].get("stock_name") or stock_name
                    ).strip()
                    catalog_date = str(
                        matched.iloc[-1].get("data_date") or ""
                    ).strip()
        except (OSError, ValueError, pd.errors.ParserError):
            pass
    return (stock_name or symbol)[:80], catalog_date[:20]


def import_market_data(
    project_root: str | Path,
    payload: dict[str, Any],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Validate and persist a manually supplied daily CSV in the project format."""
    root = Path(project_root)
    requested_symbol = str(payload.get("symbol") or "").strip().upper()
    if requested_symbol.isdigit() and len(requested_symbol) == 6:
        exchange = (
            "SH"
            if requested_symbol.startswith("6")
            else "SZ"
            if requested_symbol.startswith(("0", "3"))
            else "BJ"
        )
        requested_symbol = f"{requested_symbol}.{exchange}"
    normalized_symbol = validate_symbol(requested_symbol)
    code, exchange = parse_a_share_symbol(normalized_symbol)
    filename = Path(str(payload.get("filename") or "market_data.csv")).name
    if not filename.lower().endswith(".csv"):
        raise MarketDataError("手动导入只接受 CSV 文件。")
    filename = filename.replace("\r", " ").replace("\n", " ")[:160]

    adjust = str(payload.get("adjust") or "qfq").strip().lower()
    if adjust == "none":
        adjust = ""
    if adjust not in ADJUST_LABELS:
        raise MarketDataError("复权方式不正确。")
    input_volume_unit = str(payload.get("volume_unit") or "shares").strip().lower()
    if input_volume_unit not in {"shares", "lots"}:
        raise MarketDataError("成交量单位只支持“股”或“手”。")

    encoded = str(payload.get("content_base64") or "")
    if not encoded:
        raise MarketDataError("请选择需要导入的 CSV 文件。")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise MarketDataError("CSV 文件内容无法解码。") from exc
    if not content:
        raise MarketDataError("CSV 文件为空。")
    if len(content) > MANUAL_IMPORT_MAX_BYTES:
        raise MarketDataError("CSV 文件超过 8 MB，请缩小文件后重试。")

    text, encoding = _decode_manual_csv(content)
    try:
        raw = pd.read_csv(
            StringIO(text),
            dtype=str,
            keep_default_na=False,
            sep=None,
            engine="python",
        )
    except (ValueError, pd.errors.ParserError) as exc:
        raise MarketDataError(f"CSV 结构无法读取：{exc}") from exc
    if raw.empty:
        raise MarketDataError("CSV 中没有行情记录。")
    if len(raw) > MANUAL_IMPORT_MAX_ROWS:
        raise MarketDataError("CSV 行数超过 100000 行，请缩小文件后重试。")

    mapping, ignored_columns = _manual_column_mapping(list(raw.columns))
    volume_header = _normalized_header(mapping["volume"])
    if "(手)" in volume_header:
        input_volume_unit = "lots"
    elif "(股)" in volume_header:
        input_volume_unit = "shares"
    normalized = pd.DataFrame(index=raw.index)
    normalized["date"] = _parse_manual_dates(raw[mapping["date"]])
    future = normalized["date"].dt.date > (today or date.today())
    if future.any():
        raise MarketDataError(
            f"CSV 包含未来日期，CSV 行号：{_manual_row_numbers(future)}。"
        )
    for field in ("open", "high", "low", "close", "volume"):
        normalized[field] = _parse_manual_number(raw[mapping[field]], field)
    if "amount" in mapping:
        normalized["amount"] = _parse_manual_number(raw[mapping["amount"]], "amount")

    if "symbol" in mapping:
        mismatched = raw[mapping["symbol"]].fillna("").astype(str).map(
            lambda value: value.strip().upper()
        ).map(
            lambda value: (
                f"{value.zfill(6)}.{exchange}"
                if value.isdigit()
                else value
            )
        ) != normalized_symbol
        if mismatched.any():
            raise MarketDataError(
                "CSV 中的股票代码与导入代码不一致，CSV 行号："
                f"{_manual_row_numbers(mismatched)}。"
            )

    price_columns = ["open", "high", "low", "close"]
    non_positive_price = (normalized[price_columns] <= 0).any(axis=1)
    if non_positive_price.any():
        raise MarketDataError(
            "开高低收必须大于 0，CSV 行号："
            f"{_manual_row_numbers(non_positive_price)}。"
        )
    negative_volume = normalized["volume"] < 0
    if negative_volume.any():
        raise MarketDataError(
            f"成交量不能为负数，CSV 行号：{_manual_row_numbers(negative_volume)}。"
        )
    if input_volume_unit == "lots":
        normalized["volume"] *= 100
    fractional_volume = (normalized["volume"] % 1).abs() > 1e-9
    if fractional_volume.any():
        raise MarketDataError(
            "标准化后的成交量必须是整数股，CSV 行号："
            f"{_manual_row_numbers(fractional_volume)}。"
        )
    normalized["volume"] = normalized["volume"].round().astype("int64")
    if "amount" in normalized:
        negative_amount = normalized["amount"] < 0
        if negative_amount.any():
            raise MarketDataError(
                f"成交额不能为负数，CSV 行号：{_manual_row_numbers(negative_amount)}。"
            )

    inconsistent = (
        normalized["high"] < normalized[price_columns].max(axis=1)
    ) | (
        normalized["low"] > normalized[price_columns].min(axis=1)
    )
    if inconsistent.any():
        raise MarketDataError(
            "最高价或最低价与开盘、收盘不一致，CSV 行号："
            f"{_manual_row_numbers(inconsistent)}。"
        )

    duplicate_mask = normalized.duplicated(subset=["date"], keep=False)
    if duplicate_mask.any():
        for trade_date, group in normalized.loc[duplicate_mask].groupby("date"):
            if any(group[column].nunique(dropna=False) > 1 for column in normalized.columns if column != "date"):
                raise MarketDataError(
                    f"{trade_date.date()} 存在内容不一致的重复记录，请先修正后导入。"
                )
    rows_received = int(len(normalized))
    normalized = (
        normalized.drop_duplicates(subset=["date"], keep="last")
        .sort_values("date")
        .reset_index(drop=True)
    )
    exact_duplicates_removed = rows_received - int(len(normalized))

    columns = ["date", "open", "high", "low", "close", "volume"]
    if "amount" in normalized:
        columns.append("amount")
    normalized = normalized[columns]
    latest_date = normalized["date"].iloc[-1].date()
    normalized_csv = normalized.to_csv(
        index=False,
        date_format="%Y-%m-%d",
        lineterminator="\n",
        float_format="%.12g",
    )
    normalized_hash = hashlib.sha256(normalized_csv.encode("utf-8")).hexdigest()

    target_dir = root / "data" / normalized_symbol / "raw" / "day"
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        active_path = find_latest_csv(
            normalized_symbol,
            "day",
            base_dir=str(root / "data"),
        )
        active_dates = pd.to_datetime(
            pd.read_csv(active_path, usecols=["date"])["date"],
            errors="coerce",
        ).dropna()
        active_latest = None if active_dates.empty else active_dates.max().date()
    except (FileNotFoundError, OSError, ValueError, pd.errors.ParserError):
        active_latest = None
    if active_latest is not None and latest_date < active_latest:
        raise MarketDataError(
            f"导入文件最新日期为 {latest_date}，早于当前本地数据 {active_latest}；"
            "为避免导入后仍使用旧文件，本次未写入。"
        )
    target_name = (
        f"{normalized_symbol}_day_{latest_date:%Y%m%d}_manual_"
        f"{normalized_hash[:12]}.csv"
    )
    target_path = target_dir / target_name
    metadata_path = target_path.with_suffix(".meta.json")
    imported_at = datetime.now().astimezone().isoformat(timespec="seconds")
    stock_name, catalog_snapshot_date = _manual_stock_identity(
        root,
        normalized_symbol,
        payload.get("stock_name"),
    )
    metadata = {
        "schema_version": 1,
        "provider": "手动 CSV 导入",
        "source_type": "manual_upload",
        "source_filename": filename,
        "source_file_sha256": hashlib.sha256(content).hexdigest(),
        "normalized_file_sha256": normalized_hash,
        "normalizer_version": "manual-ohlcv-v1",
        "request_id": str(payload.get("request_id") or "")[:100],
        "symbol": normalized_symbol,
        "code": code,
        "exchange": exchange,
        "timeframe": "day",
        "adjust": adjust,
        "adjust_label": ADJUST_LABELS[adjust],
        "input_volume_unit": input_volume_unit,
        "volume_unit": "shares",
        "rows_received": rows_received,
        "rows": int(len(normalized)),
        "exact_duplicates_removed": exact_duplicates_removed,
        "start_date": str(normalized["date"].iloc[0].date()),
        "latest_date": str(latest_date),
        "imported_at": imported_at,
        "fetched_at": imported_at,
        "column_mapping": {
            canonical: source for canonical, source in mapping.items()
        },
        "ignored_columns": ignored_columns,
    }
    deduplicated = target_path.exists() and metadata_path.exists()
    if not deduplicated:
        csv_temporary = target_path.with_suffix(".csv.tmp")
        metadata_temporary = metadata_path.with_suffix(".json.tmp")
        try:
            csv_temporary.write_text(normalized_csv, encoding="utf-8")
            metadata_temporary.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            metadata_temporary.replace(metadata_path)
            csv_temporary.replace(target_path)
        except OSError as exc:
            csv_temporary.unlink(missing_ok=True)
            metadata_temporary.unlink(missing_ok=True)
            if not target_path.exists():
                metadata_path.unlink(missing_ok=True)
            raise MarketDataError(f"手动行情保存失败：{exc}") from exc

    member, added = add_local_stock_member(
        root,
        symbol=normalized_symbol,
        stock_name=stock_name,
        catalog_snapshot_date=catalog_snapshot_date,
        source="manual_import",
    )
    return {
        **metadata,
        "status": "IMPORTED",
        "deduplicated": deduplicated,
        "membership_status": "ADDED" if added else "EXISTING",
        "stock_name": member["stock_name"],
        "latest_price": round(float(normalized["close"].iloc[-1]), 4),
        "path": str(target_path.relative_to(root)),
        "metadata_path": str(metadata_path.relative_to(root)),
        "normalization": {
            "rows_received": rows_received,
            "rows_written": int(len(normalized)),
            "exact_duplicates_removed": exact_duplicates_removed,
            "columns": columns,
        },
    }


def fetch_eastmoney_history(
    symbol: str,
    exchange: str,
    period: str,
    start_date: str,
    end_date: str,
    adjust: str,
    timeout: int = 20,
) -> pd.DataFrame:
    """Fetch A-share bars from Eastmoney in the same column shape as AKShare."""
    market = EASTMONEY_MARKET_MAP.get(exchange)
    klt = EASTMONEY_PERIOD_MAP.get(period)
    fqt = EASTMONEY_ADJUST_MAP.get(adjust)
    if market is None or klt is None or fqt is None:
        raise MarketDataError("东方财富行情接口不支持当前代码、周期或复权方式。")

    params = urlencode(
        {
            "secid": f"{market}.{symbol}",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": klt,
            "fqt": fqt,
            "beg": start_date,
            "end": end_date,
        }
    )
    request = Request(
        f"{EASTMONEY_HISTORY_URL}?{params}",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": f"https://quote.eastmoney.com/{exchange.lower()}{symbol}.html",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Safari/537.36",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    rows = (payload.get("data") or {}).get("klines") or []
    if not isinstance(rows, list) or not rows:
        raise MarketDataError("东方财富行情接口返回空数据。")
    parsed_rows = [str(row).split(",") for row in rows]
    if any(len(row) != len(EASTMONEY_COLUMNS) for row in parsed_rows):
        raise MarketDataError("东方财富行情接口的字段格式发生变化。")
    return pd.DataFrame(parsed_rows, columns=EASTMONEY_COLUMNS)


def fetch_tencent_history(
    symbol: str,
    exchange: str,
    period: str,
    start_date: str,
    end_date: str,
    adjust: str,
    timeout: int = 20,
) -> pd.DataFrame:
    """Fetch A-share daily bars from Tencent when Eastmoney is unavailable."""
    if period != "daily":
        raise MarketDataError("腾讯证券备用接口目前只用于日线更新。")
    adjust_key = {"": "day", "qfq": "qfqday", "hfq": "hfqday"}.get(adjust)
    if adjust_key is None:
        raise MarketDataError("腾讯证券备用接口不支持当前复权方式。")

    market_symbol = f"{exchange.lower()}{symbol}"
    start = datetime.strptime(start_date, "%Y%m%d").strftime("%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y%m%d").strftime("%Y-%m-%d")
    params = urlencode({"param": f"{market_symbol},day,{start},{end},640,{adjust}"})
    request = Request(
        f"{TENCENT_HISTORY_URL}?{params}",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": f"https://gu.qq.com/{market_symbol}/gp",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Safari/537.36",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    stock_data = (payload.get("data") or {}).get(market_symbol) or {}
    rows = stock_data.get(adjust_key) or []
    # 新上市股票通常尚未生成 qfqday / hfqday，但会返回 day。
    # 尚无除权事件时原始日线与复权序列等价，可直接用于首次导入。
    if not rows and adjust and stock_data.get("day"):
        rows = stock_data.get("day") or []
    if not isinstance(rows, list) or not rows:
        raise MarketDataError("腾讯证券备用接口返回空数据。")

    parsed_rows = [row[:6] for row in rows if isinstance(row, list) and len(row) >= 6]
    frame = pd.DataFrame(parsed_rows, columns=["日期", "开盘", "收盘", "最高", "最低", "成交量"])

    quote = stock_data.get("qt", {}).get(market_symbol) or []
    if len(quote) > 36:
        quote_date = str(quote[30])[:8]
        latest_history_date = str(frame.iloc[-1]["日期"]).replace("-", "")
        if quote_date.isdigit() and quote_date > latest_history_date:
            frame.loc[len(frame)] = [
                datetime.strptime(quote_date, "%Y%m%d").strftime("%Y-%m-%d"),
                quote[5],
                quote[3],
                quote[33],
                quote[34],
                quote[36],
            ]
    return frame


def update_market_data(
    project_root: str | Path,
    symbol: str,
    timeframe: str = "day",
    adjust: str = "qfq",
    history_years: float = 3,
    fetcher: Callable[..., pd.DataFrame] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    root = Path(project_root)
    normalized_symbol = validate_symbol(symbol)
    code, exchange = parse_a_share_symbol(normalized_symbol)
    normalized_timeframe = validate_timeframe(timeframe)
    if normalized_timeframe not in PERIOD_MAP:
        raise MarketDataError("自动更新目前支持日线、周线和月线。")
    if adjust not in ADJUST_LABELS:
        raise MarketDataError("复权方式不正确。")
    try:
        years = float(history_years)
    except (TypeError, ValueError):
        raise MarketDataError("历史区间必须是数字年数。") from None
    if not math.isfinite(years) or not 0.5 <= years <= 10:
        raise MarketDataError("历史区间必须位于半年到 10 年之间。")
    minimum_years = MINIMUM_HISTORY_YEARS[normalized_timeframe]
    if years < minimum_years:
        raise MarketDataError(f"{normalized_timeframe} 周期至少需要 {minimum_years} 年数据才能完成规则审查。")

    end_date = today or date.today()
    start_date = end_date - timedelta(days=round(366 * years))

    provider = "AKShare"
    provider_version = None
    upstream = "stock_zh_a_hist"
    if fetcher is None:
        if importlib.util.find_spec("akshare") is not None:
            import akshare as ak

            provider_version = getattr(ak, "__version__", None)
            fetcher = ak.stock_zh_a_hist
        else:
            provider = "东方财富公开日线接口"
            upstream = "push2his.eastmoney.com/api/qt/stock/kline/get"
            fallback_state: dict[str, str] = {}

            def fetcher(**kwargs: Any) -> pd.DataFrame:
                try:
                    return fetch_eastmoney_history(exchange=exchange, **kwargs)
                except Exception as eastmoney_error:
                    fallback_state["provider"] = "腾讯证券公开日线接口"
                    fallback_state["upstream"] = "web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                    fallback_state["fallback_reason"] = str(eastmoney_error)
                    return fetch_tencent_history(exchange=exchange, **kwargs)

    try:
        frame = fetcher(
            symbol=code,
            period=PERIOD_MAP[normalized_timeframe],
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
            adjust=adjust,
            timeout=20,
        )
    except Exception as exc:
        raise MarketDataError(f"行情更新失败：{exc}") from exc

    try:
        # 行情保存与规则计算分离。新上市股票只要返回至少一行有效
        # OHLCV 就正常落盘，历史长度由后续具体规则自行判断。
        normalized = normalize_akshare_frame(frame, minimum_rows=1)
    except (TypeError, ValueError) as exc:
        raise MarketDataError(f"行情数据无法保存：{exc}") from exc
    if "fallback_state" in locals() and fallback_state:
        provider = fallback_state["provider"]
        upstream = fallback_state["upstream"]

    latest_date = normalized["date"].iloc[-1].date()
    target_dir = root / "data" / normalized_symbol / "raw" / normalized_timeframe
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{normalized_symbol}_{normalized_timeframe}_{latest_date:%Y%m%d}.csv"
    temporary_path = target_path.with_suffix(".csv.tmp")
    normalized.to_csv(temporary_path, index=False, date_format="%Y-%m-%d")
    temporary_path.replace(target_path)

    fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
    metadata = {
        "provider": provider,
        "provider_version": provider_version,
        "upstream": upstream,
        "symbol": normalized_symbol,
        "code": code,
        "exchange": exchange,
        "timeframe": normalized_timeframe,
        "adjust": adjust,
        "adjust_label": ADJUST_LABELS[adjust],
        "history_years": int(years) if years.is_integer() else years,
        "volume_unit": "shares",
        "rows": int(len(normalized)),
        "start_date": str(normalized["date"].iloc[0].date()),
        "latest_date": str(latest_date),
        "fetched_at": fetched_at,
    }
    if "fallback_state" in locals() and fallback_state.get("fallback_reason"):
        metadata["fallback_reason"] = fallback_state["fallback_reason"]
    metadata_path = target_path.with_suffix(".meta.json")
    metadata_tmp = metadata_path.with_suffix(".json.tmp")
    metadata_tmp.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    metadata_tmp.replace(metadata_path)

    return {
        **metadata,
        "latest_price": round(float(normalized["close"].iloc[-1]), 4),
        "path": str(target_path.relative_to(root)),
        "metadata_path": str(metadata_path.relative_to(root)),
    }
