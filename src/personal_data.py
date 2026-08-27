from __future__ import annotations

import base64
import csv
from datetime import date, datetime
from io import StringIO
import json
import math
from pathlib import Path
import re
import shutil
import threading
from typing import Any

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
    "source",
    "notes",
]

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


def _optional_trade_number(value: Any, label: str) -> float:
    if value in (None, ""):
        return 0.0
    try:
        parsed = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        raise PersonalDataError(f"{label}必须是有效数字。") from None
    if not math.isfinite(parsed) or parsed < 0:
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


def _trade_timestamp(row: dict[str, str]) -> datetime | None:
    raw = f"{row.get('trade_date', '')} {row.get('trade_time', '') or '00:00:00'}"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _latest_trade_balance(
    rows: list[dict[str, str]],
    symbol: str,
) -> tuple[int, float, datetime | None]:
    shares_total = 0
    average_cost = 0.0
    latest_timestamp: datetime | None = None
    matching = [row for row in rows if row.get("symbol") == symbol]
    matching.sort(key=lambda row: _trade_timestamp(row) or datetime.min)
    for row in matching:
        timestamp = _trade_timestamp(row)
        if timestamp is not None:
            latest_timestamp = timestamp
        remaining_raw = row.get("remaining_shares", "")
        average_raw = row.get("avg_cost_after_trade", "")
        if remaining_raw not in ("", None):
            try:
                shares_total = max(0, int(round(float(remaining_raw))))
            except ValueError:
                pass
        else:
            try:
                quantity = max(0, int(round(float(row.get("shares") or 0))))
            except ValueError:
                quantity = 0
            if row.get("side") == "BUY":
                shares_total += quantity
            elif row.get("side") == "SELL":
                shares_total = max(0, shares_total - quantity)
        if average_raw not in ("", None):
            try:
                average_cost = max(0.0, float(average_raw))
            except ValueError:
                pass
        elif shares_total == 0:
            average_cost = 0.0
    return shares_total, average_cost, latest_timestamp


