from __future__ import annotations

import base64
import csv
from datetime import date, datetime
from hashlib import sha256
from io import StringIO
import json
import math
from pathlib import Path
import re
import shutil
import threading
from typing import Any
from uuid import uuid4

import pandas as pd


class PersonalDataError(ValueError):
    pass


MAX_IMPORT_BYTES = 5 * 1024 * 1024
SUPPORTED_KINDS = {"positions", "trades"}
TARGET_FILES = {
    "positions": Path("records/positions_snapshot.csv"),
    "trades": Path("records/my_trades.csv"),
}

POSITION_COLUMNS = [
    "snapshot_date",
    "symbol",
    "stock_name",
    "shares_available",
    "shares_total",
    "avg_cost",
    "current_price",
    "market_value",
    "unrealized_pnl",
    "unrealized_pnl_pct",
    "position_pct",
    "day_pnl",
    "day_pnl_pct",
    "account_total_asset",
    "rule_status",
    "notes",
]

TRADE_COLUMNS = [
    "trade_date",
    "trade_time",
    "symbol",
    "stock_name",
    "market",
    "side",
    "operation",
    "price",
    "shares",
    "gross_amount",
    "fee",
    "stamp_tax",
    "transfer_fee",
    "other_fee",
    "net_amount",
    "avg_cost_after_trade",
    "realized_pnl",
    "remaining_shares",
    "account_total_asset",
    "position_market_value",
    "position_pct",
    "rule_status",
    "emotion",
    "checklist_version",
    "reason_tags_json",
    "discipline_checks_json",
    "emotion_flags_json",
    "emotion_clear",
    "record_ref",
    "source",
    "notes",
]

# 旧台账的兼容引用只取这组固定成交事实。未来增加展示或审查字段时，
# 不会因 TRADE_COLUMNS 扩展而让尚未持久化的旧引用变化。
LEGACY_RECORD_REF_FACT_COLUMNS = (
    "trade_date",
    "trade_time",
    "symbol",
    "side",
    "price",
    "shares",
    "gross_amount",
    "fee",
    "stamp_tax",
    "transfer_fee",
    "other_fee",
    "net_amount",
    "avg_cost_after_trade",
    "realized_pnl",
    "remaining_shares",
    "source",
)

_TRADE_WRITE_LOCK = threading.RLock()

TRADE_REASON_LABELS = {
    "BUY": {
        "RULE_TRIGGER": "来自当前规则筛选候选",
        "LOGIC_STATUS": "核心逻辑仍成立",
        "CLOSE_CONFIRMATION": "收盘确认信号已出现",
        "PLANNED_BATCH": "按计划分批买入",
    },
    "SELL": {
        "RULE_TRIGGER": "事前止损或退出规则已触发",
        "LOGIC_STATUS": "核心逻辑已经失效",
        "CLOSE_CONFIRMATION": "计划卖出条件已到达",
        "PLANNED_BATCH": "按计划分批卖出",
    },
}

DISCIPLINE_CHECK_LABELS = {
    "EXIT_PLAN": "已提前写明止损或退出条件",
    "POSITION_CHECK": "已核对仓位和数量",
    "NOT_IMPULSIVE": "不是盘中临时起意",
}

EMOTION_FLAG_LABELS = {
    "FOMO": "害怕错过或追涨",
    "RECOVERY_URGE": "焦虑、后悔或急于回本",
    "CHASE_BACK": "刚卖飞想追回",
    "OVERWATCH": "连续盯盘超过20分钟",
    "FEELING_ONLY": "操作理由只是感觉",
}

# 四类操作只由「本笔成交股数」和「成交后剩余股数」这两个已记录事实推出，
# 不看券商摘要文字，也不重新推算持仓。
TRADE_OPERATION_LABELS = {
    "FIRST_BUY": "首次买入",
    "ADD": "加仓",
    "REDUCE": "减仓",
    "CLOSE": "清仓",
}

# 台账每笔成交必须具备的事实字段。缺任何一项都只标记「历史数据不完整」，
# 不倒推、不拿别的字段凑数。成交时间刻意不在其中：它允许未知。
# 费用也不在其中：费用未知不算缺字段，而是让受它影响的成本与盈亏带「不含未知费用」。
TRADE_FACT_LABELS = {
    "trade_date": "成交日期",
    "symbol": "股票代码",
    "side": "买卖方向",
    "price": "成交价格",
    "shares": "成交数量",
    "remaining_shares": "成交后剩余股数",
    "avg_cost_after_trade": "成交后平均成本",
    "realized_pnl": "该笔已实现盈亏",
}

UNKNOWN_TIME_LABEL = "未记录"
INCOMPLETE_HISTORY_LABEL = "历史数据不完整"
UNCLASSIFIED_OPERATION_LABEL = "未归类"
FEE_UNKNOWN_LABEL = "费用未知"
FEE_EXCLUDED_LABEL = "不含未知费用"
ORDER_KNOWN = "ORDER_KNOWN"
ORDER_AMBIGUOUS = "ORDER_AMBIGUOUS"
ORDER_STATUS_LABELS = {
    ORDER_KNOWN: "顺序明确",
    ORDER_AMBIGUOUS: "顺序待核对",
}
ORDER_AMBIGUOUS_REASON = "同一天有成交没有记录成交时间，先后顺序无法确认"

ALIASES = {
    "snapshot_date": ["snapshot_date", "快照日期", "日期", "数据日期"],
    "trade_date": ["trade_date", "成交日期", "交易日期", "日期"],
    "trade_time": ["trade_time", "成交时间", "交易时间", "时间"],
    "symbol": ["symbol", "证券代码", "股票代码", "代码", "证券编号"],
    "stock_name": ["stock_name", "证券名称", "股票名称", "名称"],
    "market": ["market", "市场", "交易所"],
    "shares_available": [
        "shares_available",
        "可用股份",
        "可用数量",
        "可用余额",
        "可卖数量",
        "可卖股数",
        "可用股数",
    ],
    "shares_total": [
        "shares_total",
        "股份余额",
        "持仓数量",
        "持仓股数",
        "持有数量",
        "证券数量",
        "证券余额",
        "股票余额",
        "当前拥股",
    ],
    "avg_cost": ["avg_cost", "平均成本", "成本价", "持仓成本", "摊薄成本价"],
    "current_price": ["current_price", "当前价", "现价", "市价", "最新价", "最新市价"],
    "market_value": ["market_value", "市值", "证券市值", "持仓市值"],
    "unrealized_pnl": ["unrealized_pnl", "浮动盈亏", "持仓盈亏", "盈亏"],
    "unrealized_pnl_pct": ["unrealized_pnl_pct", "盈亏比例", "盈亏率", "持仓盈亏比例"],
    "position_pct": ["position_pct", "仓位比例", "持仓占比"],
    "day_pnl": ["day_pnl", "当日盈亏", "今日盈亏"],
    "day_pnl_pct": ["day_pnl_pct", "当日盈亏比例", "今日盈亏率"],
    "account_total_asset": ["account_total_asset", "账户总资产", "总资产", "资产总值"],
    "side": ["side", "买卖方向", "操作方向", "委托方向", "业务名称"],
    "operation": ["operation", "操作", "成交类型", "业务名称"],
    "price": ["price", "成交价格", "成交均价", "成交价"],
    "shares": ["shares", "成交数量", "成交股数", "发生数量"],
    "gross_amount": ["gross_amount", "成交金额", "发生金额", "成交额"],
    "fee": ["fee", "佣金", "手续费"],
    "stamp_tax": ["stamp_tax", "印花税"],
    "transfer_fee": ["transfer_fee", "过户费"],
    "other_fee": ["other_fee", "其他费用", "其他费"],
    "net_amount": ["net_amount", "发生金额净额", "清算金额", "资金发生数"],
    "avg_cost_after_trade": ["avg_cost_after_trade", "成交后成本", "剩余成本价"],
    "realized_pnl": ["realized_pnl", "实现盈亏", "已实现盈亏"],
    "remaining_shares": ["remaining_shares", "成交后余额", "剩余股数", "股份余额"],
    "position_market_value": ["position_market_value", "成交后市值", "持仓市值"],
    "rule_status": ["rule_status", "规则状态"],
    "emotion": ["emotion", "情绪记录", "情绪"],
    "record_ref": [
        "record_ref",
        "成交编号",
        "成交序号",
        "交易流水号",
        "trade_id",
        "trade_no",
    ],
    "source": ["source", "数据来源", "来源"],
    "notes": ["notes", "备注", "说明"],
}


def _clean_column(value: Any) -> str:
    return re.sub(r"[\s\u3000]+", "", str(value or "")).lower()


def _column_map(frame: pd.DataFrame) -> dict[str, str]:
    available = {_clean_column(column): str(column) for column in frame.columns}
    mapped: dict[str, str] = {}
    for canonical, aliases in ALIASES.items():
        for alias in aliases:
            source = available.get(_clean_column(alias))
            if source is not None:
                mapped[canonical] = source
                break
    return mapped


