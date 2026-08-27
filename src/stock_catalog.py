from __future__ import annotations

from datetime import date, datetime
import hashlib
from http.cookiejar import CookieJar
from io import BytesIO
import json
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
    urlopen,
)
import warnings

import pandas as pd

from src.io_loader import find_latest_csv, validate_symbol
from src.market_data import MarketDataError, parse_a_share_symbol, update_market_data
from src.market_screening import (
    MIN_FULL_MARKET_ROWS,
    UNIVERSE_COLUMNS,
    UNIVERSE_FILENAME,
    UNIVERSE_METADATA_FILENAME,
    fetch_eastmoney_universe,
    normalize_universe_snapshot,
)
from src.screening.dataset_manifest import (
    DatasetManifestError,
    build_dataset_manifest,
)
from src.stock_library import (
    LocalStockLibraryError,
    add_local_stock_member,
    local_stock_name_map,
)


REQUIRED_MARKETS = {"SH", "SZ", "BJ"}
CATALOG_QUERY_LIMIT = 60
CATALOG_RESULT_LIMIT = 50
OFFICIAL_CATALOG_PROVIDER = "沪深北交易所公开股票列表"
OFFICIAL_CATALOG_UPSTREAM = "sse.com.cn；szse.cn；bse.cn"


class StockCatalogError(ValueError):
    code = "STOCK_CATALOG_ERROR"


class StockCatalogNotReadyError(StockCatalogError):
    code = "STOCK_CATALOG_NOT_READY"


class _PreservePostRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> Request | None:
        if code in {307, 308}:
            return Request(
                new_url,
                data=request.data,
                headers=dict(request.headers),
                origin_req_host=request.origin_req_host,
                unverifiable=True,
                method=request.get_method(),
            )
        return super().redirect_request(
            request,
            file_pointer,
            code,
            message,
            headers,
            new_url,
        )


def _official_code(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value or "").strip()
    if not text or text.lower() == "nan":
        return ""
    if text.endswith(".0"):
        text = text[:-2]
    return text.zfill(6)


def _read_json_request(request: Request, *, timeout: int) -> dict[str, Any]:
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise StockCatalogError("交易所股票列表返回格式不正确。")
    return payload


def _fetch_sse_stock_catalog(*, timeout: int) -> pd.DataFrame:
    url = "https://query.sse.com.cn/sseQuery/commonQuery.do"
    headers = {
        "Referer": "https://www.sse.com.cn/assortment/stock/list/share/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Safari/537.36"
        ),
    }
    rows: list[dict[str, str]] = []
    for stock_type in ("1", "8"):
        params = {
            "STOCK_TYPE": stock_type,
            "REG_PROVINCE": "",
            "CSRC_CODE": "",
            "STOCK_CODE": "",
            "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
            "COMPANY_STATUS": "2,4,5,7,8",
            "type": "inParams",
            "isPagination": "true",
            "pageHelp.cacheSize": "1",
            "pageHelp.beginPage": "1",
            "pageHelp.pageSize": "10000",
            "pageHelp.pageNo": "1",
            "pageHelp.endPage": "1",
        }
        request = Request(
            f"{url}?{urlencode(params)}",
            headers=headers,
        )
        payload = _read_json_request(request, timeout=timeout)
        result = payload.get("result")
        if not isinstance(result, list):
            raise StockCatalogError("上海证券交易所股票列表返回格式不正确。")
        for item in result:
            if not isinstance(item, dict):
                continue
            code = _official_code(item.get("A_STOCK_CODE"))
            name = str(item.get("SEC_NAME_CN") or "").strip()
            if code and name:
                rows.append({"代码": code, "名称": name})
    frame = pd.DataFrame(rows)
    if len(frame) < 1_500:
        raise StockCatalogError(
            f"上海证券交易所仅返回 {len(frame)} 只 A 股，未达到完整性门槛。"
        )
    return frame


def _fetch_szse_stock_catalog(*, timeout: int) -> pd.DataFrame:
    params = {
        "SHOWTYPE": "xlsx",
        "CATALOGID": "1110",
        "TABKEY": "tab1",
        "random": "0.6935816432433362",
    }
    request = Request(
        f"https://www.szse.cn/api/report/ShowReport?{urlencode(params)}",
        headers={
            "Referer": "https://www.szse.cn/market/product/stock/list/index.html",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Safari/537.36"
            ),
        },
    )
    with urlopen(request, timeout=timeout) as response:
        body = response.read()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            raw = pd.read_excel(BytesIO(body))
    except Exception as exc:
        raise StockCatalogError(f"深圳证券交易所股票列表无法读取：{exc}") from exc
    if not {"A股代码", "A股简称"}.issubset(raw.columns):
        raise StockCatalogError("深圳证券交易所股票列表缺少代码或简称字段。")
    frame = pd.DataFrame(
        {
            "代码": raw["A股代码"].map(_official_code),
            "名称": raw["A股简称"].fillna("").astype(str).str.strip(),
        }
    )
    frame = frame.loc[
        frame["代码"].str.fullmatch(r"\d{6}")
        & frame["名称"].ne("")
    ].reset_index(drop=True)
    if len(frame) < 2_000:
        raise StockCatalogError(
            f"深圳证券交易所仅返回 {len(frame)} 只 A 股，未达到完整性门槛。"
        )
    return frame


