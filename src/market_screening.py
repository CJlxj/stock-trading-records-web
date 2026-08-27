from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import time
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pandas as pd

from src.indicators import add_indicators
from src.io_loader import find_latest_csv, load_ohlcv, validate_symbol
from src.rules.registry import RuleRegistry
from src.rules.storage import canonical_hash
from src.rules.version_store import RuleSetStore
from src.screening.dataset_manifest import (
    build_dataset_manifest,
    engine_build_manifest,
)
from src.screening.ranking import apply_candidate_ranking
from src.selection_engine import evaluate_selection_snapshot
from src.signal_engine import (
    active_candidate_rules,
    candidate_framework_diagnostics,
    evaluate_latest,
    load_rules,
)
from src.stock_library import local_stock_name_map


class ScreeningError(RuntimeError):
    def __init__(self, message: str, *, code: str = "SCREENING_ERROR") -> None:
        super().__init__(message)
        self.code = code


UNIVERSE_COLUMNS = [
    "symbol",
    "stock_name",
    "market",
    "latest_price",
    "latest_volume",
    "latest_amount",
    "data_date",
]
UNIVERSE_FILENAME = "a_share_snapshot.csv"
UNIVERSE_METADATA_FILENAME = "a_share_snapshot.meta.json"
STATUS_ORDER = {"PASS": 0, "DATA_GAP": 1, "FAIL": 2, "DISABLED": 3}
TECHNICAL_ORDER = {
    "BUY_CANDIDATE": 0,
    "WATCH": 1,
    "NO_TRADE": 2,
    "RISK_REDUCE": 3,
    "STOP_TRIGGER": 4,
    "DATA_GAP": 5,
    "NOT_EVALUATED": 6,
}
PRIORITY_ORDER = {"P1": 0, "P2": 1, "P3": 2, "DATA": 3, "RISK": 4, "HOLD": 5, "OUT": 6}
EASTMONEY_UNIVERSE_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_UNIVERSE_FILTER = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
EASTMONEY_UNIVERSE_FIELDS = "f12,f13,f14,f2,f5,f6,f124"
EASTMONEY_UNIVERSE_TOKEN = "bd1d9ddb04089700cf9c27f6f7426281"
MIN_FULL_MARKET_ROWS = 4000


def _now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _market_for_code(code: str) -> str:
    if code.startswith("6"):
        return "SH"
    if code.startswith(("0", "3")):
        return "SZ"
    if code.startswith(("4", "8", "9")):
        return "BJ"
    return ""


def _as_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if pd.notna(parsed) else None


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _column(frame: pd.DataFrame, *names: str) -> str | None:
    available = {str(column).strip().lower(): str(column) for column in frame.columns}
    for name in names:
        match = available.get(name.strip().lower())
        if match is not None:
            return match
    return None


def normalize_universe_snapshot(
    frame: pd.DataFrame,
    snapshot_date: date | str | None = None,
) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise ScreeningError("基础股票池返回空数据。")

    symbol_column = _column(frame, "symbol")
    code_column = _column(frame, "code", "代码", "股票代码")
    name_column = _column(frame, "stock_name", "name", "名称", "股票名称")
    price_column = _column(frame, "latest_price", "最新价", "现价", "收盘")
    volume_column = _column(frame, "latest_volume", "成交量", "volume")
    amount_column = _column(frame, "latest_amount", "成交额", "amount")
    date_column = _column(frame, "data_date", "日期", "date")

    if symbol_column is None and code_column is None:
        raise ScreeningError("基础股票池缺少股票代码字段。")
    if name_column is None:
        raise ScreeningError("基础股票池缺少股票名称字段。")

    normalized_rows: list[dict[str, Any]] = []
    default_date = str(snapshot_date or date.today())
    volume_is_lots = volume_column is not None and str(volume_column).strip() == "成交量"
    for _, raw in frame.iterrows():
        if symbol_column is not None:
            raw_symbol = str(raw.get(symbol_column, "")).strip().upper()
            code = raw_symbol.split(".", 1)[0]
            market = raw_symbol.rsplit(".", 1)[-1] if "." in raw_symbol else _market_for_code(code)
        else:
            code = str(raw.get(code_column, "")).strip().split(".", 1)[0].zfill(6)
            market = _market_for_code(code)
        if len(code) != 6 or not code.isdigit() or market not in {"SH", "SZ", "BJ"}:
            continue
        symbol = validate_symbol(f"{code}.{market}")
        stock_name = str(raw.get(name_column, "")).strip()
        if not stock_name:
            stock_name = symbol

        latest_volume = _as_float(raw.get(volume_column)) if volume_column else None
        if latest_volume is not None and volume_is_lots:
            latest_volume *= 100
        raw_date = raw.get(date_column) if date_column else default_date
        parsed_date = pd.to_datetime(raw_date, errors="coerce")
        normalized_rows.append(
            {
                "symbol": symbol,
                "stock_name": stock_name,
                "market": market,
                "latest_price": _as_float(raw.get(price_column)) if price_column else None,
                "latest_volume": latest_volume,
                "latest_amount": _as_float(raw.get(amount_column)) if amount_column else None,
                "data_date": default_date if pd.isna(parsed_date) else str(parsed_date.date()),
            }
        )

    if not normalized_rows:
        raise ScreeningError("基础股票池没有可识别的沪深京 A 股代码。")
    return (
        pd.DataFrame(normalized_rows, columns=UNIVERSE_COLUMNS)
        .drop_duplicates(subset=["symbol"], keep="last")
        .sort_values("symbol")
        .reset_index(drop=True)
    )