def _series(frame: pd.DataFrame, mapping: dict[str, str], key: str, default: Any = "") -> pd.Series:
    source = mapping.get(key)
    if source is None:
        return pd.Series([default] * len(frame), index=frame.index, dtype="object")
    return frame[source]


def _numbers(series: pd.Series, *, percentage: bool = False) -> pd.Series:
    cleaned = (
        series.astype(str)
        .str.replace(",", "", regex=False)
        .str.replace("￥", "", regex=False)
        .str.replace("元", "", regex=False)
        .str.replace("股", "", regex=False)
        .str.replace("--", "", regex=False)
        .str.strip()
    )
    has_percent = cleaned.str.contains("%", regex=False)
    cleaned = cleaned.str.replace("%", "", regex=False)
    values = pd.to_numeric(cleaned, errors="coerce")
    if percentage:
        values = values.where(~has_percent, values / 100)
    return values


def _market_from_code(code: str) -> str:
    if code.startswith("6"):
        return "SH"
    if code.startswith(("0", "3")):
        return "SZ"
    if code.startswith(("4", "8", "9")):
        return "BJ"
    return ""


def _normalize_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    text = text.replace("XSHG", "SH").replace("XSHE", "SZ")
    compact = re.sub(r"[^0-9A-Z]", "", text)
    match = re.search(r"(\d{6})", compact)
    if not match:
        raise PersonalDataError(f"无法识别股票代码：{text or '空值'}")
    code = match.group(1)
    if "SH" in compact:
        market = "SH"
    elif "SZ" in compact:
        market = "SZ"
    elif "BJ" in compact:
        market = "BJ"
    else:
        market = _market_from_code(code)
    if not market:
        raise PersonalDataError(f"无法判断股票市场：{text}")
    return f"{code}.{market}"


def _normalize_dates(series: pd.Series, label: str, default: date | None = None) -> pd.Series:
    raw = series.astype(str).str.strip().replace({"": None, "nan": None, "None": None})
    parsed = pd.to_datetime(raw, errors="coerce")
    if default is not None:
        parsed = parsed.fillna(pd.Timestamp(default))
    if parsed.isna().any():
        row = int(parsed[parsed.isna()].index[0]) + 2
        raise PersonalDataError(f"{label}第 {row} 行无法识别。")
    return parsed.dt.strftime("%Y-%m-%d")


def _clean_text(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).replace({"nan": "", "None": ""}).str.strip()


def _read_payload_bytes(payload: dict[str, Any]) -> tuple[bytes, str]:
    filename = str(payload.get("filename") or "import.csv").strip() or "import.csv"
    if payload.get("csv_text") is not None:
        raw = str(payload.get("csv_text") or "").encode("utf-8")
    else:
        encoded = str(payload.get("content_base64") or "")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise PersonalDataError("导入文件内容格式不正确。") from exc
    if not raw:
        raise PersonalDataError("导入文件为空。")
    if len(raw) > MAX_IMPORT_BYTES:
        raise PersonalDataError("导入文件不能超过 5 MB。")
    return raw, filename