def _parse_bse_payload(text: str) -> dict[str, Any]:
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < start:
        raise StockCatalogError("北京证券交易所股票列表返回格式不正确。")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise StockCatalogError("北京证券交易所股票列表返回格式不正确。")
    return payload[0]


def _fetch_bse_stock_catalog(*, timeout: int) -> pd.DataFrame:
    url = "https://www.bse.cn/nqxxController/nqxxCnzq.do"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://www.bse.cn/nq/listedcompany.html",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Safari/537.36"
        ),
    }
    rows: list[dict[str, str]] = []
    total_pages: int | None = None
    page = 0
    opener = build_opener(
        HTTPCookieProcessor(CookieJar()),
        _PreservePostRedirectHandler(),
    )
    while total_pages is None or page < total_pages:
        form = urlencode(
            {
                "page": str(page),
                "typejb": "T",
                "xxfcbj[]": "2",
                "xxzqdm": "",
                "sortfield": "xxzqdm",
                "sorttype": "asc",
            }
        ).encode("utf-8")
        request = Request(url, data=form, headers=headers, method="POST")
        with opener.open(request, timeout=timeout) as response:
            text = response.read().decode("utf-8")
        payload = _parse_bse_payload(text)
        try:
            current_total_pages = int(payload.get("totalPages") or 0)
        except (TypeError, ValueError):
            current_total_pages = 0
        if current_total_pages <= 0 or current_total_pages > 100:
            raise StockCatalogError("北京证券交易所股票列表分页数量异常。")
        if total_pages is None:
            total_pages = current_total_pages
        elif current_total_pages != total_pages:
            raise StockCatalogError("北京证券交易所股票列表分页总数发生变化。")
        content = payload.get("content")
        if not isinstance(content, list):
            raise StockCatalogError("北京证券交易所股票列表缺少股票内容。")
        for item in content:
            if not isinstance(item, dict):
                continue
            code = _official_code(item.get("xxzqdm"))
            name = str(item.get("xxzqjc") or "").strip()
            if code and name:
                rows.append({"代码": code, "名称": name})
        page += 1
    frame = pd.DataFrame(rows)
    if len(frame) < 100:
        raise StockCatalogError(
            f"北京证券交易所仅返回 {len(frame)} 只 A 股，未达到完整性门槛。"
        )
    return frame


def fetch_official_exchange_catalog(*, timeout: int = 20) -> pd.DataFrame:
    try:
        frames = [
            _fetch_sse_stock_catalog(timeout=timeout),
            _fetch_szse_stock_catalog(timeout=timeout),
            _fetch_bse_stock_catalog(timeout=timeout),
        ]
    except StockCatalogError:
        raise
    except Exception as exc:
        raise StockCatalogError(f"交易所公开股票列表同步失败：{exc}") from exc
    return (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates(subset=["代码"], keep="last")
        .sort_values("代码")
        .reset_index(drop=True)
    )


def stock_catalog_paths(project_root: str | Path) -> tuple[Path, Path]:
    root = Path(project_root) / "data" / "universe"
    return root / UNIVERSE_FILENAME, root / UNIVERSE_METADATA_FILENAME