def normalize_eastmoney_universe_payload(
    payload: dict[str, Any],
    snapshot_date: date | str | None = None,
) -> pd.DataFrame:
    data = payload.get("data") or {}
    raw_rows = data.get("diff") or []
    if isinstance(raw_rows, dict):
        raw_rows = list(raw_rows.values())
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ScreeningError("东方财富全市场接口返回空数据。")

    fallback_date = str(snapshot_date or date.today())
    rows = []
    for item in raw_rows:
        if not isinstance(item, dict):
            continue
        code = str(item.get("f12") or "").strip().zfill(6)
        if len(code) != 6 or not code.isdigit():
            continue
        timestamp = _as_float(item.get("f124"))
        try:
            data_date = str(datetime.fromtimestamp(timestamp).date()) if timestamp else fallback_date
        except (OSError, OverflowError, ValueError):
            data_date = fallback_date
        rows.append(
            {
                "代码": code,
                "名称": str(item.get("f14") or code).strip(),
                "最新价": _as_float(item.get("f2")),
                "成交量": _as_float(item.get("f5")),
                "成交额": _as_float(item.get("f6")),
                "日期": data_date,
            }
        )
    if not rows:
        raise ScreeningError("东方财富全市场接口没有可识别的 A 股代码。")
    return pd.DataFrame(rows)