def _trade_display_row(row: dict[str, str]) -> dict[str, Any]:
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
    return {
        "request_id": request_id,
        "trade_date": row.get("trade_date"),
        "trade_time": row.get("trade_time"),
        "symbol": row.get("symbol"),
        "stock_name": row.get("stock_name") or row.get("symbol"),
        "side": row.get("side"),
        "price": float(row["price"]) if row.get("price") else None,
        "shares": int(round(float(row["shares"]))) if row.get("shares") else None,
        "gross_amount": (
            float(row["gross_amount"]) if row.get("gross_amount") else None
        ),
        "operation": row.get("operation", ""),
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


def list_trade_records(
    project_root: str | Path,
    *,
    symbol: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    root = Path(project_root)
    normalized_symbol = _normalize_symbol(symbol) if symbol else None
    safe_limit = max(1, min(int(limit), 200))
    rows = _trade_rows(root / TARGET_FILES["trades"])
    if normalized_symbol:
        rows = [row for row in rows if row.get("symbol") == normalized_symbol]
    rows.sort(key=lambda row: _trade_timestamp(row) or datetime.min, reverse=True)
    return {
        "records": [_trade_display_row(row) for row in rows[:safe_limit]],
        "count": len(rows),
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

    raw_time = str(payload.get("trade_time") or "").strip()
    if raw_time:
        try:
            trade_time = datetime.strptime(raw_time, "%H:%M").strftime("%H:%M:%S")
        except ValueError:
            try:
                trade_time = datetime.strptime(raw_time, "%H:%M:%S").strftime(
                    "%H:%M:%S"
                )
            except ValueError:
                raise PersonalDataError("操作时间格式不正确。") from None
    else:
        trade_time = datetime.now().strftime("%H:%M:%S")

    symbol = _normalize_symbol(payload.get("symbol"))
    side = _normalize_side(payload.get("side"))
    price = float(_trade_number(payload.get("price"), "成交价格"))
    shares = int(_trade_number(payload.get("shares"), "成交数量", integer=True))
    fee = _optional_trade_number(payload.get("fee"), "手续费")
    notes = str(payload.get("notes") or "").strip()
    if len(notes) > 500:
        raise PersonalDataError("备注不能超过 500 字。")
    stock_name = str(payload.get("stock_name") or symbol).strip()[:80] or symbol
    source = f"panel_manual:{request_id}"
    timestamp = datetime.combine(trade_date, datetime.strptime(trade_time, "%H:%M:%S").time())

    root = Path(project_root)
    target = root / TARGET_FILES["trades"]
    target.parent.mkdir(parents=True, exist_ok=True)
    with _TRADE_WRITE_LOCK:
        rows = _trade_rows(target)
        duplicate = next((row for row in rows if row.get("source") == source), None)
        if duplicate is not None:
            return {
                "record": _trade_display_row(duplicate),
                "deduplicated": True,
                "count": len(rows),
            }

        previous_shares, previous_cost, latest_timestamp = _latest_trade_balance(
            rows,
            symbol,
        )
        if latest_timestamp is not None and timestamp < latest_timestamp:
            raise PersonalDataError("只能追加该股票最新的一次操作，请先核对日期和时间。")
        if side == "SELL" and previous_shares <= 0:
            raise PersonalDataError("没有可核对的历史持仓，暂不能录入卖出。")
        if side == "SELL" and shares > previous_shares:
            raise PersonalDataError(
                f"卖出数量超过记录中的持仓 {previous_shares} 股，请先核对历史记录。"
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
        if side == "BUY":
            remaining_shares = previous_shares + shares
            average_cost = (
                previous_shares * previous_cost + gross_amount + fee
            ) / remaining_shares
            realized_pnl = 0.0
            net_amount = -(gross_amount + fee)
        else:
            remaining_shares = previous_shares - shares
            average_cost = previous_cost if remaining_shares else 0.0
            realized_pnl = (price - previous_cost) * shares - fee
            net_amount = gross_amount - fee

        if side == "BUY":
            operation = "首次买入" if previous_shares <= 0 else "加仓"
        else:
            operation = "清仓" if shares == previous_shares else "减仓"

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
                "fee": f"{fee:.2f}",
                "stamp_tax": "0.00",
                "transfer_fee": "0.00",
                "other_fee": "0.00",
                "net_amount": f"{net_amount:.2f}",
                "avg_cost_after_trade": f"{average_cost:.4f}",
                "realized_pnl": f"{realized_pnl:.2f}",
                "remaining_shares": str(remaining_shares),
                "position_market_value": f"{remaining_shares * price:.2f}",
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
                "source": source,
                "notes": notes,
            }
        )
        rows.append(row)
        rows.sort(key=lambda item: _trade_timestamp(item) or datetime.min)

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

    return {
        "record": _trade_display_row(row),
        "deduplicated": False,
        "count": len(rows),
    }


def _normalize_trades(frame: pd.DataFrame, filename: str) -> tuple[pd.DataFrame, list[str]]:
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
    output["trade_time"] = _clean_text(_series(frame, mapping, "trade_time", "15:00:00"))
    output.loc[output["trade_time"] == "", "trade_time"] = "15:00:00"
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
    for key in ["fee", "stamp_tax", "transfer_fee", "other_fee"]:
        output[key] = _numbers(_series(frame, mapping, key)).fillna(0.0)
    total_fee = output[["fee", "stamp_tax", "transfer_fee", "other_fee"]].sum(axis=1)
    output["net_amount"] = _numbers(_series(frame, mapping, "net_amount"))
    derived_net = output["gross_amount"].where(output["side"] == "SELL", -output["gross_amount"])
    derived_net = derived_net.where(output["side"] == "BUY", derived_net - total_fee)
    derived_net = derived_net.where(output["side"] == "SELL", derived_net - total_fee)
    output["net_amount"] = output["net_amount"].fillna(derived_net)
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
    output["source"] = _clean_text(_series(frame, mapping, "source"))
    output.loc[output["source"] == "", "source"] = "panel_import"
    existing_notes = _clean_text(_series(frame, mapping, "notes"))
    import_note = f"数据中心导入：{filename}"
    output["notes"] = existing_notes.map(lambda value: f"{value}；{import_note}" if value else import_note)

    output = output.sort_values(["trade_date", "trade_time", "symbol"]).reset_index(drop=True)
    holdings: dict[str, tuple[int, float]] = {}
    for index, row in output.iterrows():
        symbol = str(row["symbol"])
        previous_shares, previous_cost = holdings.get(symbol, (0, 0.0))
        shares = int(row["shares"])
        price = float(row["price"])
        fees = float(row["fee"] + row["stamp_tax"] + row["transfer_fee"] + row["other_fee"])
        if row["side"] == "BUY":
            calculated_shares = previous_shares + shares
            calculated_cost = (
                (previous_shares * previous_cost + float(row["gross_amount"]) + fees) / calculated_shares
                if calculated_shares > 0
                else 0.0
            )
            calculated_realized = 0.0
        else:
            if shares > previous_shares and previous_shares > 0:
                warnings.append(f"{symbol} 存在卖出数量高于已导入买入数量的记录，请核对导出区间。")
            calculated_shares = max(0, previous_shares - shares)
            calculated_cost = previous_cost if calculated_shares else 0.0
            calculated_realized = (price - previous_cost) * shares - fees if previous_shares else 0.0

        remaining = row["remaining_shares"]
        average = row["avg_cost_after_trade"]
        realized = row["realized_pnl"]
        remaining_value = calculated_shares if pd.isna(remaining) else max(0, int(round(float(remaining))))
        average_value = calculated_cost if pd.isna(average) else max(0.0, float(average))
        realized_value = calculated_realized if pd.isna(realized) else float(realized)
        output.at[index, "remaining_shares"] = remaining_value
        output.at[index, "avg_cost_after_trade"] = average_value
        output.at[index, "realized_pnl"] = realized_value
        if pd.isna(row["position_market_value"]):
            output.at[index, "position_market_value"] = remaining_value * price
        if pd.isna(row["position_pct"]) and pd.notna(row["account_total_asset"]) and float(row["account_total_asset"]) > 0:
            output.at[index, "position_pct"] = float(output.at[index, "position_market_value"]) / float(row["account_total_asset"])
        holdings[symbol] = (remaining_value, average_value)

    if "gross_amount" not in mapping:
        warnings.append("未找到成交金额，已按成交价格 × 成交数量计算。")
    if "account_total_asset" not in mapping:
        warnings.append("未找到账户总资产，单股审查时需要填写。")
    output["remaining_shares"] = output["remaining_shares"].fillna(0).round().astype(int)
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


def normalize_personal_data(payload: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    kind = str(payload.get("kind") or "").strip().lower()
    if kind not in SUPPORTED_KINDS:
        raise PersonalDataError("个人数据类型只支持 positions 或 trades。")
    frame, filename, encoding, warnings = _parse_frame(payload)
    if kind == "positions":
        normalized, normalize_warnings = _normalize_positions(frame, filename)
        date_column = "snapshot_date"
    else:
        normalized, normalize_warnings = _normalize_trades(frame, filename)
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
    normalized, summary = normalize_personal_data(payload)
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