def _read_metadata(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalized_catalog(
    frame: pd.DataFrame,
    *,
    snapshot_date: date | str | None = None,
    minimum_rows: int = MIN_FULL_MARKET_ROWS,
) -> pd.DataFrame:
    normalized = normalize_universe_snapshot(frame, snapshot_date=snapshot_date)
    row_count = int(len(normalized))
    if row_count < minimum_rows:
        raise StockCatalogNotReadyError(
            f"股票目录只有 {row_count} 只，未达到完整性门槛 {minimum_rows}；"
            "本次结果不会覆盖本地目录。"
        )
    markets = set(normalized["market"].dropna().astype(str))
    missing_markets = sorted(REQUIRED_MARKETS - markets)
    if missing_markets:
        raise StockCatalogNotReadyError(
            "股票目录缺少市场：" + "、".join(missing_markets) + "；本次结果不会覆盖本地目录。"
        )
    if normalized["symbol"].duplicated().any():
        raise StockCatalogNotReadyError("股票目录存在重复代码，本次结果不会覆盖本地目录。")
    return normalized


def load_full_stock_catalog(
    project_root: str | Path,
    *,
    minimum_rows: int = MIN_FULL_MARKET_ROWS,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    root = Path(project_root)
    csv_path, metadata_path = stock_catalog_paths(root)
    if not csv_path.exists():
        raise StockCatalogNotReadyError("尚未同步完整股票目录。")
    try:
        raw = pd.read_csv(csv_path)
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        raise StockCatalogNotReadyError(f"本地股票目录无法读取：{exc}") from exc
    normalized = _normalized_catalog(raw, minimum_rows=minimum_rows)
    metadata = _read_metadata(metadata_path)
    dates = normalized["data_date"].dropna().astype(str)
    metadata.update(
        {
            "ready": True,
            "is_full_market": True,
            "rows": int(len(normalized)),
            "snapshot_date": (
                metadata.get("snapshot_date")
                or (dates.max() if not dates.empty else None)
            ),
            "path": str(csv_path.relative_to(root)),
        }
    )
    return normalized, metadata


def stock_catalog_status(project_root: str | Path) -> dict[str, Any]:
    root = Path(project_root)
    csv_path, metadata_path = stock_catalog_paths(root)
    metadata = _read_metadata(metadata_path)
    actual_rows = 0
    actual_markets: list[str] = []
    if csv_path.exists():
        try:
            raw = pd.read_csv(csv_path)
            normalized = normalize_universe_snapshot(raw)
            actual_rows = int(len(normalized))
            actual_markets = sorted(
                set(normalized["market"].dropna().astype(str))
            )
        except (OSError, ValueError, pd.errors.ParserError):
            actual_rows = 0
    try:
        _frame, complete = load_full_stock_catalog(root)
    except (StockCatalogError, ValueError):
        return {
            "ready": False,
            "is_full_market": False,
            "rows": actual_rows,
            "minimum_rows": MIN_FULL_MARKET_ROWS,
            "markets": actual_markets,
            "snapshot_date": metadata.get("snapshot_date"),
            "fetched_at": metadata.get("fetched_at"),
            "provider": metadata.get("provider") or "东方财富公开全市场接口",
            "message": (
                f"当前目录只有 {actual_rows} 只，需从 Web 更新完整沪深北股票目录。"
                if actual_rows
                else "尚未同步完整沪深北股票目录。"
            ),
        }
    return {
        "ready": True,
        "is_full_market": True,
        "rows": int(complete["rows"]),
        "minimum_rows": MIN_FULL_MARKET_ROWS,
        "markets": sorted(REQUIRED_MARKETS),
        "snapshot_date": complete.get("snapshot_date"),
        "fetched_at": complete.get("fetched_at"),
        "provider": complete.get("provider") or "东方财富公开全市场接口",
        "catalog_hash": complete.get("catalog_hash"),
        "message": (
            f"完整股票目录可用，共 {complete['rows']} 只，"
            f"目录日期 {complete.get('snapshot_date') or '未知'}。"
        ),
    }


def _write_catalog_pair(
    project_root: Path,
    normalized: pd.DataFrame,
    metadata: dict[str, Any],
) -> None:
    csv_path, metadata_path = stock_catalog_paths(project_root)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_temporary = csv_path.with_suffix(".csv.tmp")
    metadata_temporary = metadata_path.with_suffix(".json.tmp")
    old_csv = csv_path.read_bytes() if csv_path.exists() else None
    old_metadata = metadata_path.read_bytes() if metadata_path.exists() else None
    try:
        normalized.to_csv(
            csv_temporary,
            index=False,
            columns=UNIVERSE_COLUMNS,
        )
        metadata_temporary.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        csv_temporary.replace(csv_path)
        metadata_temporary.replace(metadata_path)
    except OSError as exc:
        csv_temporary.unlink(missing_ok=True)
        metadata_temporary.unlink(missing_ok=True)
        try:
            if old_csv is None:
                csv_path.unlink(missing_ok=True)
            else:
                rollback_csv = csv_path.with_suffix(".csv.rollback")
                rollback_csv.write_bytes(old_csv)
                rollback_csv.replace(csv_path)
            if old_metadata is None:
                metadata_path.unlink(missing_ok=True)
            else:
                rollback_metadata = metadata_path.with_suffix(".json.rollback")
                rollback_metadata.write_bytes(old_metadata)
                rollback_metadata.replace(metadata_path)
        except OSError:
            pass
        raise StockCatalogError(f"完整股票目录保存失败：{exc}") from exc


def sync_stock_catalog(
    project_root: str | Path,
    *,
    fetcher: Callable[[], pd.DataFrame] | None = None,
    today: date | None = None,
    minimum_rows: int = MIN_FULL_MARKET_ROWS,
) -> dict[str, Any]:
    root = Path(project_root)
    provider = "自定义目录提供方"
    upstream = "custom"
    if fetcher is not None:
        try:
            raw = fetcher()
        except Exception as exc:
            raise StockCatalogError(f"Web 股票目录同步失败：{exc}") from exc
    else:
        provider = OFFICIAL_CATALOG_PROVIDER
        upstream = OFFICIAL_CATALOG_UPSTREAM
        try:
            raw = fetch_official_exchange_catalog()
        except Exception as official_error:
            provider = "东方财富公开全市场接口"
            upstream = "push2.eastmoney.com/api/qt/clist/get"
            try:
                raw = fetch_eastmoney_universe()
            except Exception as fallback_error:
                raise StockCatalogError(
                    "Web 股票目录同步失败：交易所公开列表暂不可用，"
                    f"备用目录也失败（{fallback_error}）。"
                ) from official_error
    requested_date = today or date.today()
    normalized = _normalized_catalog(
        raw,
        snapshot_date=requested_date,
        minimum_rows=minimum_rows,
    )
    csv_text = normalized.to_csv(
        index=False,
        columns=UNIVERSE_COLUMNS,
        lineterminator="\n",
    )
    catalog_hash = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
    dates = normalized["data_date"].dropna().astype(str)
    snapshot_date = dates.max() if not dates.empty else str(requested_date)
    metadata = {
        "provider": provider,
        "provider_version": None,
        "upstream": upstream,
        "source": "a_share_snapshot",
        "is_full_market": True,
        "snapshot_date": str(snapshot_date),
        "rows": int(len(normalized)),
        "markets": sorted(REQUIRED_MARKETS),
        "catalog_hash": catalog_hash,
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "path": str(
            (Path("data") / "universe" / UNIVERSE_FILENAME)
        ),
    }
    existing = stock_catalog_status(root)
    if existing.get("ready") and existing.get("catalog_hash") == catalog_hash:
        return {**existing, "unchanged": True}
    _write_catalog_pair(root, normalized, metadata)
    return {**stock_catalog_status(root), "unchanged": False}


def _project_local_symbols(project_root: Path) -> set[str]:
    symbols = set(local_stock_name_map(project_root))
    watchlist = project_root / "watchlist.csv"
    if watchlist.exists():
        try:
            frame = pd.read_csv(watchlist)
            for raw in frame.get("symbol", []):
                try:
                    symbols.add(validate_symbol(raw))
                except ValueError:
                    continue
        except (OSError, pd.errors.ParserError):
            pass
    data_root = project_root / "data"
    if data_root.exists():
        for directory in data_root.iterdir():
            if not directory.is_dir() or directory.name == "universe":
                continue
            try:
                symbols.add(validate_symbol(directory.name))
            except ValueError:
                continue
    return symbols


def _dataset_state(project_root: Path) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
    try:
        dataset = build_dataset_manifest(project_root)
    except (DatasetManifestError, ValueError, FileNotFoundError):
        return None, {}
    entries = {
        str(item.get("symbol")): item
        for item in dataset.get("entries", [])
        if item.get("symbol")
    }
    return dataset, entries


def search_stock_catalog(
    project_root: str | Path,
    query: str,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    root = Path(project_root)
    text = str(query or "").strip()
    if not text:
        return {
            "catalog": stock_catalog_status(root),
            "query": "",
            "total_matches": 0,
            "results": [],
        }
    if len(text) > CATALOG_QUERY_LIMIT:
        raise StockCatalogError(
            f"搜索内容不能超过 {CATALOG_QUERY_LIMIT} 个字符。"
        )
    try:
        result_limit = int(limit)
    except (TypeError, ValueError):
        raise StockCatalogError("搜索数量格式不正确。") from None
    if not 1 <= result_limit <= CATALOG_RESULT_LIMIT:
        raise StockCatalogError(
            f"每次最多显示 {CATALOG_RESULT_LIMIT} 条搜索结果。"
        )

    frame, metadata = load_full_stock_catalog(root)
    query_lower = text.lower()
    query_upper = text.upper()
    query_digits = "".join(character for character in text if character.isdigit())
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for _, row in frame.iterrows():
        symbol = str(row["symbol"])
        code = symbol.split(".", 1)[0]
        name = str(row["stock_name"])
        name_lower = name.lower()
        if query_lower not in symbol.lower() and query_lower not in name_lower:
            if not query_digits or query_digits not in code:
                continue
        if query_upper in {symbol.upper(), code}:
            rank = 0
        elif query_digits and code.startswith(query_digits):
            rank = 1
        elif name_lower.startswith(query_lower):
            rank = 2
        elif query_lower in name_lower:
            rank = 3
        else:
            rank = 4
        candidates.append((rank, symbol, row.to_dict()))
    candidates.sort(key=lambda item: (item[0], item[1]))

    local_symbols = _project_local_symbols(root)
    _dataset, dataset_entries = _dataset_state(root)
    results = []
    for _rank, symbol, row in candidates[:result_limit]:
        local_entry = dataset_entries.get(symbol)
        results.append(
            {
                "symbol": symbol,
                "stock_name": str(row.get("stock_name") or symbol),
                "market": str(row.get("market") or symbol.rsplit(".", 1)[-1]),
                "catalog_date": str(row.get("data_date") or metadata.get("snapshot_date") or ""),
                "in_local_library": symbol in local_symbols,
                "local_data_status": (
                    str(local_entry.get("status"))
                    if local_entry is not None
                    else None
                ),
                "local_latest_date": (
                    (
                        local_entry.get("last_used_trade_date")
                        or local_entry.get("last_trade_date")
                    )
                    if local_entry is not None
                    else None
                ),
            }
        )
    return {
        "catalog": stock_catalog_status(root),
        "query": text,
        "total_matches": len(candidates),
        "results": results,
    }


def _has_local_day_data(project_root: Path, symbol: str) -> bool:
    try:
        find_latest_csv(
            symbol,
            "day",
            base_dir=str(project_root / "data"),
        )
    except (ValueError, FileNotFoundError):
        return False
    return True


def add_stock_from_catalog(
    project_root: str | Path,
    payload: dict[str, Any],
    *,
    updater: Callable[..., dict[str, Any]] = update_market_data,
) -> dict[str, Any]:
    root = Path(project_root)
    requested = str(payload.get("symbol") or "").strip().upper()
    try:
        normalized = validate_symbol(requested)
        parse_a_share_symbol(normalized)
    except (ValueError, MarketDataError) as exc:
        raise StockCatalogError(str(exc)) from exc

    catalog, metadata = load_full_stock_catalog(root)
    matched = catalog.loc[catalog["symbol"] == normalized]
    if matched.empty:
        raise StockCatalogError(
            "该股票不在当前完整沪深北目录中，请先更新目录后重试。"
        )
    catalog_row = matched.iloc[-1]
    local_symbols_before = _project_local_symbols(root)
    already_present = normalized in local_symbols_before
    had_day_data = _has_local_day_data(root, normalized)
    if already_present and had_day_data:
        dataset, entries = _dataset_state(root)
        return {
            "symbol": normalized,
            "stock_name": str(catalog_row["stock_name"]),
            "membership_status": "EXISTING",
            "import_status": "ALREADY_PRESENT",
            "already_present": True,
            "data": None,
            "dataset": dataset,
            "entry": entries.get(normalized),
        }

    member, added = add_local_stock_member(
        root,
        symbol=normalized,
        stock_name=str(catalog_row["stock_name"]),
        catalog_snapshot_date=str(metadata.get("snapshot_date") or ""),
    )
    try:
        data_result = updater(
            root,
            symbol=normalized,
            timeframe="day",
            adjust="qfq",
            history_years=3,
        )
    except (MarketDataError, OSError):
        data_saved = _has_local_day_data(root, normalized)
        dataset, entries = _dataset_state(root)
        return {
            "symbol": normalized,
            "stock_name": member["stock_name"],
            "membership_status": "ADDED" if added else "EXISTING",
            "import_status": "SAVED_WITH_WARNING" if data_saved else "FAILED",
            "already_present": already_present,
            "data_saved": data_saved,
            "import_error": "股票已加入本地库，但行情暂时无法导入，请稍后重试。",
            "data": None,
            "dataset": dataset,
            "entry": entries.get(normalized),
        }

    dataset, entries = _dataset_state(root)
    return {
        "symbol": normalized,
        "stock_name": member["stock_name"],
        "membership_status": "ADDED" if added else "EXISTING",
        "import_status": "IMPORTED",
        "already_present": already_present,
        "data_saved": True,
        "data": data_result,
        "dataset": dataset,
        "entry": entries.get(normalized),
    }