def fetch_eastmoney_universe(
    page_size: int = 100,
    timeout: int = 20,
    opener: Callable[..., Any] = urlopen,
    minimum_rows: int = MIN_FULL_MARKET_ROWS,
    request_delay: float = 0.2,
    max_retries: int = 3,
) -> pd.DataFrame:
    try:
        requested_page_size = int(page_size)
    except (TypeError, ValueError):
        raise ScreeningError("东方财富全市场快照分页数量格式不正确。") from None
    # 该接口实际每页最多返回 100 条。若仍按请求的 500 条计算页数，
    # 会在只拿到约 1200 条时提前结束。
    effective_page_size = max(1, min(requested_page_size, 100))
    frames: list[pd.DataFrame] = []
    page = 1
    declared_total: int | None = None
    seen_codes: set[str] = set()
    while declared_total is None or len(seen_codes) < declared_total:
        query = urlencode(
            {
                "pn": page,
                "pz": effective_page_size,
                "po": 1,
                "np": 2,
                "ut": EASTMONEY_UNIVERSE_TOKEN,
                "fltt": 2,
                "invt": 2,
                "fid": "f12",
                "fs": EASTMONEY_UNIVERSE_FILTER,
                "fields": EASTMONEY_UNIVERSE_FIELDS,
            }
        )
        request = Request(
            f"{EASTMONEY_UNIVERSE_URL}?{query}",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://quote.eastmoney.com/center/gridlist.html",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 Safari/537.36"
                ),
            },
        )
        if opener is urlopen and page > 1 and request_delay > 0:
            time.sleep(min(float(request_delay), 2.0))
        payload = None
        last_error: Exception | None = None
        retry_count = max(1, int(max_retries))
        for attempt in range(retry_count):
            try:
                with opener(request, timeout=timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except Exception as exc:
                last_error = exc
                if opener is urlopen and attempt + 1 < retry_count:
                    time.sleep(min(0.8 * (attempt + 1), 3.0))
        if payload is None:
            raise ScreeningError(
                f"东方财富全市场快照第 {page} 页更新失败：{last_error}"
            ) from last_error
        data = payload.get("data") or {}
        try:
            page_total = int(data.get("total") or 0)
        except (TypeError, ValueError):
            page_total = 0
        if declared_total is None:
            declared_total = page_total
        elif page_total > 0 and page_total != declared_total:
            raise ScreeningError(
                "东方财富全市场快照分页总数发生变化，本次结果不会覆盖本地快照。"
            )
        page_frame = normalize_eastmoney_universe_payload(payload)
        frames.append(page_frame)
        code_column = _column(page_frame, "代码", "code")
        if code_column is not None:
            seen_codes.update(page_frame[code_column].astype(str))
        if declared_total <= 0 or len(seen_codes) >= declared_total:
            break
        page += 1
        if page > 100:
            raise ScreeningError("东方财富全市场快照分页数量异常，已停止更新。")
    result = pd.concat(frames, ignore_index=True)
    code_column = _column(result, "代码", "code")
    unique_rows = int(result[code_column].astype(str).nunique()) if code_column else len(result)
    required_rows = max(1, int(minimum_rows), int(declared_total or 0))
    if unique_rows < required_rows:
        raise ScreeningError(
            f"东方财富仅返回 {unique_rows} 只股票，完整目录应有 {required_rows} 只；"
            "本次结果不会覆盖本地快照。"
        )
    return result


def _universe_paths(project_root: str | Path) -> tuple[Path, Path]:
    root = Path(project_root) / "data" / "universe"
    return root / UNIVERSE_FILENAME, root / UNIVERSE_METADATA_FILENAME


def update_universe_snapshot(
    project_root: str | Path,
    fetcher: Callable[[], pd.DataFrame] | None = None,
    today: date | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    provider_version = None
    provider = "AKShare"
    upstream = "stock_zh_a_spot_em"
    if fetcher is None:
        if importlib.util.find_spec("akshare") is not None:
            import akshare as ak

            provider_version = getattr(ak, "__version__", None)
            fetcher = ak.stock_zh_a_spot_em
        else:
            provider = "东方财富公开全市场接口"
            upstream = "push2.eastmoney.com/api/qt/clist/get"
            fetcher = fetch_eastmoney_universe

    try:
        raw = fetcher()
    except Exception as exc:
        raise ScreeningError(f"全 A 股基础池更新失败：{exc}") from exc

    requested_date = today or date.today()
    normalized = normalize_universe_snapshot(raw, snapshot_date=requested_date)
    available_dates = normalized["data_date"].dropna().astype(str)
    snapshot_date = available_dates.max() if not available_dates.empty else str(requested_date)
    csv_path, metadata_path = _universe_paths(project_root)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_tmp = csv_path.with_suffix(".csv.tmp")
    normalized.to_csv(csv_tmp, index=False)
    csv_tmp.replace(csv_path)

    metadata = {
        "provider": provider,
        "provider_version": provider_version,
        "upstream": upstream,
        "source": "a_share_snapshot",
        "is_full_market": True,
        "snapshot_date": str(snapshot_date),
        "rows": int(len(normalized)),
        "fetched_at": _now().isoformat(timespec="seconds"),
        "path": str(csv_path.relative_to(Path(project_root))),
    }
    metadata_tmp = metadata_path.with_suffix(".json.tmp")
    metadata_tmp.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    metadata_tmp.replace(metadata_path)
    return normalized, metadata


def _read_optional_csv(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path) if path.exists() else pd.DataFrame()
    except (OSError, ValueError, pd.errors.ParserError):
        return pd.DataFrame()


def _local_symbol_names(project_root: Path) -> dict[str, str]:
    names: dict[str, str] = local_stock_name_map(project_root)
    for path in [
        project_root / "watchlist.csv",
        project_root / "records" / "positions_snapshot.csv",
        project_root / "records" / "my_trades.csv",
    ]:
        frame = _read_optional_csv(path)
        if frame.empty or "symbol" not in frame.columns:
            continue
        for _, row in frame.iterrows():
            try:
                symbol = validate_symbol(row.get("symbol", ""))
            except ValueError:
                continue
            name = str(row.get("stock_name", "") or "").strip()
            names[symbol] = name or names.get(symbol, symbol)

    data_root = project_root / "data"
    if data_root.exists():
        for directory in data_root.iterdir():
            if not directory.is_dir() or directory.name == "universe":
                continue
            try:
                symbol = validate_symbol(directory.name)
            except ValueError:
                continue
            names.setdefault(symbol, symbol)
    return names


def _local_day_paths(project_root: Path) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for symbol in _local_symbol_names(project_root):
        try:
            paths[symbol] = find_latest_csv(symbol, "day", base_dir=str(project_root / "data"))
        except FileNotFoundError:
            continue
    return paths


def _project_universe(project_root: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    names = _local_symbol_names(project_root)
    day_paths = _local_day_paths(project_root)
    rows: list[dict[str, Any]] = []
    for symbol, stock_name in names.items():
        latest_price = None
        latest_volume = None
        latest_amount = None
        data_date = None
        path = day_paths.get(symbol)
        if path is not None:
            try:
                frame = pd.read_csv(path)
                if not frame.empty:
                    latest = frame.iloc[-1]
                    latest_price = _as_float(latest.get("close"))
                    latest_volume = _as_float(latest.get("volume"))
                    latest_amount = _as_float(latest.get("amount"))
                    parsed_date = pd.to_datetime(latest.get("date"), errors="coerce")
                    data_date = None if pd.isna(parsed_date) else str(parsed_date.date())
            except (OSError, ValueError, pd.errors.ParserError):
                pass
        rows.append(
            {
                "symbol": symbol,
                "stock_name": stock_name,
                "market": symbol.rsplit(".", 1)[-1] if "." in symbol else "",
                "latest_price": latest_price,
                "latest_volume": latest_volume,
                "latest_amount": latest_amount,
                "data_date": data_date,
            }
        )
    if not rows:
        raise ScreeningError("项目内没有可用于筛选的股票，也没有全 A 股基础池快照。")
    frame = pd.DataFrame(rows, columns=UNIVERSE_COLUMNS).sort_values("symbol").reset_index(drop=True)
    dates = [value for value in frame["data_date"].dropna().astype(str).tolist() if value]
    return frame, {
        "provider": "项目本地数据",
        "source": "project_symbols",
        "is_full_market": False,
        "snapshot_date": max(dates) if dates else None,
        "rows": int(len(frame)),
        "fetched_at": None,
        "path": None,
    }


def load_universe_snapshot(project_root: str | Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    root = Path(project_root)
    csv_path, metadata_path = _universe_paths(root)
    if csv_path.exists():
        try:
            frame = normalize_universe_snapshot(pd.read_csv(csv_path))
            metadata = (
                json.loads(metadata_path.read_text(encoding="utf-8"))
                if metadata_path.exists()
                else {}
            )
            is_full_market = bool(metadata.get("is_full_market", True)) and len(frame) >= MIN_FULL_MARKET_ROWS
            metadata.update(
                {
                    "provider": metadata.get("provider", "本地缓存"),
                    "source": "a_share_snapshot",
                    "is_full_market": is_full_market,
                    "rows": int(len(frame)),
                    "path": str(csv_path.relative_to(root)),
                }
            )
            if not metadata.get("snapshot_date"):
                dates = frame["data_date"].dropna().astype(str)
                metadata["snapshot_date"] = dates.max() if not dates.empty else None
            if not is_full_market:
                return _project_universe(root)
            return frame, metadata
        except (OSError, ValueError, json.JSONDecodeError, pd.errors.ParserError, ScreeningError):
            pass
    return _project_universe(root)


def universe_status(project_root: str | Path) -> dict[str, Any]:
    root = Path(project_root)
    csv_path, metadata_path = _universe_paths(root)
    akshare_available = importlib.util.find_spec("akshare") is not None
    provider_available = True
    cached = csv_path.exists()
    metadata: dict[str, Any] = {}
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            metadata = {}
    local_count = len(_local_symbol_names(root))
    cached_rows = int(metadata.get("rows", 0) or 0) if cached else 0
    is_full_market = cached and bool(metadata.get("is_full_market", True)) and cached_rows >= MIN_FULL_MARKET_ROWS
    if cached and is_full_market:
        message = f"全 A 股快照可用，数据日期 {metadata.get('snapshot_date') or '未知'}"
    elif cached:
        message = (
            f"现有市场快照仅 {cached_rows} 只，未达到全市场完整性门槛；"
            "请重新更新，当前结果不会标记为全市场"
        )
    elif akshare_available:
        message = "尚未生成全 A 股快照，可以通过 AKShare 更新后运行筛选"
    else:
        message = (
            f"尚无全 A 股快照；当前回退筛选项目内 {local_count} 只股票，"
            "也可以通过东方财富公开接口更新全市场快照"
        )
    return {
        "provider": "AKShare" if akshare_available else "东方财富公开全市场接口",
        "provider_available": provider_available,
        "akshare_available": akshare_available,
        "cache_available": cached,
        "is_full_market": is_full_market,
        "is_partial_market": cached and not is_full_market,
        "snapshot_date": metadata.get("snapshot_date"),
        "rows": cached_rows if cached else local_count,
        "fetched_at": metadata.get("fetched_at"),
        "path": metadata.get("path") if cached else None,
        "message": message,
    }


def _rules_hash(rules: dict[str, Any]) -> str:
    relevant = {
        "selection_rules": rules.get("selection_rules", {}),
        "data_quality": {"minimum_rows": rules.get("data_quality", {}).get("minimum_rows")},
        "signal_rules": rules.get("signal_rules", {}),
        "candidate_framework": rules.get("candidate_framework", {}),
        "thresholds": rules.get("thresholds", {}),
    }
    encoded = json.dumps(relevant, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


def _data_gap_rule_evidence(
    resolved_rule_set: dict[str, list[dict[str, Any]]],
    *,
    reason: str,
    available_rows: int,
    required_rows: int,
    data_date: str | None,
    latest_row: pd.Series | None = None,
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for section in ("required", "veto", "scored", "secondary"):
        for definition in resolved_rule_set.get(section, []):
            key = (
                str(definition.get("id")),
                int(definition.get("version", 1)),
            )
            if key in seen:
                continue
            seen.add(key)
            observed: dict[str, Any] = {}
            for input_name in definition.get("inputs", []):
                value = None
                if latest_row is not None and input_name in latest_row.index:
                    value = _as_float(latest_row.get(input_name))
                observed[str(input_name)] = value
            observed["available_rows"] = int(available_rows)
            observed["required_rows"] = int(
                max(required_rows, int(definition.get("lookback", 1) or 1))
            )
            implementation = definition.get("implementation", {})
            secondary_skipped = section == "secondary"
            evidence.append(
                {
                    "rule_id": definition.get("id"),
                    "rule_version": int(definition.get("version", 1)),
                    "name": definition.get("name") or definition.get("id"),
                    "kind": definition.get("kind") or section,
                    "screening_stage": (
                        "primary" if section == "scored" else section
                    ),
                    "rule_ref": (
                        f"{definition.get('id')}@{int(definition.get('version', 1))}"
                    ),
                    "group": definition.get("group"),
                    "status": "SKIPPED" if secondary_skipped else "DATA_GAP",
                    "boolean_result": None,
                    "observed": observed,
                    "comparisons": [],
                    "plain_explanation": (
                        f"{definition.get('name') or definition.get('id')}："
                        + (
                            "一级候选资格尚未形成，二级规则未执行。"
                            if secondary_skipped
                            else f"{reason}，本条规则未计算。"
                        )
                    ),
                    "data_date": data_date,
                    "normalized_expression": implementation.get(
                        "normalized_expression"
                    )
                    or implementation.get("expression"),
                    "params": {},
                    "missing_policy": definition.get("missing_policy"),
                    "source_path": definition.get("source_path"),
                    "source_symbol": definition.get("source_symbol"),
                    "source_hash": definition.get("source_hash"),
                    "definition_hash": definition.get("definition_hash"),
                    "error": None,
                }
            )
    return evidence


def _technical_state(
    path: Path | None,
    rules: dict[str, Any],
    minimum_rows: int,
    market_context: str,
    reference_date: date,
    as_of_trade_date: date,
    registry: RuleRegistry,
    rule_set: dict[str, Any],
    resolved_rule_set: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    configured_rule_count = len(resolved_rule_set.get("scored", []))

    def data_gap_result(
        reason: str,
        *,
        status: str = "DATA_GAP",
        freshness_status: str = "MISSING",
        available_rows: int = 0,
        latest_data_date: str | None = None,
        data_age_days: int | None = None,
        latest_row: pd.Series | None = None,
        priority_label: str = "数据待补",
        include_rule_evidence: bool = True,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "score": None,
            "max_score": None,
            "score_ratio": None,
            "matched_rule_count": 0,
            "active_rule_count": configured_rule_count,
            "passed": [],
            "failed": [],
            "freshness_status": freshness_status,
            "data_age_days": data_age_days,
            "latest_data_date": latest_data_date,
            "reason": reason,
            "groups": {},
            "group_pass_count": 0,
            "priority": {"level": "DATA", "label": priority_label, "score": 0},
            "rule_results": (
                _data_gap_rule_evidence(
                    resolved_rule_set,
                    reason=reason,
                    available_rows=available_rows,
                    required_rows=minimum_rows,
                    data_date=latest_data_date,
                    latest_row=latest_row,
                )
                if include_rule_evidence
                else []
            ),
            "required_passed": False,
            "veto_triggered": False,
            "blocking_rule_error": False,
        }

    if path is None:
        return data_gap_result(
            "没有本地日线，尚未计算技术候选",
            status="NOT_EVALUATED",
            priority_label="待补日线",
            include_rule_evidence=False,
        )
    try:
        market = load_ohlcv(path, minimum_rows=1)
        market_dates = pd.to_datetime(market["date"], errors="coerce")
        market = market.loc[
            market_dates.notna() & (market_dates.dt.date <= as_of_trade_date)
        ].copy()
        if market.empty:
            return data_gap_result(
                f"目标收盘日 {as_of_trade_date} 之前没有可用日线"
            )

        latest_date = pd.to_datetime(market["date"].max()).date()
        latest_date_text = str(latest_date)
        data_age_days = (reference_date - latest_date).days
        latest_row = market.iloc[-1]
        if len(market) < minimum_rows:
            return data_gap_result(
                f"本地日线只有 {len(market)} 行，少于 {minimum_rows} 行",
                freshness_status="INCOMPLETE",
                available_rows=len(market),
                latest_data_date=latest_date_text,
                data_age_days=data_age_days,
                latest_row=latest_row,
            )

        max_age_days = int(
            rules.get("data_quality", {}).get("max_age_days", {}).get("day", 7)
        )
        if data_age_days > max_age_days:
            return data_gap_result(
                f"日线截至 {latest_date}，距筛选日 {data_age_days} 天，"
                f"超过允许的 {max_age_days} 天",
                freshness_status="STALE",
                available_rows=len(market),
                latest_data_date=latest_date_text,
                data_age_days=data_age_days,
                latest_row=latest_row,
                priority_label="日线过期",
            )
        if latest_date != as_of_trade_date:
            return data_gap_result(
                f"日线截至 {latest_date}，与目标收盘日 "
                f"{as_of_trade_date} 不一致",
                freshness_status="STALE",
                available_rows=len(market),
                latest_data_date=latest_date_text,
                data_age_days=data_age_days,
                latest_row=latest_row,
                priority_label="日期不一致",
            )
        signal = evaluate_latest(
            add_indicators(market),
            rules=rules,
            market_context=market_context,
            registry=registry,
            rule_set=rule_set,
            resolved_rule_set=resolved_rule_set,
        )
        return {
            "status": signal.status,
            "score": int(signal.score),
            "max_score": int(signal.max_score),
            "score_ratio": signal.score_ratio,
            "matched_rule_count": signal.matched_rule_count,
            "active_rule_count": signal.active_rule_count,
            "passed": signal.passed,
            "failed": signal.failed,
            "freshness_status": "FRESH",
            "data_age_days": data_age_days,
            "latest_data_date": latest_date_text,
            "reason": (
                f"{signal.matched_rule_count}/{signal.active_rule_count} 条规则符合"
                f"（{signal.score_ratio:.0%}），{signal.group_pass_count}/4 组达到要求"
            ),
            "groups": signal.groups,
            "group_pass_count": signal.group_pass_count,
            "priority": signal.priority,
            "rule_results": signal.rule_results,
            "required_passed": signal.required_passed,
            "veto_triggered": signal.veto_triggered,
            "blocking_rule_error": signal.blocking_rule_error,
        }
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        return data_gap_result(
            f"本地日线无法计算：{exc}",
            freshness_status="INVALID",
        )


def _screen_row(
    row: pd.Series,
    rules: dict[str, Any],
    local_path: Path | None,
    market_context: str,
    reference_date: date,
    as_of_trade_date: date,
    registry: RuleRegistry,
    rule_set: dict[str, Any],
    resolved_rule_set: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    minimum_rows = int(rules.get("data_quality", {}).get("minimum_rows", 60))
    symbol = str(row.get("symbol", ""))
    stock_name = str(row.get("stock_name", symbol))
    market = str(row.get("market", ""))
    price = _as_float(row.get("latest_price"))
    volume = _as_float(row.get("latest_volume"))
    amount = _as_float(row.get("latest_amount"))
    avg_amount_20d = None
    avg_amount_available = False
    if local_path is not None:
        try:
            history = pd.read_csv(local_path)
            if "date" in history.columns:
                history_dates = pd.to_datetime(history["date"], errors="coerce")
                history = history.loc[
                    history_dates.notna()
                    & (history_dates.dt.date <= as_of_trade_date)
                ].copy()
            if "amount" in history.columns:
                amounts = pd.to_numeric(history["amount"], errors="coerce").tail(20)
                avg_amount_available = amounts.notna().sum() >= min(20, len(history))
                if avg_amount_available:
                    avg_amount_20d = float(amounts.mean())
        except (OSError, ValueError, pd.errors.ParserError):
            pass

    selection_result = evaluate_selection_snapshot(
        symbol=symbol,
        stock_name=stock_name,
        latest_close=price,
        latest_volume=volume,
        avg_amount_20d=avg_amount_20d,
        avg_amount_available=avg_amount_available,
        rules=rules,
    )

    technical_rule_set = resolved_rule_set
    if selection_result.status != "PASS" and resolved_rule_set.get("secondary"):
        technical_rule_set = {
            **resolved_rule_set,
            "secondary": [],
        }
    technical = _technical_state(
        local_path,
        rules,
        minimum_rows,
        market_context,
        reference_date,
        as_of_trade_date,
        registry,
        rule_set,
        technical_rule_set,
    )
    priority = dict(technical["priority"])
    if selection_result.status != "PASS":
        priority = {
            "level": "OUT" if selection_result.status == "FAIL" else "DATA",
            "label": "未通过准入" if selection_result.status == "FAIL" else "准入待补",
            "score": 0,
        }
    reason_items = selection_result.failed or selection_result.data_gaps or selection_result.passed
    base_gate_evidence = [
        {
            "rule_id": "system.selection_gate",
            "rule_version": 1,
            "name": "基础准入",
            "kind": "base_gate",
            "group": "system",
            "status": selection_result.status
            if selection_result.status in {"PASS", "FAIL", "DATA_GAP"}
            else "SKIPPED",
            "boolean_result": selection_result.status == "PASS",
            "observed": selection_result.metrics,
            "comparisons": [],
            "plain_explanation": "；".join(reason_items[:3])
            if reason_items
            else "基础准入没有可显示说明。",
            "data_date": str(as_of_trade_date),
            "normalized_expression": None,
            "params": {},
            "missing_policy": "DATA_GAP",
            "source_path": "src/selection_engine.py",
            "source_symbol": "evaluate_selection_snapshot",
            "source_hash": None,
            "definition_hash": None,
            "error": None,
        },
        {
            "rule_id": "system.same_as_of_date",
            "rule_version": 1,
            "name": "统一目标收盘日",
            "kind": "base_gate",
            "group": "system",
            "status": "PASS"
            if technical["latest_data_date"] == str(as_of_trade_date)
            else "DATA_GAP",
            "boolean_result": technical["latest_data_date"] == str(as_of_trade_date),
            "observed": {
                "expected": str(as_of_trade_date),
                "actual": technical["latest_data_date"],
            },
            "comparisons": [],
            "plain_explanation": (
                f"日线截止 {technical['latest_data_date'] or '无'}；"
                f"本批次目标收盘日 {as_of_trade_date}。"
            ),
            "data_date": technical["latest_data_date"],
            "normalized_expression": None,
            "params": {},
            "missing_policy": "DATA_GAP",
            "source_path": "src/market_screening.py",
            "source_symbol": "_technical_state",
            "source_hash": None,
            "definition_hash": None,
            "error": None,
        },
    ]
    rule_evidence = [*base_gate_evidence, *technical["rule_results"]]
    return {
        "symbol": symbol,
        "stock_name": stock_name,
        "market": market,
        "latest_price": None if price is None else round(price, 4),
        "latest_amount": None if amount is None else round(amount, 2),
        "data_date": row.get("data_date") if pd.notna(row.get("data_date")) else None,
        "selection_status": selection_result.status,
        "technical_status": technical["status"],
        "technical_score": technical["score"],
        "technical_max_score": technical["max_score"],
        "technical_score_ratio": technical["score_ratio"],
        "matched_rule_count": technical["matched_rule_count"],
        "active_rule_count": technical["active_rule_count"],
        "passed_candidate_rules": technical["passed"],
        "failed_candidate_rules": technical["failed"],
        "technical_groups": technical["groups"],
        "technical_group_pass_count": technical["group_pass_count"],
        "required_passed": technical["required_passed"],
        "veto_triggered": technical["veto_triggered"],
        "blocking_rule_error": technical["blocking_rule_error"],
        "rule_evidence": rule_evidence,
        "priority_level": priority["level"],
        "priority_label": priority["label"],
        "priority_score": priority["score"],
        "history_status": {
            "FRESH": "LOCAL",
            "STALE": "STALE",
            "MISSING": "MISSING",
            "INCOMPLETE": "INCOMPLETE",
            "INVALID": "INVALID",
        }.get(technical["freshness_status"], "MISSING"),
        "history_date": technical["latest_data_date"],
        "data_age_days": technical["data_age_days"],
        "reason": "；".join(reason_items[:3]) if reason_items else "没有可显示的规则说明",
        "technical_reason": technical["reason"],
        "avg_amount_20d": None if avg_amount_20d is None else round(avg_amount_20d, 2),
        "failed": selection_result.failed,
        "data_gaps": selection_result.data_gaps,
    }


def screen_universe(
    project_root: str | Path,
    payload: dict[str, Any] | None = None,
    fetcher: Callable[[], pd.DataFrame] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    root = Path(project_root)
    request = payload or {}
    universe_scope = str(request.get("universe_scope") or "auto").strip().lower()
    if universe_scope not in {"auto", "local_library", "full_market"}:
        raise ScreeningError("筛选范围不正确，只支持本地股票库或完整市场。")

    if universe_scope == "local_library":
        universe, universe_meta = _project_universe(root)
    elif _as_bool(request.get("refresh_universe", False)):
        universe, universe_meta = update_universe_snapshot(root, fetcher=fetcher, today=today)
    else:
        universe, universe_meta = load_universe_snapshot(root)
        if universe_scope == "full_market" and not universe_meta.get("is_full_market"):
            raise ScreeningError("完整市场目录尚未就绪，请先更新完整股票目录。")

    resolved_universe_scope = (
        "full_market"
        if universe_meta.get("is_full_market")
        else "local_library"
    )

    rules = load_rules(str(root / "config" / "strategy_rules.yaml"))
    registry_root = root if (root / "rules" / "catalog" / "builtin").exists() else Path(
        __file__
    ).resolve().parents[1]
    registry = RuleRegistry(registry_root)
    if (root / "rules" / "rule_sets").exists():
        rule_set_store = RuleSetStore(root)
        active_rule_set = rule_set_store.active()
        requested_rule_set = str(request.get("rule_set_id") or "").strip()
        requested_version_raw = request.get("rule_set_version")
        try:
            requested_version = (
                None
                if requested_version_raw is None or requested_version_raw == ""
                else int(requested_version_raw)
            )
        except (TypeError, ValueError):
            raise ScreeningError(
                "规则组合版本格式不正确。",
                code="RULE_SET_VERSION_INVALID",
            ) from None
        if requested_rule_set and requested_rule_set != active_rule_set["id"]:
            raise ScreeningError(
                "正式筛选只能使用当前启用的规则组合。",
                code="FORMAL_SCREEN_ACTIVE_RULE_SET_REQUIRED",
            )
        if (
            requested_version is not None
            and requested_version != int(active_rule_set["version"])
        ):
            raise ScreeningError(
                "正式筛选只能使用当前启用组合的精确版本。",
                code="FORMAL_SCREEN_ACTIVE_RULE_SET_REQUIRED",
            )
        rule_set = active_rule_set
        resolved_rule_set = rule_set_store.resolve(rule_set)
    else:
        framework = rules.get("candidate_framework", {})
        active_rules = active_candidate_rules(rules)
        rule_set = {
            "schema_version": 1,
            "id": "legacy_compatibility",
            "version": 0,
            "name": "兼容规则方案",
            "description": "隔离测试或旧项目的兼容方案。",
            "status": "ACTIVE",
            "timeframe": "day",
            "base_gates": {"mode": "all", "rules": []},
            "required": {"mode": "all", "rules": []},
            "veto": {"mode": "any", "rules": []},
            "scored": {
                "mode": "equal_score",
                "rules": [f"{rule_id}@1" for rule_id in active_rules],
                "pass_ratio": float(framework.get("pass_score_ratio", 0.65)),
                "error_policy": "block_candidate",
            },
            "secondary": {
                "mode": "minimum_match",
                "rules": [],
                "minimum_match": 0,
                "error_policy": "exclude_from_refined_candidates",
            },
            "ranking": {
                "mode": "priority_strength",
                "top_n": 5,
            },
            "near_policy": {
                "score_ratio": float(framework.get("watch_score_ratio", 0.45)),
                "require_no_veto": True,
            },
            "group_policy": {
                "minimum_groups": int(framework.get("min_group_passes", 3)),
                "group_pass_ratio": float(framework.get("group_pass_ratio", 0.5)),
            },
        }
        rule_set["rule_set_hash"] = canonical_hash(rule_set)
        resolved_rule_set = {
            "required": [],
            "veto": [],
            "scored": [registry.get(rule_id, 1) for rule_id in active_rules],
            "secondary": [],
        }
    market_context = str(request.get("market_context") or "unknown")
    if market_context not in {"unknown", "supportive", "neutral", "defensive"}:
        market_context = "unknown"
    local_paths = _local_day_paths(root)
    reference_date = today or date.today()
    universe_symbols = [str(value) for value in universe["symbol"].tolist()]
    requested_as_of = request.get("as_of_trade_date")
    try:
        dataset_manifest = build_dataset_manifest(
            root,
            symbols=universe_symbols,
            as_of_trade_date=requested_as_of,
            reference_date=reference_date,
        )
    except ValueError:
        fallback_as_of = requested_as_of or universe_meta.get("snapshot_date") or reference_date
        dataset_manifest = build_dataset_manifest(
            root,
            symbols=universe_symbols,
            as_of_trade_date=fallback_as_of,
            reference_date=reference_date,
        )
    as_of_trade_date = pd.Timestamp(
        dataset_manifest["as_of_trade_date"]
    ).date()
    rows = [
        _screen_row(
            row,
            rules,
            local_paths.get(str(row.get("symbol", ""))),
            market_context,
            reference_date,
            as_of_trade_date,
            registry,
            rule_set,
            resolved_rule_set,
        )
        for _, row in universe.iterrows()
    ]
    ranking_summary = apply_candidate_ranking(rows, rule_set, resolved_rule_set)

    def display_order(item: dict[str, Any]) -> tuple[Any, ...]:
        if item.get("final_candidate"):
            bucket = 0
        elif item.get("primary_candidate") and item.get("secondary_status") == "DATA_GAP":
            bucket = 1
        elif item.get("primary_candidate"):
            bucket = 2
        elif item.get("selection_status") == "PASS" and item.get("technical_status") == "WATCH":
            bucket = 3
        elif item.get("selection_status") == "DATA_GAP" or item.get("technical_status") in {
            "DATA_GAP",
            "NOT_EVALUATED",
        }:
            bucket = 4
        elif item.get("selection_status") == "PASS":
            bucket = 5
        else:
            bucket = 6
        return (
            bucket,
            item.get("rank_position") or item.get("primary_rank_position") or 10**9,
            -float(item.get("ranking_score") or 0),
            -float(item.get("technical_score_ratio") or 0),
            item["symbol"],
        )

    rows.sort(key=display_order)

    counts = {status: sum(row["selection_status"] == status for row in rows) for status in STATUS_ORDER}
    technical_ready_count = sum(
        row["technical_status"] not in {"NOT_EVALUATED", "DATA_GAP"} for row in rows
    )
    admitted_rows = [row for row in rows if row["selection_status"] == "PASS"]
    admitted_ready_rows = [
        row
        for row in admitted_rows
        if row["technical_status"] not in {"NOT_EVALUATED", "DATA_GAP"}
    ]
    primary_candidate_count = sum(
        row["technical_status"] == "BUY_CANDIDATE" for row in admitted_rows
    )
    candidate_count = int(ranking_summary["final_candidate_count"])
    near_miss_count = sum(row["technical_status"] == "WATCH" for row in admitted_rows)
    primary_data_gap_count = sum(
        row["selection_status"] == "DATA_GAP"
        or (
            row["selection_status"] != "FAIL"
            and row["technical_status"] in {"DATA_GAP", "NOT_EVALUATED"}
        )
        for row in rows
    )
    result_data_gap_count = (
        primary_data_gap_count
        + int(ranking_summary["secondary_data_gap_count"])
    )
    not_selected_count = max(
        0,
        len(rows) - candidate_count - near_miss_count - result_data_gap_count,
    )
    failed_rule_counts = Counter(
        rule_id
        for row in admitted_ready_rows
        for rule_id in row.get("failed_candidate_rules", [])
    )
    scored_definitions = resolved_rule_set.get("scored", [])
    scored_by_id = {item["id"]: item for item in scored_definitions}
    bottleneck_rules = []
    for rule_id, failed_count in failed_rule_counts.most_common(5):
        metadata = scored_by_id.get(rule_id, {})
        bottleneck_rules.append(
            {
                "id": rule_id,
                "label": metadata.get("name") or metadata.get("label") or rule_id,
                "failed_count": failed_count,
                "evaluated_count": len(admitted_ready_rows),
                "failure_ratio": round(
                    failed_count / len(admitted_ready_rows), 4
                )
                if admitted_ready_rows
                else 0,
            }
        )
    priority_counts = {
        level: sum(row["priority_level"] == level for row in rows)
        for level in PRIORITY_ORDER
    }
    selection = rules.get("selection_rules", {})
    framework_diagnostics = candidate_framework_diagnostics(
        rules,
        catalog_by_id={item["id"]: item for item in scored_definitions},
        active_rules_override=[item["id"] for item in scored_definitions],
        framework_override={
            "active_rules": [item["id"] for item in scored_definitions],
            "min_active_rules": 1,
            "max_active_rules": 50,
            "pass_ratio": rule_set["scored"]["pass_ratio"],
            "near_ratio": rule_set["near_policy"]["score_ratio"],
            "minimum_groups": rule_set["group_policy"]["minimum_groups"],
            "group_pass_ratio": rule_set["group_policy"]["group_pass_ratio"],
            "priority_high_ratio": max(
                float(rule_set["scored"]["pass_ratio"]),
                float(
                    rules.get("candidate_framework", {}).get(
                        "priority_high_ratio", 0.8
                    )
                ),
            ),
        },
    )
    build_manifest = engine_build_manifest(root)
    rule_set_hash = rule_set.get("rule_set_hash") or canonical_hash(
        {
            key: value
            for key, value in rule_set.items()
            if key
            not in {
                "status",
                "validated_at",
                "activated_at",
                "source_path",
                "source_hash",
                "rule_set_hash",
            }
        }
    )
    rule_snapshots = [
        {
            key: value
            for key, value in definition.items()
            if key not in {"validation"}
        }
        for section in ("required", "veto", "scored", "secondary")
        for definition in resolved_rule_set.get(section, [])
    ]
    input_fingerprint = canonical_hash(
        {
            "as_of_trade_date": dataset_manifest["as_of_trade_date"],
            "dataset_hash": dataset_manifest["dataset_hash"],
            "rule_set_hash": rule_set_hash,
            "engine_build_hash": build_manifest["engine_build_hash"],
            "market_context": market_context,
            "universe_scope": resolved_universe_scope,
        }
    )
    created_at = _now().isoformat(timespec="seconds")
    result = {
        "schema_version": 2,
        "meta": {
            "created_at": created_at,
            "snapshot_date": dataset_manifest["as_of_trade_date"],
            "as_of_trade_date": dataset_manifest["as_of_trade_date"],
            "candidate_for_trade_date": None,
            "candidate_for_label": "下一有效交易日",
            "universe_source": universe_meta.get("source"),
            "universe_provider": universe_meta.get("provider"),
            "universe_scope": resolved_universe_scope,
            "is_full_market": bool(universe_meta.get("is_full_market")),
            "rule_hash": rule_set_hash,
            "rule_set_id": rule_set["id"],
            "rule_set_version": int(rule_set["version"]),
            "rule_set_name": rule_set["name"],
            "dataset_hash": dataset_manifest["dataset_hash"],
            "engine_version": build_manifest["engine_version"],
            "engine_build_hash": build_manifest["engine_build_hash"],
            "input_fingerprint": input_fingerprint,
            "market_context": market_context,
        },
        "summary": {
            "universe_count": len(rows),
            "pass_count": counts["PASS"],
            "admission_count": counts["PASS"],
            "fail_count": counts["FAIL"],
            "data_gap_count": counts["DATA_GAP"],
            "disabled_count": counts["DISABLED"],
            "technical_ready_count": technical_ready_count,
            "technical_coverage_ratio": round(technical_ready_count / len(rows), 4) if rows else 0,
            "admission_technical_ready_count": len(admitted_ready_rows),
            "technical_candidate_count": primary_candidate_count,
            "primary_candidate_count": primary_candidate_count,
            "candidate_count": candidate_count,
            "final_candidate_count": candidate_count,
            "secondary_enabled": ranking_summary["secondary_enabled"],
            "secondary_rule_count": ranking_summary["secondary_rule_count"],
            "secondary_minimum_match": ranking_summary["secondary_minimum_match"],
            "secondary_match_mode": ranking_summary["secondary_match_mode"],
            "secondary_pass_count": ranking_summary["secondary_pass_count"],
            "secondary_filtered_count": ranking_summary["secondary_filtered_count"],
            "secondary_data_gap_count": ranking_summary["secondary_data_gap_count"],
            "top_candidate_count": ranking_summary["top_candidate_count"],
            "overflow_candidate_count": ranking_summary["overflow_candidate_count"],
            "near_miss_count": near_miss_count,
            "not_selected_count": not_selected_count,
            "result_data_gap_count": result_data_gap_count,
            "rule_excluded_count": sum(
                row["selection_status"] == "FAIL"
                or row["technical_status"] in {"NO_TRADE", "RISK_REDUCE", "STOP_TRIGGER"}
                or (
                    bool(row.get("primary_candidate"))
                    and not bool(row.get("final_candidate"))
                )
                for row in rows
            ),
            "zero_candidate_is_valid": True,
            "bottleneck_rules": bottleneck_rules,
            "priority_p1_count": priority_counts["P1"],
            "priority_p2_count": priority_counts["P2"],
            "ranking_version": ranking_summary["ranking_version"],
            "ranking_formula": ranking_summary["ranking_formula"],
        },
        "rules": {
            "allowed_markets": selection.get("allowed_markets", ["SH", "SZ", "BJ"]),
            "exclude_st": bool(selection.get("exclude_st", True)),
            "exclude_suspended": bool(selection.get("exclude_suspended", True)),
            "min_price": float(selection.get("min_price", 0) or 0),
            "max_price": float(selection.get("max_price", 0) or 0),
            "min_avg_amount_20d": float(selection.get("min_avg_amount_20d", 0) or 0),
            "active_candidate_rule_count": framework_diagnostics["active_rule_count"],
            "candidate_formula": framework_diagnostics["formula"],
            "framework_status": framework_diagnostics["status"],
            "primary_rule_priority": list(rule_set["scored"]["rules"]),
            "secondary_rules": list(rule_set.get("secondary", {}).get("rules") or []),
            "secondary_minimum_match": int(
                rule_set.get("secondary", {}).get("minimum_match") or 0
            ),
            "secondary_match_mode": (
                "ALL"
                if str(rule_set.get("editor_mode") or "") == "simple_all"
                else "LEGACY_MINIMUM_MATCH"
            ),
            "ranking_top_n": int(rule_set.get("ranking", {}).get("top_n") or 5),
        },
        "rows": rows,
        "row_results": rows,
        "dataset_manifest": dataset_manifest,
        "rule_set_snapshot": {
            "rule_set": rule_set,
            "rules": rule_snapshots,
        },
        "engine_build_manifest": build_manifest,
        "disclaimer": (
            "筛选结果只表示规则准入、一级技术候选和可选二级筛选状态；"
            "排名分只用于候选内排序，不是上涨概率。零候选是有效结果，"
            "系统不会自动放宽规则，也不构成投资建议或买卖命令。"
        ),
    }
    return result