def _decode_csv(raw: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise PersonalDataError("文件编码无法识别，请使用 UTF-8 或 GB18030 CSV。")


def _parse_frame(payload: dict[str, Any]) -> tuple[pd.DataFrame, str, str, list[str]]:
    raw, filename = _read_payload_bytes(payload)
    text, encoding = _decode_csv(raw)
    try:
        frame = pd.read_csv(StringIO(text), dtype=str, keep_default_na=False)
    except (pd.errors.ParserError, UnicodeError, ValueError) as exc:
        raise PersonalDataError(f"CSV 无法解析：{exc}") from exc
    if frame.empty:
        raise PersonalDataError("CSV 没有可导入的数据行。")
    if len(frame) > 100_000:
        raise PersonalDataError("单次导入不能超过 100,000 行。")
    duplicate_columns = [str(column) for column in frame.columns if list(frame.columns).count(column) > 1]
    if duplicate_columns:
        raise PersonalDataError(f"CSV 存在重复列：{sorted(set(duplicate_columns))}")
    return frame, filename, encoding, []


def _normalize_positions(frame: pd.DataFrame, filename: str) -> tuple[pd.DataFrame, list[str]]:
    mapping = _column_map(frame)
    if "symbol" not in mapping:
        raise PersonalDataError("持仓 CSV 缺少证券代码/股票代码列。")
    shares_key = "shares_total" if "shares_total" in mapping else "remaining_shares"
    if shares_key not in mapping:
        raise PersonalDataError("持仓 CSV 缺少持仓数量/股份余额列。")

    warnings: list[str] = []
    output = pd.DataFrame(index=frame.index)
    output["snapshot_date"] = _normalize_dates(
        _series(frame, mapping, "snapshot_date"), "快照日期", default=date.today()
    )
    if "snapshot_date" not in mapping:
        warnings.append("未找到快照日期，已使用导入当天日期。")
    output["symbol"] = _series(frame, mapping, "symbol").map(_normalize_symbol)
    output["stock_name"] = _clean_text(_series(frame, mapping, "stock_name"))
    output.loc[output["stock_name"] == "", "stock_name"] = output["symbol"]

    output["shares_total"] = _numbers(_series(frame, mapping, shares_key)).fillna(0).round().astype(int)
    if (output["shares_total"] < 0).any():
        raise PersonalDataError("持仓数量不能为负数。")
    output["shares_available"] = _numbers(_series(frame, mapping, "shares_available"))
    output["shares_available"] = output["shares_available"].fillna(output["shares_total"]).round().astype(int)
    output["avg_cost"] = _numbers(_series(frame, mapping, "avg_cost"))
    output["current_price"] = _numbers(_series(frame, mapping, "current_price"))
    output["market_value"] = _numbers(_series(frame, mapping, "market_value"))
    derived_market_value = output["shares_total"] * output["current_price"]
    output["market_value"] = output["market_value"].fillna(derived_market_value)
    output["unrealized_pnl"] = _numbers(_series(frame, mapping, "unrealized_pnl"))
    derived_pnl = output["market_value"] - output["shares_total"] * output["avg_cost"]
    output["unrealized_pnl"] = output["unrealized_pnl"].fillna(derived_pnl)
    output["unrealized_pnl_pct"] = _numbers(
        _series(frame, mapping, "unrealized_pnl_pct"), percentage=True
    )
    cost_value = output["shares_total"] * output["avg_cost"]
    derived_pnl_pct = output["unrealized_pnl"] / cost_value.where(cost_value != 0)
    output["unrealized_pnl_pct"] = output["unrealized_pnl_pct"].fillna(derived_pnl_pct)
    output["account_total_asset"] = _numbers(_series(frame, mapping, "account_total_asset"))
    output["position_pct"] = _numbers(_series(frame, mapping, "position_pct"), percentage=True)
    derived_position = output["market_value"] / output["account_total_asset"].where(
        output["account_total_asset"] > 0
    )
    output["position_pct"] = output["position_pct"].fillna(derived_position)
    output["day_pnl"] = _numbers(_series(frame, mapping, "day_pnl"))
    output["day_pnl_pct"] = _numbers(_series(frame, mapping, "day_pnl_pct"), percentage=True)
    output["rule_status"] = _clean_text(_series(frame, mapping, "rule_status"))
    existing_notes = _clean_text(_series(frame, mapping, "notes"))
    import_note = f"数据中心导入：{filename}"
    output["notes"] = existing_notes.map(lambda value: f"{value}；{import_note}" if value else import_note)

    if output["avg_cost"].isna().any():
        warnings.append("部分持仓缺少平均成本，单股审查时需要补充。")
    if output["current_price"].isna().any():
        warnings.append("部分持仓缺少当前价格，将优先使用本地最新行情。")
    if output["account_total_asset"].isna().all():
        warnings.append("未找到账户总资产，单股审查时需要填写。")
    for column in ["avg_cost", "current_price"]:
        output[column] = output[column].round(4)
    for column in ["market_value", "unrealized_pnl", "day_pnl", "account_total_asset"]:
        output[column] = output[column].round(2)
    for column in ["unrealized_pnl_pct", "position_pct", "day_pnl_pct"]:
        output[column] = output[column].round(6)
    output = output[POSITION_COLUMNS].sort_values(["snapshot_date", "symbol"]).reset_index(drop=True)
    return output, warnings


def _normalize_side(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"BUY", "B", "买", "买入", "证券买入", "融资买入"} or "买入" in text:
        return "BUY"
    if text in {"SELL", "S", "卖", "卖出", "证券卖出", "卖券还款"} or "卖出" in text:
        return "SELL"
    raise PersonalDataError(f"无法识别买卖方向：{value}")


def _trade_number(value: Any, label: str, *, integer: bool = False) -> float | int:
    try:
        parsed = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        raise PersonalDataError(f"{label}必须是有效数字。") from None
    if not math.isfinite(parsed) or parsed <= 0:
        raise PersonalDataError(f"{label}必须大于 0。")
    if integer:
        rounded = int(round(parsed))
        if abs(parsed - rounded) > 1e-9:
            raise PersonalDataError(f"{label}必须是整数。")
        return rounded
    return parsed


def _reported_trade_number(value: Any, label: str) -> float | None:
    """读用户自报的可选金额。留空返回 None（未知），明确填 0 返回 0.0。

    这两者绝不能折叠成同一个值：0 是「确实没有费用」这个事实，
    None 是「不知道」，后者不得让受它影响的成本或盈亏被当成精确完整值。
    """
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError:
        raise PersonalDataError(f"{label}必须是有效数字。") from None
    if not math.isfinite(parsed):
        raise PersonalDataError(f"{label}必须是有效数字。")
    if parsed < 0:
        raise PersonalDataError(f"{label}不能小于 0。")
    return parsed


def _trade_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            return [
                {column: str(row.get(column) or "").strip() for column in TRADE_COLUMNS}
                for row in reader
            ]
    except (OSError, csv.Error) as exc:
        raise PersonalDataError(f"操作记录无法读取：{exc}") from exc


def _trade_date_value(value: Any) -> date | None:
    """只读真实成交日期。无法识别时返回 None，不用其他日期补位。"""
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def _actual_trade_timestamp(row: dict[str, str]) -> datetime | None:
    """只有日期和实际成交时间都存在时才返回时刻。"""
    trade_date = _trade_date_value(row.get("trade_date"))
    trade_time = _normalize_trade_time(row.get("trade_time"))
    if trade_date is None or not trade_time:
        return None
    return datetime.fromisoformat(f"{trade_date.isoformat()} {trade_time}")


def _trade_business_sort_key(row: dict[str, str]) -> tuple[date, str]:
    """仅在顺序已被证明时使用的业务排序键。"""
    return (
        _trade_date_value(row.get("trade_date")) or date.min,
        _normalize_trade_time(row.get("trade_time")),
    )


def _trade_display_sort_key(row: dict[str, str]) -> tuple[date, int, str, str]:
    """只用于让列表稳定展示，不证明同日成交的业务先后。"""
    trade_time = _normalize_trade_time(row.get("trade_time"))
    return (
        _trade_date_value(row.get("trade_date")) or date.min,
        0 if trade_time else 1,
        trade_time,
        str(row.get("record_ref") or "").strip(),
    )


def _legacy_record_payload(row: dict[str, str]) -> str:
    return "\x1f".join(
        str(row.get(column) or "").strip()
        for column in LEGACY_RECORD_REF_FACT_COLUMNS
    )


def _legacy_record_ref(payload: str, occurrence: int) -> str:
    digest = sha256(f"{payload}\x1e{occurrence}".encode("utf-8")).hexdigest()[:24]
    return f"legacy-{digest}"


def _assign_trade_record_refs(rows: list[dict[str, str]]) -> bool:
    """为旧台账补稳定身份，不修改任何成交事实。

    手工记录恢复 request_id；其他旧行使用「行事实 + 同内容出现次序」
    生成兼容引用。出现次序使两笔内容完全相同的真实成交仍有不同引用。
    此函数只改内存中的 record_ref；读取不会写盘，下一次明确追加时才
    由现有带备份的整表写入流程持久化。
    """
    changed = False
    used: set[str] = set()
    occurrences: dict[str, int] = {}
    for row in rows:
        existing = str(row.get("record_ref") or "").strip()
        source = str(row.get("source") or "").strip()
        candidate = existing
        if not candidate and source.startswith("panel_manual:"):
            candidate = source.removeprefix("panel_manual:").strip()

        if not candidate or candidate in used:
            payload = _legacy_record_payload(row)
            occurrence = occurrences.get(payload, 0) + 1
            candidate = _legacy_record_ref(payload, occurrence)
            while candidate in used:
                occurrence += 1
                candidate = _legacy_record_ref(payload, occurrence)
            occurrences[payload] = occurrence

        if existing != candidate:
            row["record_ref"] = candidate
            changed = True
        used.add(candidate)
    return changed


def read_trade_rows(path: str | Path) -> list[dict[str, str]]:
    """按原始字符串读取成交，并在内存中补齐兼容引用；绝不写回文件。"""
    rows = _trade_rows(Path(path))
    _assign_trade_record_refs(rows)
    return rows


def _normalize_trade_time(value: Any) -> str:
    """把成交时间统一成 HH:MM:SS；识别不了就留空，由调用方决定报错还是按未记录处理。"""
    text = re.sub(r"[\s　]+", "", str(value or ""))
    if not text:
        return ""
    for pattern in ("%H:%M:%S", "%H:%M", "%H%M%S", "%H%M"):
        try:
            return datetime.strptime(text, pattern).strftime("%H:%M:%S")
        except ValueError:
            continue
    return ""


def _trade_time_label(value: Any) -> str:
    normalized = _normalize_trade_time(value)
    return normalized[:5] if normalized else UNKNOWN_TIME_LABEL


def _stored_number(value: Any) -> float | None:
    """读台账里已经落盘的数字。空值或读不出来一律返回 None，绝不补默认值。"""
    text = "" if value is None else str(value).replace(",", "").strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _stored_int(value: Any) -> int | None:
    parsed = _stored_number(value)
    return None if parsed is None else int(round(parsed))


def _trade_operation_class(side: str, shares: int | None, remaining: int | None) -> str:
    if side == "BUY":
        if shares is None or remaining is None:
            return ""
        previous = remaining - shares
        if previous < 0:
            return ""
        return "FIRST_BUY" if previous == 0 else "ADD"
    if side == "SELL":
        if remaining is None:
            return ""
        return "CLOSE" if remaining == 0 else "REDUCE"
    return ""


def _trade_ledger_facts(
    row: dict[str, str],
    *,
    fee_complete: bool = True,
) -> dict[str, Any]:
    """只复述该行已经写下的事实，并如实报告缺了哪些字段、数值是否已含全部费用。"""
    side = str(row.get("side") or "").strip().upper()
    price = _stored_number(row.get("price"))
    shares = _stored_int(row.get("shares"))
    remaining = _stored_int(row.get("remaining_shares"))
    average_cost = _stored_number(row.get("avg_cost_after_trade"))
    realized = _stored_number(row.get("realized_pnl"))
    fee = _stored_number(row.get("fee"))
    # 「本笔费用合计」在两条路径上的表达方式不同，读取时必须按来源分别理解：
    # 手工行把合计直接写进 fee，三项明细留空表示「未单独记录」；
    # 导入行的 fee 列语义是券商的「佣金」，只有四项明细齐全时合计才算已知——
    # 只有佣金=0 而印花税缺列时说「本笔费用合计 0 元」是假话。
    if str(row.get("source") or "").startswith("panel_manual:"):
        fee_total = fee
    else:
        details = [
            _stored_number(row.get(key))
            for key in ("fee", "stamp_tax", "transfer_fee", "other_fee")
        ]
        fee_total = None if any(d is None for d in details) else round(sum(details), 4)
    present = {
        "trade_date": _trade_date_value(row.get("trade_date")) is not None,
        "symbol": bool(str(row.get("symbol") or "").strip()),
        "side": side in {"BUY", "SELL"},
        "price": price is not None and price > 0,
        "shares": shares is not None and shares > 0,
        "remaining_shares": remaining is not None and remaining >= 0,
        "avg_cost_after_trade": average_cost is not None and average_cost >= 0,
        "realized_pnl": realized is not None,
    }
    missing = [TRADE_FACT_LABELS[key] for key, ok in present.items() if not ok]
    # 四类归类只由本行**已被接受**的成交股数与成交后剩余股数推出。
    # 绝不用刚刚判为缺失的值（例如源文件给了负数剩余股数）去归类，
    # 也绝不反解 operation 列里的中文标签——那四个词本身就是面板算出来的。
    operation_class = _trade_operation_class(
        side,
        shares if present["shares"] else None,
        remaining if present["remaining_shares"] else None,
    )
    # 费用只影响两处：买入费用计入成本，卖出费用计入已实现盈亏。
    # 买入的已实现盈亏恒为 0，与费用无关，所以那一格不加限定语。
    caveat = "" if fee_complete else FEE_EXCLUDED_LABEL
    cost_note = caveat if present["avg_cost_after_trade"] else ""
    realized_note = (
        caveat if present["realized_pnl"] and side == "SELL" else ""
    )
    return {
        "price": price if present["price"] else None,
        "shares": shares if present["shares"] else None,
        "remaining_shares": remaining if present["remaining_shares"] else None,
        "avg_cost_after_trade": average_cost if present["avg_cost_after_trade"] else None,
        "realized_pnl": realized if present["realized_pnl"] else None,
        "fee": fee_total,
        "fee_reported": fee_total is not None,
        "fee_complete": bool(fee_complete),
        # 只有确实存在受费用影响的数值时才提这句，否则数字位已经是「—」，无须再加注。
        "fee_note": cost_note or realized_note,
        "cost_fee_note": cost_note,
        "realized_fee_note": realized_note,
        "exact": (not missing) and bool(fee_complete),
        "operation_class": operation_class,
        "operation_label": TRADE_OPERATION_LABELS.get(
            operation_class, UNCLASSIFIED_OPERATION_LABEL
        ),
        "complete": not missing,
        "missing_fields": missing,
    }


def _trade_fee_chain(rows: list[dict[str, str]]) -> dict[int, bool]:
    """标出每一行的成本与已实现盈亏是否已含全部费用。

    面板自己算出来的行（source 以 panel_manual: 开头）在费用留空时把标记置为「未含」，
    并沿该股票之后的每一笔继续传递——早先一笔费用未知，后面的摊薄成本同样不含它。
    源文件直接给出剩余股数与平均成本的行会重置标记：那两个数字是券商的口径，不是我们算的。
    """
    by_symbol: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        symbol = str(row.get("symbol") or "").strip()
        if symbol:
            by_symbol.setdefault(symbol, []).append(index)

    carried_by_index: dict[int, bool] = {}
    for indexes in by_symbol.values():
        matching = [rows[index] for index in indexes]
        if _order_is_ambiguous(matching):
            # 顺序不可证时不让展示行序决定费用如何向后传递。
            # 只要这组手工成交里有一笔费用未知，就不声称其他手工行的
            # 成本链已包全部费用。券商直接给出的本行事实仍沿用功能 1.1 口径。
            manual_fee_chain_complete = all(
                _stored_number(row.get("fee")) is not None
                for row in matching
                if str(row.get("source") or "").startswith("panel_manual:")
            )
            for index in indexes:
                row = rows[index]
                if str(row.get("source") or "").startswith("panel_manual:"):
                    carried_by_index[index] = manual_fee_chain_complete
                else:
                    carried_by_index[index] = True
            continue

        indexes.sort(key=lambda index: _trade_business_sort_key(rows[index]))
        carried = True
        for index in indexes:
            row = rows[index]
            if str(row.get("source") or "").startswith("panel_manual:"):
                if _stored_number(row.get("fee")) is None:
                    carried = False
            elif (
                _stored_int(row.get("remaining_shares")) is not None
                and _stored_number(row.get("avg_cost_after_trade")) is not None
            ):
                carried = True
            carried_by_index[index] = carried
            # 清仓后成本基数归零：之后重新买入的成本与更早那些未知费用再无关系。
            # 但清仓这一笔自己的已实现盈亏仍受未知费用影响，所以先记下再复位。
            if _stored_int(row.get("remaining_shares")) == 0:
                carried = True
    return carried_by_index


def _order_is_ambiguous(rows: list[dict[str, str]]) -> bool:
    """判断业务先后是否有事实依据。

    创建顺序、提交时间、CSV 行序和稳定展示顺序都不是成交顺序事实。
    同股同日多笔中只要任一笔时间未知，本轮就不选择「最后成交」。
    """
    return _trade_order_status(rows) == ORDER_AMBIGUOUS


def _trade_order_status(rows: list[dict[str, str]]) -> str:
    by_date: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        trade_date = _trade_date_value(row.get("trade_date"))
        if trade_date is None:
            return ORDER_AMBIGUOUS
        by_date.setdefault(trade_date.isoformat(), []).append(row)
    for group in by_date.values():
        if len(group) > 1 and any(
            not _normalize_trade_time(row.get("trade_time")) for row in group
        ):
            return ORDER_AMBIGUOUS
        known_times = [
            _normalize_trade_time(row.get("trade_time"))
            for row in group
            if _normalize_trade_time(row.get("trade_time"))
        ]
        if len(known_times) != len(set(known_times)):
            return ORDER_AMBIGUOUS
    return ORDER_KNOWN


def _trade_order_reasons(rows: list[dict[str, str]]) -> list[str]:
    reasons: list[str] = []
    if any(_trade_date_value(row.get("trade_date")) is None for row in rows):
        reasons.append("有成交缺少可识别的成交日期，先后顺序无法确认")
    if _trade_order_status(rows) == ORDER_AMBIGUOUS:
        by_date: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            trade_date = _trade_date_value(row.get("trade_date"))
            if trade_date is not None:
                by_date.setdefault(trade_date.isoformat(), []).append(row)
        if any(
            len(group) > 1
            and any(not _normalize_trade_time(row.get("trade_time")) for row in group)
            for group in by_date.values()
        ):
            reasons.append(ORDER_AMBIGUOUS_REASON)
        if any(
            len(times) != len(set(times))
            for times in (
                [
                    _normalize_trade_time(row.get("trade_time"))
                    for row in group
                    if _normalize_trade_time(row.get("trade_time"))
                ]
                for group in by_date.values()
            )
        ):
            reasons.append("同一天有多笔成交记录了相同时间，先后顺序无法确认")
    return list(dict.fromkeys(reasons))


def _recorded_position_state(
    rows: list[dict[str, str]],
    symbol: str,
    fee_chain: dict[int, bool] | None = None,
) -> dict[str, Any]:
    """该股票在台账里已经记录的最新持仓状态，读不出来就明确报未知。

    绝不按买卖方向重建持仓，绝不把未知成本当成 0，也绝不沿用上一行的数字顶替这一行。
    """
    indexes = [index for index, row in enumerate(rows) if row.get("symbol") == symbol]
    if not indexes:
        # 台账里这只股票一笔都没有：前序确实是 0 股 0 成本，这是记录本身的事实，不是假定。
        return {
            "has_history": False,
            "known": True,
            "shares": 0,
            "cost": 0.0,
            "fee_complete": True,
            "latest_timestamp": None,
            "latest_trade_date": None,
            "latest_time_known": True,
            "order_status": ORDER_KNOWN,
            "reasons": [],
        }

    matching = [rows[index] for index in indexes]
    order_status = _trade_order_status(matching)
    ordered_indexes = sorted(
        indexes, key=lambda index: _trade_business_sort_key(rows[index])
    )
    facts = [_trade_ledger_facts(rows[index]) for index in ordered_indexes]
    all_dates = [_trade_date_value(row.get("trade_date")) for row in matching]
    dates_valid = all(value is not None for value in all_dates)
    latest_trade_date = max(all_dates) if dates_valid else None

    latest_index = ordered_indexes[-1] if order_status == ORDER_KNOWN else None
    latest = rows[latest_index] if latest_index is not None else None
    latest_facts = facts[-1] if latest_index is not None else None
    shares = latest_facts["remaining_shares"] if latest_facts is not None else None
    cost = latest_facts["avg_cost_after_trade"] if latest_facts is not None else None

    reasons: list[str] = []
    broken = sum(1 for item in facts if not item["complete"])
    if broken:
        reasons.append(f"{broken} 笔成交缺少必要字段")
    reasons.extend(_trade_order_reasons(matching))
    # 链条上任意一笔缺剩余股数或平均成本，就说不清持仓怎么变到今天的——
    # 只看「排最后那一行」会让中间被判为不可信而留空的成交被整笔跳过。
    incomplete_chain = any(
        item["remaining_shares"] is None or item["avg_cost_after_trade"] is None
        for item in facts
    )
    if order_status == ORDER_KNOWN:
        if shares is None:
            reasons.append("最后一笔没有记录成交后剩余股数")
        if cost is None:
            reasons.append("最后一笔没有记录成交后平均成本")

    chain = fee_chain if fee_chain is not None else _trade_fee_chain(rows)
    return {
        "has_history": True,
        "known": not incomplete_chain and order_status == ORDER_KNOWN,
        "shares": shares,
        "cost": cost,
        "fee_complete": (
            chain.get(latest_index, True)
            if latest_index is not None
            else all(chain.get(index, True) for index in indexes)
        ),
        "latest_timestamp": (
            _actual_trade_timestamp(latest) if latest is not None else None
        ),
        "latest_trade_date": latest_trade_date,
        "latest_time_known": bool(
            latest is not None and _normalize_trade_time(latest.get("trade_time"))
        ),
        "order_status": order_status,
        "reasons": list(dict.fromkeys(reasons)),
    }


def _trade_display_row(
    row: dict[str, str],
    *,
    fee_complete: bool = True,
) -> dict[str, Any]:
    source = row.get("source", "")
    request_id = (
        source.removeprefix("panel_manual:")
        if source.startswith("panel_manual:")
        else ""
    )
    def json_list(key: str) -> list[str]:
        try:
            value = json.loads(row.get(key, "") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return [str(item) for item in value] if isinstance(value, list) else []

    emotion_clear_raw = str(row.get("emotion_clear") or "").strip().lower()
    facts = _trade_ledger_facts(row, fee_complete=fee_complete)
    return {
        "request_id": request_id,
        "record_ref": str(row.get("record_ref") or "").strip(),
        "trade_date": row.get("trade_date"),
        "trade_time": row.get("trade_time"),
        "trade_time_known": bool(_normalize_trade_time(row.get("trade_time"))),
        "trade_time_label": _trade_time_label(row.get("trade_time")),
        "symbol": row.get("symbol"),
        "stock_name": row.get("stock_name") or row.get("symbol"),
        "side": row.get("side"),
        "price": facts["price"],
        "shares": facts["shares"],
        "gross_amount": _stored_number(row.get("gross_amount")),
        "remaining_shares": facts["remaining_shares"],
        "avg_cost_after_trade": facts["avg_cost_after_trade"],
        "realized_pnl": facts["realized_pnl"],
        "fee": facts["fee"],
        "fee_reported": facts["fee_reported"],
        "fee_complete": facts["fee_complete"],
        "fee_note": facts["fee_note"],
        "cost_fee_note": facts["cost_fee_note"],
        "realized_fee_note": facts["realized_fee_note"],
        "exact": facts["exact"],
        "operation": row.get("operation", ""),
        "operation_class": facts["operation_class"],
        "operation_label": facts["operation_label"],
        "complete": facts["complete"],
        "missing_fields": facts["missing_fields"],
        "incomplete_label": "" if facts["complete"] else INCOMPLETE_HISTORY_LABEL,
        "rule_status": row.get("rule_status", ""),
        "emotion": row.get("emotion", ""),
        "checklist_version": row.get("checklist_version", ""),
        "reason_tags": json_list("reason_tags_json"),
        "discipline_checks": json_list("discipline_checks_json"),
        "emotion_flags": json_list("emotion_flags_json"),
        "emotion_clear": (
            emotion_clear_raw in {"true", "1", "yes"}
            if emotion_clear_raw
            else None
        ),
        "notes": row.get("notes", ""),
    }


def _trade_choice_list(
    payload: dict[str, Any],
    key: str,
    allowed: dict[str, str],
    label: str,
) -> list[str]:
    raw = payload.get(key, [])
    if raw is None:
        return []
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise PersonalDataError(f"{label}格式不正确。")
    values = list(dict.fromkeys(item.strip().upper() for item in raw if item.strip()))
    unknown = [item for item in values if item not in allowed]
    if unknown:
        raise PersonalDataError(f"{label}包含不支持的选项。")
    return values


def _trade_checklist_summary(
    payload: dict[str, Any],
    *,
    side: str,
    extra_notes: str,
) -> tuple[str, str, str, dict[str, Any]]:
    reason_labels = TRADE_REASON_LABELS[side]
    reason_tags = _trade_choice_list(
        payload,
        "reason_tags",
        reason_labels,
        "操作依据",
    )
    discipline_checks = _trade_choice_list(
        payload,
        "discipline_checks",
        DISCIPLINE_CHECK_LABELS,
        "纪律核对",
    )
    emotion_flags = _trade_choice_list(
        payload,
        "emotion_flags",
        EMOTION_FLAG_LABELS,
        "情绪风险",
    )
    emotion_clear = payload.get("emotion_clear", False)
    if not isinstance(emotion_clear, bool):
        raise PersonalDataError("情绪确认格式不正确。")
    if emotion_clear and emotion_flags:
        raise PersonalDataError("“无明显情绪驱动”不能与情绪风险同时勾选。")

    if emotion_flags:
        rule_status = "情绪风险已记录"
        emotion = "、".join(EMOTION_FLAG_LABELS[item] for item in emotion_flags)
    elif (
        reason_tags
        and len(discipline_checks) == len(DISCIPLINE_CHECK_LABELS)
        and emotion_clear
    ):
        rule_status = "纪律核对完整"
        emotion = "当时无明显情绪驱动（本人勾选）"
    else:
        rule_status = "纪律核对不完整"
        emotion = (
            "当时无明显情绪驱动（本人勾选）"
            if emotion_clear
            else "情绪状态未确认"
        )

    reason_text = (
        "、".join(reason_labels[item] for item in reason_tags)
        if reason_tags
        else "未勾选明确依据"
    )
    missing = [
        label
        for key, label in DISCIPLINE_CHECK_LABELS.items()
        if key not in discipline_checks
    ]
    discipline_text = f"{len(discipline_checks)} / {len(DISCIPLINE_CHECK_LABELS)} 已确认"
    if missing:
        discipline_text += f"（未确认：{'、'.join(missing)}）"
    note_parts = [
        f"操作点：{reason_text}",
        f"纪律：{discipline_text}",
    ]
    if extra_notes:
        note_parts.append(f"补充：{extra_notes}")
    notes = "；".join(note_parts)
    if len(notes) > 500:
        raise PersonalDataError("勾选摘要和补充说明合计不能超过 500 字。")
    return rule_status, emotion, notes, {
        "checklist_version": str(payload.get("checklist_version") or "1")[:16],
        "reason_tags": reason_tags,
        "discipline_checks": discipline_checks,
        "emotion_flags": emotion_flags,
        "emotion_clear": emotion_clear,
    }


def _trade_symbol_summaries(
    rows: list[dict[str, str]],
    fee_chain: dict[int, bool] | None = None,
) -> list[dict[str, Any]]:
    """按股票复述可确认的台账汇总，不重算、不补齐、不用展示行序作证。"""
    chain = fee_chain if fee_chain is not None else _trade_fee_chain(rows)
    grouped: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            continue
        grouped.setdefault(symbol, []).append(index)

    summaries: list[dict[str, Any]] = []
    for symbol, indexes in grouped.items():
        matching = [rows[index] for index in indexes]
        order_status = _trade_order_status(matching)
        if order_status == ORDER_KNOWN:
            ordered_indexes = sorted(
                indexes, key=lambda index: _trade_business_sort_key(rows[index])
            )
        else:
            ordered_indexes = sorted(
                indexes, key=lambda index: _trade_display_sort_key(rows[index])
            )
        facts = [
            _trade_ledger_facts(rows[index], fee_complete=chain.get(index, True))
            for index in ordered_indexes
        ]
        latest_index = ordered_indexes[-1] if order_status == ORDER_KNOWN else None
        latest = rows[latest_index] if latest_index is not None else None
        latest_facts = facts[-1] if latest is not None else None
        representative = rows[
            sorted(indexes, key=lambda index: _trade_display_sort_key(rows[index]))[-1]
        ]

        field_reasons: list[str] = []
        broken = sum(1 for item in facts if not item["complete"])
        if broken:
            field_reasons.append(f"{broken} 笔成交缺少必要字段")

        order_reasons = _trade_order_reasons(matching)
        remaining = (
            latest_facts["remaining_shares"] if latest_facts is not None else None
        )
        average_cost = (
            latest_facts["avg_cost_after_trade"] if latest_facts is not None else None
        )
        if order_status == ORDER_KNOWN:
            if remaining is None:
                field_reasons.append("最后一笔没有记录成交后剩余股数")
            if average_cost is None:
                field_reasons.append("最后一笔没有记录成交后平均成本")
        else:
            remaining = None
            average_cost = None

        fee_complete = (
            latest_facts["fee_complete"]
            if latest_facts is not None
            else all(item["fee_complete"] for item in facts)
        )
        fee_reasons: list[str] = []
        # 没有数值可限定时不提费用：数字位已经是「—」，再说「不含未知费用」只会添乱。
        has_value = remaining is not None or average_cost is not None
        if not fee_complete and has_value:
            fee_reasons.append("成交账本平均成本与已实现盈亏不含未知费用")
        summary_complete = not field_reasons and order_status == ORDER_KNOWN
        valid_dates = [
            value
            for value in (
                _trade_date_value(row.get("trade_date")) for row in matching
            )
            if value is not None
        ]
        summaries.append(
            {
                "symbol": symbol,
                "stock_name": str(representative.get("stock_name") or "").strip() or symbol,
                "trade_count": len(indexes),
                "remaining_shares": remaining,
                "avg_cost": average_cost,
                "last_trade_date": (
                    str(latest.get("trade_date") or "").strip() or None
                    if latest is not None
                    else None
                ),
                "last_trade_time": (
                    str(latest.get("trade_time") or "").strip() or None
                    if latest is not None
                    else None
                ),
                "last_trade_time_label": (
                    _trade_time_label(latest.get("trade_time"))
                    if latest is not None
                    else None
                ),
                "last_operation_label": (
                    latest_facts["operation_label"]
                    if latest_facts is not None
                    else None
                ),
                "order_status": order_status,
                "order_status_label": ORDER_STATUS_LABELS[order_status],
                "order_reasons": order_reasons,
                "basis_record_ref": (
                    str(latest.get("record_ref") or "").strip()
                    if latest is not None
                    else None
                ),
                "complete": summary_complete,
                "incomplete_reasons": list(dict.fromkeys(field_reasons)),
                "incomplete_label": (
                    INCOMPLETE_HISTORY_LABEL if field_reasons else ""
                ),
                "fee_complete": fee_complete,
                "fee_reasons": fee_reasons,
                "fee_label": FEE_UNKNOWN_LABEL if fee_reasons else "",
                # 「精确完整」要求既不缺字段、也不含未知费用。
                "exact": summary_complete and fee_complete,
                "_sort_date": max(valid_dates) if valid_dates else date.min,
            }
        )
    # 只按最新可识别日期排股票卡片；不因此宣称某笔是最后成交。
    summaries.sort(key=lambda item: item["symbol"])
    summaries.sort(key=lambda item: item["_sort_date"], reverse=True)
    for item in summaries:
        item.pop("_sort_date", None)
    return summaries


def summarize_trade_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """让其他只读消费者复用操作记录的唯一账本与顺序口径。

    调用方常从 pandas DataFrame 传入带 NaN 的行。这里先复制并把缺失值恢复为
    台账使用的空字符串，再在内存中补兼容引用；不会写回成交文件。
    """
    normalized_rows: list[dict[str, str]] = []
    for source_row in rows:
        normalized: dict[str, str] = {}
        for key, value in source_row.items():
            try:
                missing = value is None or bool(pd.isna(value))
            except (TypeError, ValueError):
                missing = value is None
            normalized[str(key)] = "" if missing else str(value).strip()
        normalized_rows.append(normalized)
    _assign_trade_record_refs(normalized_rows)
    fee_chain = _trade_fee_chain(normalized_rows)
    return _trade_symbol_summaries(normalized_rows, fee_chain)


def list_trade_records(
    project_root: str | Path,
    *,
    symbol: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    root = Path(project_root)
    normalized_symbol = _normalize_symbol(symbol) if symbol else None
    safe_limit = max(1, min(int(limit), 200))
    rows = read_trade_rows(root / TARGET_FILES["trades"])
    if normalized_symbol:
        rows = [row for row in rows if row.get("symbol") == normalized_symbol]
    rows.sort(key=_trade_display_sort_key, reverse=True)
    fee_chain = _trade_fee_chain(rows)
    facts = [
        _trade_ledger_facts(row, fee_complete=fee_chain.get(index, True))
        for index, row in enumerate(rows)
    ]
    # 明细受 limit 限制，汇总必须覆盖全部成交，否则「还剩多少」会被截断。
    return {
        "records": [
            _trade_display_row(row, fee_complete=fee_chain.get(index, True))
            for index, row in enumerate(rows[:safe_limit])
        ],
        "count": len(rows),
        "symbol_summaries": _trade_symbol_summaries(rows, fee_chain),
        "incomplete_record_count": sum(1 for item in facts if not item["complete"]),
        "fee_unknown_record_count": sum(1 for item in facts if not item["fee_complete"]),
    }


def append_trade_record(
    project_root: str | Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    request_id = str(payload.get("request_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", request_id):
        raise PersonalDataError("操作编号无效，请刷新页面后重试。")

    raw_date = str(payload.get("trade_date") or "").strip()
    try:
        trade_date = date.fromisoformat(raw_date)
    except ValueError:
        raise PersonalDataError("操作日期格式不正确。") from None
    if trade_date > date.today():
        raise PersonalDataError("操作日期不能晚于今天。")

    # 成交时间未知就留空，绝不用提交时间或创建时间冒充。
    raw_time = str(payload.get("trade_time") or "").strip()
    trade_time = _normalize_trade_time(raw_time)
    if raw_time and not trade_time:
        raise PersonalDataError("操作时间格式不正确。")

    symbol = _normalize_symbol(payload.get("symbol"))
    side = _normalize_side(payload.get("side"))
    price = float(_trade_number(payload.get("price"), "成交价格"))
    shares = int(_trade_number(payload.get("shares"), "成交数量", integer=True))
    # 本笔费用合计：留空表示未知，明确填 0 才表示确实没有费用。两者绝不可混为一谈。
    fee = _reported_trade_number(payload.get("fee"), "本笔费用合计")
    notes = str(payload.get("notes") or "").strip()
    if len(notes) > 500:
        raise PersonalDataError("备注不能超过 500 字。")
    stock_name = str(payload.get("stock_name") or symbol).strip()[:80] or symbol
    source = f"panel_manual:{request_id}"
    timestamp = (
        datetime.fromisoformat(f"{trade_date.isoformat()} {trade_time}")
        if trade_time
        else None
    )

    root = Path(project_root)
    target = root / TARGET_FILES["trades"]
    target.parent.mkdir(parents=True, exist_ok=True)
    with _TRADE_WRITE_LOCK:
        rows = _trade_rows(target)
        # 兼容旧表头：先在内存中补身份。只有本次确实追加时，
        # 才随后面带备份的整表写入一起持久化。
        _assign_trade_record_refs(rows)
        duplicate = next((row for row in rows if row.get("source") == source), None)
        if duplicate is not None:
            duplicate_chain = _trade_fee_chain(rows)
            duplicate_index = rows.index(duplicate)
            return {
                "record": _trade_display_row(
                    duplicate,
                    fee_complete=duplicate_chain.get(duplicate_index, True),
                ),
                "deduplicated": True,
                "count": len(rows),
            }

        # 手工记录优先直接复用 request_id。只有券商原编号恰好同名时，
        # 才分配一个可持久化的本地后备引用；request_id 的幂等判定仍只看 source。
        used_record_refs = {
            str(existing.get("record_ref") or "").strip()
            for existing in rows
            if str(existing.get("record_ref") or "").strip()
        }
        record_ref = request_id
        if record_ref in used_record_refs:
            base_ref = f"manual-{request_id}"
            record_ref = base_ref
            suffix = 2
            while record_ref in used_record_refs:
                record_ref = f"{base_ref}-{suffix}"
                suffix += 1

        state = _recorded_position_state(rows, symbol)
        latest_timestamp = state["latest_timestamp"]
        latest_trade_date = state["latest_trade_date"]
        # 顺序校验只在能确定新成交更早时才拦。任一侧时间未知时只比日期——
        # 不能拿展示顺序去否决一笔已经发生的成交。
        if latest_trade_date is not None:
            if (
                state["order_status"] == ORDER_KNOWN
                and state["latest_time_known"]
                and trade_time
            ):
                too_early = timestamp < latest_timestamp
            else:
                too_early = trade_date < latest_trade_date
            if too_early:
                raise PersonalDataError("只能追加该股票最新的一次操作，请先核对日期和时间。")

        # 顺序判定与读取侧共用 _order_is_ambiguous，口径不得分叉。
        same_date_rows = [
            row
            for row in rows
            if row.get("symbol") == symbol
            and str(row.get("trade_date") or "").strip() == trade_date.isoformat()
        ]
        pending_row = {
            "trade_date": trade_date.isoformat(),
            "trade_time": trade_time,
            "record_ref": record_ref,
            "source": source,
        }
        order_ambiguous = _order_is_ambiguous([*same_date_rows, pending_row])
        previous_known = state["known"] and not order_ambiguous

        # 只有前序确实已知时才校验数量：未知时不得用一个从未被记录的数字
        # 否决一笔已经发生的成交（需求 3）。
        if side == "SELL" and previous_known:
            if state["shares"] <= 0:
                raise PersonalDataError("没有可核对的历史持仓，暂不能录入卖出。")
            if shares > state["shares"]:
                raise PersonalDataError(
                    f"卖出数量超过记录中的持仓 {state['shares']} 股，请先核对历史记录。"
                )

        has_checklist = any(
            key in payload
            for key in (
                "reason_tags",
                "discipline_checks",
                "emotion_flags",
                "emotion_clear",
                "checklist_version",
            )
        )
        if has_checklist:
            rule_status, emotion, notes, checklist = _trade_checklist_summary(
                payload,
                side=side,
                extra_notes=notes,
            )
        else:
            rule_status = "历史未审查"
            emotion = "未记录"
            checklist = None

        gross_amount = round(price * shares, 2)
        fee_reported = fee is not None
        # 费用未知时按不含费用计算，并由 _trade_fee_chain 给该股票之后的每一笔打上
        # 「不含未知费用」标记；绝不把未知费用当成 0 而声称数值精确。
        fee_amount = fee if fee_reported else 0.0
        remaining_shares: int | None
        average_cost: float | None
        realized_pnl: float | None
        net_amount: float | None
        if side == "BUY":
            # 买入不产生已实现盈亏，这与前序状态和费用都无关，是本笔自身的事实。
            realized_pnl = 0.0
            if previous_known:
                remaining_shares = state["shares"] + shares
                average_cost = (
                    state["shares"] * state["cost"] + gross_amount + fee_amount
                ) / remaining_shares
            else:
                remaining_shares = None
                average_cost = None
            net_amount = -(gross_amount + fee_amount) if fee_reported else None
        else:
            if previous_known:
                remaining_shares = state["shares"] - shares
                average_cost = state["cost"] if remaining_shares else 0.0
                realized_pnl = (price - state["cost"]) * shares - fee_amount
            else:
                remaining_shares = None
                average_cost = None
                realized_pnl = None
            net_amount = gross_amount - fee_amount if fee_reported else None

        # 四类归类同样只在前序已知时给出，否则「首次买入」就是对前序的无据断言。
        if not previous_known:
            operation = ""
        elif side == "BUY":
            operation = "首次买入" if state["shares"] <= 0 else "加仓"
        else:
            operation = "清仓" if shares == state["shares"] else "减仓"

        row = {column: "" for column in TRADE_COLUMNS}
        row.update(
            {
                "trade_date": trade_date.isoformat(),
                "trade_time": trade_time,
                "symbol": symbol,
                "stock_name": stock_name,
                "market": symbol.rsplit(".", 1)[-1],
                "side": side,
                "operation": operation,
                "price": f"{price:.4f}".rstrip("0").rstrip("."),
                "shares": str(shares),
                "gross_amount": f"{gross_amount:.2f}",
                # 费用未知留空，明确 0 才写 0.00。三项明细手工表单从未采集过，
                # 一律留空表示「未单独记录」，不再硬写 0.00 冒充事实。
                # 用 4 位小数再去尾零：不足一分的自报费用不能被写成 "0.00"，
                # 那会把「有一点费用」说成「明确没有费用」。
                "fee": (
                    f"{fee_amount:.4f}".rstrip("0").rstrip(".") or "0"
                    if fee_reported
                    else ""
                ),
                "stamp_tax": "",
                "transfer_fee": "",
                "other_fee": "",
                "net_amount": "" if net_amount is None else f"{net_amount:.2f}",
                "avg_cost_after_trade": (
                    "" if average_cost is None else f"{average_cost:.4f}"
                ),
                "realized_pnl": "" if realized_pnl is None else f"{realized_pnl:.2f}",
                "remaining_shares": (
                    "" if remaining_shares is None else str(remaining_shares)
                ),
                "position_market_value": (
                    "" if remaining_shares is None else f"{remaining_shares * price:.2f}"
                ),
                "rule_status": rule_status,
                "emotion": emotion,
                "checklist_version": (
                    checklist["checklist_version"] if checklist else ""
                ),
                "reason_tags_json": (
                    json.dumps(checklist["reason_tags"], ensure_ascii=False)
                    if checklist
                    else ""
                ),
                "discipline_checks_json": (
                    json.dumps(checklist["discipline_checks"], ensure_ascii=False)
                    if checklist
                    else ""
                ),
                "emotion_flags_json": (
                    json.dumps(checklist["emotion_flags"], ensure_ascii=False)
                    if checklist
                    else ""
                ),
                "emotion_clear": (
                    "true" if checklist and checklist["emotion_clear"] else (
                        "false" if checklist else ""
                    )
                ),
                "record_ref": record_ref,
                "source": source,
                "notes": notes,
            }
        )
        rows.append(row)
        rows.sort(key=_trade_display_sort_key)

        if target.exists() and target.stat().st_size > len(",".join(TRADE_COLUMNS)):
            backup_dir = root / "history" / "trade_records"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / (
                f"trades_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.csv"
            )
            shutil.copy2(target, backup_path)

        temporary = target.with_suffix(".csv.tmp")
        try:
            with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=TRADE_COLUMNS)
                writer.writeheader()
                writer.writerows(rows)
            temporary.replace(target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    # 回执里的完整性标记也必须走同一条链条判定：本笔费用已知，
    # 但该股票早先某一笔费用未知时，这一笔的成本同样不含它。
    written_chain = _trade_fee_chain(rows)
    return {
        "record": _trade_display_row(
            row,
            fee_complete=written_chain.get(rows.index(row), True),
        ),
        "deduplicated": False,
        "count": len(rows),
    }


def _normalize_trades(
    frame: pd.DataFrame,
    filename: str,
    *,
    assign_local_refs: bool = False,
) -> tuple[pd.DataFrame, list[str]]:
    mapping = _column_map(frame)
    required = {
        "symbol": "证券代码/股票代码",
        "trade_date": "成交日期/交易日期",
        "side": "买卖方向",
        "price": "成交价格",
        "shares": "成交数量",
    }
    missing = [label for key, label in required.items() if key not in mapping]
    if missing:
        raise PersonalDataError(f"成交 CSV 缺少字段：{'、'.join(missing)}。")

    warnings: list[str] = []
    output = pd.DataFrame(index=frame.index)
    output["trade_date"] = _normalize_dates(_series(frame, mapping, "trade_date"), "成交日期")
    # 券商导出没有成交时间时留空，按「未记录」保存，不拿收盘时间冒充。
    output["trade_time"] = _series(frame, mapping, "trade_time").map(_normalize_trade_time)
    unknown_times = int((output["trade_time"] == "").sum())
    if unknown_times:
        warnings.append(f"{unknown_times} 笔成交没有可识别的成交时间，已按「未记录」保存。")
    output["symbol"] = _series(frame, mapping, "symbol").map(_normalize_symbol)
    output["stock_name"] = _clean_text(_series(frame, mapping, "stock_name"))
    output.loc[output["stock_name"] == "", "stock_name"] = output["symbol"]
    output["market"] = output["symbol"].str.rsplit(".", n=1).str[-1]
    output["side"] = _series(frame, mapping, "side").map(_normalize_side)
    output["operation"] = _clean_text(_series(frame, mapping, "operation"))
    output.loc[output["operation"] == "", "operation"] = output["side"].map(
        {"BUY": "证券买入", "SELL": "证券卖出"}
    )
    output["price"] = _numbers(_series(frame, mapping, "price"))
    output["shares"] = _numbers(_series(frame, mapping, "shares"))
    if output[["price", "shares"]].isna().any().any():
        raise PersonalDataError("成交价格和成交数量必须是有效数字。")
    if (output["price"] <= 0).any() or (output["shares"] <= 0).any():
        raise PersonalDataError("成交价格和成交数量必须大于 0。")
    output["shares"] = output["shares"].round().astype(int)
    output["gross_amount"] = _numbers(_series(frame, mapping, "gross_amount"))
    output["gross_amount"] = output["gross_amount"].fillna(output["price"] * output["shares"])
    # 费用缺列或空格一律保持未知，绝不当作 0：A 股卖出必然有印花税，
    # 把缺失写成 0 等于断言「本笔免费」，并让净额与成本继承这个零。
    for key in ["fee", "stamp_tax", "transfer_fee", "other_fee"]:
        output[key] = _numbers(_series(frame, mapping, key))
    # min_count 覆盖全部四项：默认的 skipna 会把未知当 0 求和，那是同一个伪造换了位置。
    total_fee = output[["fee", "stamp_tax", "transfer_fee", "other_fee"]].sum(
        axis=1, min_count=4
    )
    output["net_amount"] = _numbers(_series(frame, mapping, "net_amount"))
    derived_net = output["gross_amount"].where(output["side"] == "SELL", -output["gross_amount"])
    derived_net = derived_net.where(output["side"] == "BUY", derived_net - total_fee)
    derived_net = derived_net.where(output["side"] == "SELL", derived_net - total_fee)
    output["net_amount"] = output["net_amount"].fillna(derived_net)
    # 以下三列是台账的核心事实。源文件没给就必须保持为空：
    # 它们的正确性依赖「导入区间覆盖了该股票从第一笔买入开始的完整历史」，
    # 而导入方无从确认这一点。系统测算值一律不得反写进这三列（需求 1、2）。
    output["avg_cost_after_trade"] = _numbers(_series(frame, mapping, "avg_cost_after_trade"))
    output["realized_pnl"] = _numbers(_series(frame, mapping, "realized_pnl"))
    output["remaining_shares"] = _numbers(_series(frame, mapping, "remaining_shares"))
    output["account_total_asset"] = _numbers(_series(frame, mapping, "account_total_asset"))
    output["position_market_value"] = _numbers(_series(frame, mapping, "position_market_value"))
    output["position_pct"] = _numbers(_series(frame, mapping, "position_pct"), percentage=True)
    output["rule_status"] = _clean_text(_series(frame, mapping, "rule_status"))
    output["emotion"] = _clean_text(_series(frame, mapping, "emotion"))
    for column in (
        "checklist_version",
        "reason_tags_json",
        "discipline_checks_json",
        "emotion_flags_json",
        "emotion_clear",
    ):
        output[column] = ""
    source_refs = _clean_text(_series(frame, mapping, "record_ref"))
    provided_refs = source_refs[source_refs != ""]
    if provided_refs.duplicated().any():
        raise PersonalDataError("源文件的成交编号不能重复。")
    output["record_ref"] = source_refs.replace("", pd.NA)
    missing_refs = output["record_ref"].isna()
    if assign_local_refs and missing_refs.any():
        used = set(provided_refs.tolist())
        generated: list[str] = []
        for _ in range(int(missing_refs.sum())):
            candidate = f"import-{uuid4().hex}"
            while candidate in used:
                candidate = f"import-{uuid4().hex}"
            used.add(candidate)
            generated.append(candidate)
        output.loc[missing_refs, "record_ref"] = generated
        warnings.append(
            f"{len(generated)} 笔成交没有源成交编号，已在导入时分配并持久化本地记录引用。"
        )
    elif missing_refs.any():
        warnings.append(
            f"{int(missing_refs.sum())} 笔成交没有源成交编号，提交导入时将分配本地记录引用。"
        )
    output["source"] = _clean_text(_series(frame, mapping, "source"))
    output.loc[output["source"] == "", "source"] = "panel_import"
    existing_notes = _clean_text(_series(frame, mapping, "notes"))
    import_note = f"数据中心导入：{filename}"
    output["notes"] = existing_notes.map(lambda value: f"{value}；{import_note}" if value else import_note)

    # 这仅是输出文件的稳定展示顺序，不构成同日成交先后证据。
    output = (
        output.assign(
            _time_unknown=(output["trade_time"] == "").astype(int),
            _record_ref_sort=output["record_ref"].fillna("").astype(str),
        )
        .sort_values(
            ["trade_date", "_time_unknown", "trade_time", "symbol", "_record_ref_sort"],
            kind="stable",
        )
        .drop(columns=["_time_unknown", "_record_ref_sort"])
        .reset_index(drop=True)
    )

    # 区间完整性只做观察和提示，不写任何数值：某只股票在本次区间里卖出多于买入，
    # 说明区间起点晚于第一笔买入，用户需要重新导出。
    for symbol, group in output.groupby("symbol", sort=True):
        bought = int(group.loc[group["side"] == "BUY", "shares"].sum())
        sold = int(group.loc[group["side"] == "SELL", "shares"].sum())
        if sold > bought:
            warnings.append(
                f"{symbol} 在本次导入区间里卖出数量高于买入数量，导出区间可能不完整，请核对。"
            )

    if "gross_amount" not in mapping:
        warnings.append("未找到成交金额，已按成交价格 × 成交数量计算。")
    if "account_total_asset" not in mapping:
        warnings.append("未找到账户总资产，单股审查时需要填写。")
    for key, label in (
        ("remaining_shares", "成交后剩余股数"),
        ("avg_cost_after_trade", "成交后平均成本"),
        ("realized_pnl", "该笔已实现盈亏"),
    ):
        blank = int(output[key].isna().sum())
        if blank:
            warnings.append(
                f"{blank} 笔成交没有{label}，已保持为空并标记「{INCOMPLETE_HISTORY_LABEL}」，不做推算。"
            )
    # 源文件给了负数时如实保留原值并报警，不静默夹到 0——读取侧会把它判为缺字段。
    for key, label in (
        ("remaining_shares", "成交后剩余股数"),
        ("avg_cost_after_trade", "成交后平均成本"),
    ):
        negative = int((output[key] < 0).sum())
        if negative:
            warnings.append(
                f"{negative} 笔成交的{label}是负数，已原样保留并标记「{INCOMPLETE_HISTORY_LABEL}」，请核对导出文件。"
            )
    unknown_fee = int(total_fee.isna().sum())
    if unknown_fee:
        warnings.append(
            f"{unknown_fee} 笔成交的费用不完整，已保持为空（{FEE_UNKNOWN_LABEL}），不按 0 处理。"
        )
    # 可空整型：未知落盘为空字段。绝不 fillna(0)——「不知道还剩多少股」不是「剩 0 股」，
    # 而 0 会在读侧被判成已记录事实，卖出行还会被标成「清仓」。
    output["remaining_shares"] = output["remaining_shares"].round().astype("Int64")
    for column in ["price", "avg_cost_after_trade"]:
        output[column] = output[column].round(4)
    for column in [
        "gross_amount",
        "fee",
        "stamp_tax",
        "transfer_fee",
        "other_fee",
        "net_amount",
        "realized_pnl",
        "account_total_asset",
        "position_market_value",
    ]:
        output[column] = output[column].round(2)
    output["position_pct"] = output["position_pct"].round(6)
    output = output[TRADE_COLUMNS]
    return output, list(dict.fromkeys(warnings))


def normalize_personal_data(
    payload: dict[str, Any],
    *,
    assign_trade_refs: bool = False,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    kind = str(payload.get("kind") or "").strip().lower()
    if kind not in SUPPORTED_KINDS:
        raise PersonalDataError("个人数据类型只支持 positions 或 trades。")
    frame, filename, encoding, warnings = _parse_frame(payload)
    if kind == "positions":
        normalized, normalize_warnings = _normalize_positions(frame, filename)
        date_column = "snapshot_date"
    else:
        normalized, normalize_warnings = _normalize_trades(
            frame,
            filename,
            assign_local_refs=assign_trade_refs,
        )
        date_column = "trade_date"
    warnings.extend(normalize_warnings)
    summary = {
        "kind": kind,
        "filename": filename,
        "encoding": encoding,
        "rows": int(len(normalized)),
        "symbols": int(normalized["symbol"].nunique()),
        "date_start": str(normalized[date_column].min()),
        "date_end": str(normalized[date_column].max()),
        "recognized_columns": [str(column) for column in frame.columns],
        "output_columns": list(normalized.columns),
        "warnings": list(dict.fromkeys(warnings)),
        "preview": normalized.head(5).astype(object).where(pd.notna(normalized.head(5)), None).to_dict("records"),
    }
    return normalized, summary


def import_personal_data(project_root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    mode = str(payload.get("mode") or "preview").strip().lower()
    if mode not in {"preview", "commit"}:
        raise PersonalDataError("导入模式只支持 preview 或 commit。")
    normalized, summary = normalize_personal_data(
        payload,
        assign_trade_refs=mode == "commit",
    )
    if mode == "preview":
        return {**summary, "mode": "preview", "written": False, "backup_path": None}

    root = Path(project_root)
    target = root / TARGET_FILES[summary["kind"]]
    target.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    if target.exists():
        timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
        backup_dir = root / "history" / "imports"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"{summary['kind']}_{timestamp}.csv"
        shutil.copy2(target, backup_path)

    temporary = target.with_suffix(".csv.tmp")
    try:
        normalized.to_csv(temporary, index=False, encoding="utf-8-sig")
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {
        **summary,
        "mode": "commit",
        "written": True,
        "target_path": str(target.relative_to(root)),
        "backup_path": None if backup_path is None else str(backup_path.relative_to(root)),
    }


def _file_status(path: Path, kind: str) -> dict[str, Any]:
    date_column = "snapshot_date" if kind == "positions" else "trade_date"
    if not path.exists():
        return {
            "kind": kind,
            "state": "MISSING",
            "rows": 0,
            "symbols": 0,
            "latest_date": None,
            "is_demo": False,
            "demo_rows": 0,
            "message": "尚未导入",
        }
    try:
        frame = pd.read_csv(path)
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        return {
            "kind": kind,
            "state": "INVALID",
            "rows": 0,
            "symbols": 0,
            "latest_date": None,
            "is_demo": False,
            "demo_rows": 0,
            "message": f"文件无法读取：{exc}",
        }
    if frame.empty:
        return {
            "kind": kind,
            "state": "EMPTY",
            "rows": 0,
            "symbols": 0,
            "latest_date": None,
            "is_demo": False,
            "demo_rows": 0,
            "message": "文件没有数据",
        }
    notes = frame.get("notes", pd.Series([""] * len(frame))).fillna("").astype(str).str.lower()
    sources = frame.get("source", pd.Series([""] * len(frame))).fillna("").astype(str).str.lower()
    demo_mask = notes.str.contains("demo|示例", regex=True) | sources.eq("demo")
    demo_rows = int(demo_mask.sum())
    is_demo = demo_rows == len(frame)
    state = "DEMO" if is_demo else "PARTIAL_DEMO" if demo_rows else "READY"
    latest_date = None
    if date_column in frame.columns:
        parsed_dates = pd.to_datetime(frame[date_column], errors="coerce").dropna()
        if not parsed_dates.empty:
            latest_date = str(parsed_dates.max().date())
    symbols = int(frame["symbol"].astype(str).nunique()) if "symbol" in frame.columns else 0
    message = {
        "DEMO": "当前全部为示例数据",
        "PARTIAL_DEMO": "同时包含示例和导入数据",
        "READY": "已导入个人数据",
    }[state]
    return {
        "kind": kind,
        "state": state,
        "rows": int(len(frame)),
        "symbols": symbols,
        "latest_date": latest_date,
        "is_demo": is_demo,
        "demo_rows": demo_rows,
        "message": message,
        "path": str(path),
    }


def personal_data_status(project_root: str | Path) -> dict[str, Any]:
    root = Path(project_root)
    positions = _file_status(root / TARGET_FILES["positions"], "positions")
    trades = _file_status(root / TARGET_FILES["trades"], "trades")
    ready = positions["state"] == "READY" and trades["state"] == "READY"
    return {
        "local_only": True,
        "ready": ready,
        "has_demo_data": positions["demo_rows"] > 0 or trades["demo_rows"] > 0,
        "positions": positions,
        "trades": trades,
        "templates": {
            "positions": POSITION_COLUMNS,
            "trades": TRADE_COLUMNS,
        },
    }
