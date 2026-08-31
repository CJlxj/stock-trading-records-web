from __future__ import annotations

from datetime import date
from io import StringIO
import math
from pathlib import Path
from typing import Any

import pandas as pd

from src.indicators import add_indicators
from src.fundamental_data import review_fundamental_payload
from src.io_loader import (
    REQUIRED_COLUMNS,
    find_latest_csv,
    load_ohlcv,
    normalize_ohlcv,
    validate_symbol,
    validate_timeframe,
)
from src.position_sizer import classify_position, suggest_next_trade_capacity
from src.personal_data import personal_data_status
from src.rules.registry import RuleRegistry
from src.selection_engine import evaluate_selection
from src.signal_engine import evaluate_latest, load_rules
from src.stock_library import local_stock_name_map
from src.trade_analyzer import load_trades, summarize_symbol
from src.target_review import evaluate_target_plan


CONDITION_LABELS = {
    "close_above_ma20": "收盘价站上 MA20",
    "ma5_above_ma10": "MA5 不低于 MA10",
    "close_above_ma60": "收盘价站上 MA60",
    "ma20_slope_positive": "MA20 斜率向上",
    "ma20_above_ma60": "MA20 位于 MA60 上方",
    "macd_hist_positive": "MACD 柱为正",
    "macd_hist_improving": "MACD 柱继续改善",
    "rsi_healthy": "RSI 位于 45-70",
    "rsi_improving": "RSI 较前一日改善",
    "roc20_positive": "20 日价格动量为正",
    "volume_ratio_above_1_3": "成交量不低于 20 日均量的 1.3 倍",
    "price_volume_confirmed": "上涨日得到成交量确认",
    "volume_trend_positive": "5 日均量不低于 20 日均量",
    "obv_trend_positive": "OBV 十日方向向上",
    "close_near_high_with_volume": "放量接近 20 日高位",
    "not_over_extended": "价格未明显偏离 MA20",
    "position_above_20d_mid": "价格位于近 20 日区间中位以上",
    "near_20d_high": "距 20 日高点不超过 10%",
    "atr_not_high": "ATR 波动未超过高风险阈值",
    "gap_not_extreme": "当日跳空幅度不极端",
}

STATUS_LABELS = {
    "NO_TRADE": "不满足交易条件",
    "WATCH": "观察",
    "BUY_CANDIDATE": "买入候选",
    "RISK_REDUCE": "风险降低候选",
    "STOP_TRIGGER": "止损规则触发",
}

SELECTION_LABELS = {
    "PASS": "符合选股规则",
    "FAIL": "不符合选股规则",
    "DATA_GAP": "选股数据待补",
    "DISABLED": "选股规则未启用",
}

KIND_LABELS = {
    "fact": "已发生事实",
    "metric": "指标计算结果",
    "rule": "规则触发",
    "judgment": "主观判断",
    "emotion": "情绪因素",
    "risk": "风险点",
}

EMOTION_FLAGS = {
    "watched_over_20m": "连续看盘超过 20 分钟",
    "chasing_after_sale": "刚卖飞后想追回",
    "loss_distress": "因浮亏难受而想止痛操作",
    "urgent_recovery": "急于回本",
    "seeing_rise_only": "只是看到上涨就想操作",
    "anxious_or_regretful": "当前处于焦虑或后悔状态",
    "averaging_down": "只是因为亏损而想摊低成本",
}


def _item(kind: str, text: str) -> dict[str, str]:
    return {"kind": kind, "kind_label": KIND_LABELS[kind], "text": text}


def _to_float(value: Any, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _read_optional_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _latest_symbol_row(df: pd.DataFrame, symbol: str, date_column: str) -> dict[str, Any]:
    if df.empty or "symbol" not in df.columns:
        return {}
    rows = df[df["symbol"].astype(str).str.upper() == symbol]
    if rows.empty:
        return {}
    if date_column in rows.columns:
        rows = rows.sort_values(date_column)
    return rows.iloc[-1].to_dict()


def _symbols_from_frame(df: pd.DataFrame) -> set[str]:
    if df.empty or "symbol" not in df.columns:
        return set()
    symbols: set[str] = set()
    for value in df["symbol"]:
        try:
            symbols.add(validate_symbol(value))
        except ValueError:
            continue
    return symbols


def _fallback(value: Any, row: dict[str, Any], key: str, default: Any = None) -> Any:
    if value not in (None, ""):
        return value
    candidate = row.get(key, default)
    if not isinstance(candidate, (list, dict)) and pd.isna(candidate):
        return default
    return candidate


def _parse_review_date(value: Any) -> date:
    if value in (None, ""):
        return date.today()
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError):
        raise ValueError("审查日期格式不正确。") from None


def _load_market_data(
    payload: dict[str, Any], project_root: Path, max_upload_mb: int
) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    source = str(payload.get("data_source") or "local")
    if source == "upload":
        csv_text = payload.get("csv_text") or ""
        if not csv_text.strip():
            raise ValueError("请选择行情 CSV 文件后再运行审查。")
        if len(csv_text.encode("utf-8")) > max_upload_mb * 1024 * 1024:
            raise ValueError(f"上传的 CSV 超过 {max_upload_mb} MB。")
        raw = pd.read_csv(StringIO(csv_text))
        return raw, normalize_ohlcv(raw), str(payload.get("csv_name") or "临时上传 CSV")

    symbol = str(payload.get("symbol") or "").strip().upper()
    timeframe = str(payload.get("timeframe") or "day").strip()
    csv_path = find_latest_csv(symbol, timeframe, base_dir=str(project_root / "data"))
    return pd.read_csv(csv_path), load_ohlcv(csv_path), str(csv_path.relative_to(project_root))


def _data_quality(
    raw: pd.DataFrame,
    df: pd.DataFrame,
    source_name: str,
    timeframe: str,
    reference_date: date,
    settings: dict[str, Any],
) -> dict[str, Any]:
    columns = {str(column).strip().lower() for column in raw.columns}
    numeric_columns = [column for column in ["open", "high", "low", "close", "volume"] if column in columns]
    normalized_raw = raw.copy()
    normalized_raw.columns = [str(column).strip().lower() for column in raw.columns]

    invalid_numeric = 0
    for column in numeric_columns:
        invalid_numeric += int(pd.to_numeric(normalized_raw[column], errors="coerce").isna().sum())

    duplicate_dates = int(df["date"].duplicated().sum())
    ohlc_anomalies = int(
        (
            (df["high"] < df[["open", "close", "low"]].max(axis=1))
            | (df["low"] > df[["open", "close", "high"]].min(axis=1))
            | (df["volume"] < 0)
        ).sum()
    )
    latest_date = df["date"].iloc[-1].date()
    age_days = (reference_date - latest_date).days
    timeframe_group = "minute" if timeframe.startswith("minute/") else timeframe
    max_age_days = int(settings.get("max_age_days", {}).get(timeframe_group, 7))
    minimum_rows = int(settings.get("minimum_rows", 60))
    future_grace = int(settings.get("future_date_grace_days", 1))

    warnings: list[str] = []
    blockers: list[str] = []
    if len(df) < minimum_rows:
        message = f"有效数据少于 {minimum_rows} 行，长周期指标的解释能力有限"
        warnings.append(message)
        blockers.append(message)
    if invalid_numeric:
        message = f"原始数据中有 {invalid_numeric} 个无法解析的数值"
        warnings.append(message)
        blockers.append(message)
    if duplicate_dates:
        message = f"发现 {duplicate_dates} 个重复日期"
        warnings.append(message)
        blockers.append(message)
    if ohlc_anomalies:
        message = f"发现 {ohlc_anomalies} 行 OHLC 或成交量异常"
        warnings.append(message)
        blockers.append(message)
    if age_days > max_age_days:
        message = f"最后行情日期距审查日期 {age_days} 天，超过 {timeframe_group} 周期允许的 {max_age_days} 天"
        warnings.append(message)
        blockers.append(message)
    if age_days < -future_grace:
        message = f"最后行情日期晚于审查日期 {-age_days} 天，请检查日期字段"
        warnings.append(message)
        blockers.append(message)
    if "demo" in source_name.lower():
        message = "当前使用 Demo 数据，只能验证流程，不能视为正式行情"
        warnings.append(message)
        blockers.append(message)

    return {
        "source": source_name,
        "rows": int(len(df)),
        "start_date": str(df["date"].iloc[0].date()),
        "latest_date": str(latest_date),
        "reference_date": str(reference_date),
        "age_days": age_days,
        "max_age_days": max_age_days,
        "minimum_rows": minimum_rows,
        "columns": sorted(columns),
        "required_columns": sorted(REQUIRED_COLUMNS),
        "has_amount": "amount" in columns,
        "invalid_numeric": invalid_numeric,
        "duplicate_dates": duplicate_dates,
        "ohlc_anomalies": ohlc_anomalies,
        "warnings": warnings,
        "blockers": blockers,
        "status": "warning" if warnings else "ok",
    }


def _record_summary(project_root: Path, symbol: str, latest_price: float, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return {"message": "本次未读取本地成交记录", "timeline": []}

    trades_path = project_root / "records" / "my_trades.csv"
    if not trades_path.exists():
        return {"message": "未找到 records/my_trades.csv", "timeline": []}

    trades = load_trades(trades_path)
    summary = summarize_symbol(trades, symbol, latest_price=latest_price)
    if "message" in summary:
        summary["timeline"] = []
        return summary

    rows = trades[trades["symbol"].astype(str).str.upper() == symbol].copy()
    sources = rows.get("source", pd.Series([""] * len(rows), index=rows.index)).fillna("").astype(str).str.lower()
    notes = rows.get("notes", pd.Series([""] * len(rows), index=rows.index)).fillna("").astype(str).str.lower()
    demo_mask = sources.eq("demo") | notes.str.contains("demo|示例", regex=True)
    summary["demo_row_count"] = int(demo_mask.sum())
    summary["contains_demo_data"] = bool(demo_mask.any())
    rows = rows.sort_values([column for column in ["trade_date", "trade_time"] if column in rows.columns])
    timeline = []
    for _, row in rows.tail(6).iterrows():
        side = str(row.get("side", "")).upper()
        timeline.append(
            {
                "date": str(row.get("trade_date", "")),
                "time": str(row.get("trade_time", "")),
                "side": "买入" if side == "BUY" else "卖出" if side == "SELL" else side,
                "price": round(float(row.get("price", 0) or 0), 4),
                "shares": int(float(row.get("shares", 0) or 0)),
                "rule_status": str(row.get("rule_status", "")),
                "emotion": str(row.get("emotion", "")),
            }
        )
    summary["timeline"] = timeline
    return summary


def _chart_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    columns = ["date", "close", "ma20", "ma60", "volume"]
    rows = []
    latest_date = df["date"].iloc[-1]
    six_month_cutoff = latest_date - pd.DateOffset(months=6)
    chart_frame = df[df["date"] >= six_month_cutoff]
    for _, row in chart_frame[columns].iterrows():
        rows.append(
            {
                "date": str(row["date"].date()),
                "close": round(float(row["close"]), 4),
                "ma20": None if pd.isna(row["ma20"]) else round(float(row["ma20"]), 4),
                "ma60": None if pd.isna(row["ma60"]) else round(float(row["ma60"]), 4),
                "volume": round(float(row["volume"]), 4),
            }
        )
    return rows


def _latest_day_summary(project_root: Path, symbol: str) -> dict[str, Any] | None:
    try:
        path = find_latest_csv(symbol, "day", base_dir=str(project_root / "data"))
        frame = load_ohlcv(path)
    except (FileNotFoundError, ValueError, OSError, pd.errors.ParserError):
        return None
    latest = frame.iloc[-1]
    return {
        "date": str(latest["date"].date()),
        "close": round(float(latest["close"]), 4),
        "path": str(path.relative_to(project_root)),
        "is_demo": "demo" in path.name.lower(),
    }


def get_bootstrap(project_root: str | Path) -> dict[str, Any]:
    root = Path(project_root)
    watchlist = _read_optional_csv(root / "watchlist.csv")
    positions = _read_optional_csv(root / "records" / "positions_snapshot.csv")
    trades = _read_optional_csv(root / "records" / "my_trades.csv")
    watch_symbols = _symbols_from_frame(watchlist)
    position_symbols = _symbols_from_frame(positions)
    trade_symbols = _symbols_from_frame(trades)

    library_names = local_stock_name_map(root)
    library_symbols = set(library_names)
    symbol_names: dict[str, str] = dict(library_names)
    for frame in [watchlist, positions, trades]:
        if frame.empty or "symbol" not in frame.columns:
            continue
        for _, row in frame.iterrows():
            try:
                symbol = validate_symbol(str(row.get("symbol", "")))
            except ValueError:
                continue
            stock_name = row.get("stock_name", "")
            symbol_names[symbol] = symbol if pd.isna(stock_name) or not str(stock_name).strip() else str(stock_name).strip()

    data_root = root / "data"
    data_symbols: set[str] = set()
    if data_root.exists():
        for directory in data_root.iterdir():
            if directory.is_dir() and directory.name != "universe":
                try:
                    symbol = validate_symbol(directory.name)
                except ValueError:
                    continue
                symbol_names.setdefault(symbol, symbol)
                data_symbols.add(symbol)

    symbols = []
    for symbol in sorted(symbol_names):
        raw_root = data_root / symbol / "raw"
        timeframes: set[str] = set()
        if raw_root.exists():
            for csv_path in raw_root.rglob("*.csv"):
                timeframes.add(str(csv_path.parent.relative_to(raw_root)))

        watch_row = _latest_symbol_row(watchlist, symbol, "last_review_date")
        position_row = _latest_symbol_row(positions, symbol, "snapshot_date")
        trade_row = _latest_symbol_row(trades, symbol, "trade_date")
        account_default = _to_float(
            position_row.get("account_total_asset"), _to_float(trade_row.get("account_total_asset"), 140000)
        )
        shares_default = _to_int(
            position_row.get("shares_total"), _to_int(trade_row.get("remaining_shares"), 0)
        )
        cost_default = _to_float(
            position_row.get("avg_cost"),
            _to_float(trade_row.get("avg_cost_after_trade"), _to_float(watch_row.get("entry_price"))),
        )
        latest_day = _latest_day_summary(root, symbol)
        sources = []
        if symbol in watch_symbols:
            sources.append("watchlist")
        if symbol in position_symbols:
            sources.append("position")
        if symbol in trade_symbols:
            sources.append("trade")
        if symbol in data_symbols:
            sources.append("data")
        if symbol in library_symbols:
            sources.append("library")
        symbols.append(
            {
                "symbol": symbol,
                "stock_name": symbol_names[symbol],
                "timeframes": sorted(timeframes) or ["day"],
                "defaults": {
                    "account_total_asset": account_default,
                    "position_shares": shares_default,
                    "avg_cost": cost_default,
                    "current_price": latest_day["close"] if latest_day else _to_float(position_row.get("current_price"), _to_float(watch_row.get("current_price"))),
                    "planned_position_pct": _to_float(watch_row.get("planned_position_pct"), 0.20),
                    "stop_loss_price": _to_float(watch_row.get("stop_loss_price")),
                    "take_profit_price": _to_float(watch_row.get("take_profit_price")),
                    "holding_peak_price": _to_float(position_row.get("holding_peak_price")),
                },
                "latest_day": latest_day,
                "sources": sources,
                "in_watchlist": symbol in watch_symbols,
                "has_position": symbol in position_symbols,
                "has_trades": symbol in trade_symbols,
                "has_local_data": symbol in data_symbols,
                "in_local_library": symbol in library_symbols
                or symbol in watch_symbols
                or symbol in data_symbols,
            }
        )

    return {
        "symbols": symbols,
        "default_symbol": symbols[0]["symbol"] if symbols else "",
        "today": str(date.today()),
        "personal_data": personal_data_status(root),
        "notice": "面板只做规则审查，不替用户作出最终买卖决定。",
    }


def run_full_review(payload: dict[str, Any], project_root: str | Path) -> dict[str, Any]:
    root = Path(project_root)
    symbol = validate_symbol(str(payload.get("symbol") or ""))
    timeframe = validate_timeframe(str(payload.get("timeframe") or "day"))
    review_date = _parse_review_date(payload.get("review_date"))
    intent = str(payload.get("review_intent") or "daily")
    if intent not in {"daily", "open", "add", "risk"}:
        raise ValueError("审查目标不正确。")
    market_context = str(payload.get("market_context") or "unknown")
    if market_context not in {"unknown", "supportive", "neutral", "defensive"}:
        raise ValueError("市场环境状态不正确。")

    rules = load_rules(str(root / "config" / "strategy_rules.yaml"))
    quality_settings = rules.get("data_quality", {})
    raw_df, market_df, source_name = _load_market_data(
        payload, root, int(quality_settings.get("max_upload_mb", 8))
    )
    quality = _data_quality(raw_df, market_df, source_name, timeframe, review_date, quality_settings)
    indicator_df = add_indicators(market_df)

    watchlist = _read_optional_csv(root / "watchlist.csv")
    positions = _read_optional_csv(root / "records" / "positions_snapshot.csv")
    watch_row = _latest_symbol_row(watchlist, symbol, "last_review_date")
    position_row = _latest_symbol_row(positions, symbol, "snapshot_date")
    use_local_records = _to_bool(payload.get("use_local_records", True))
    stock_name_value = payload.get("stock_name") or watch_row.get("stock_name") or position_row.get("stock_name")
    stock_name = symbol if stock_name_value is None or pd.isna(stock_name_value) else str(stock_name_value).strip() or symbol

    latest_market_close = float(market_df.iloc[-1]["close"])
    account_asset = _to_float(
        _fallback(payload.get("account_total_asset"), position_row if use_local_records else {}, "account_total_asset"),
        float(rules["position_rules"]["default_account_total_asset"]),
    )
    shares = _to_int(
        _fallback(payload.get("position_shares"), position_row if use_local_records else {}, "shares_total"),
        0,
    )
    avg_cost = _to_float(
        _fallback(payload.get("avg_cost"), position_row if use_local_records else {}, "avg_cost"),
        _to_float(watch_row.get("entry_price")) if use_local_records else None,
    )
    current_price = _to_float(payload.get("current_price"), latest_market_close) or latest_market_close
    stop_loss_price = _to_float(
        _fallback(payload.get("stop_loss_price"), watch_row if use_local_records else {}, "stop_loss_price")
    )
    take_profit_price = _to_float(
        _fallback(payload.get("take_profit_price"), watch_row if use_local_records else {}, "take_profit_price")
    )
    holding_peak_price = _to_float(
        _fallback(payload.get("holding_peak_price"), position_row if use_local_records else {}, "holding_peak_price")
    )
    planned_position_pct_value = _to_float(
        _fallback(payload.get("planned_position_pct"), watch_row if use_local_records else {}, "planned_position_pct"),
        None,
    )
    planned_position_pct = 0.20 if planned_position_pct_value is None else planned_position_pct_value
    planned_add_amount = max(0.0, _to_float(payload.get("planned_add_amount"), 0.0) or 0.0)
    records = _record_summary(root, symbol, current_price, use_local_records)
    if use_local_records and "message" not in records:
        if payload.get("account_total_asset") in (None, "") and _to_float(position_row.get("account_total_asset")) is None:
            account_asset = _to_float(records.get("account_total_asset"), account_asset)
        if payload.get("position_shares") in (None, "") and _to_float(position_row.get("shares_total")) is None:
            shares = _to_int(records.get("remaining_shares"), shares)
        if payload.get("avg_cost") in (None, "") and _to_float(position_row.get("avg_cost")) is None:
            avg_cost = _to_float(records.get("avg_cost"), avg_cost)

    if not account_asset or account_asset <= 0:
        raise ValueError("账户总资产必须大于 0。")
    if not 0 <= planned_position_pct <= 1:
        raise ValueError("计划仓位必须位于 0% 到 100% 之间。")
    if current_price <= 0:
        raise ValueError("当前价格必须大于 0。")
    for label, value in [
        ("平均成本", avg_cost),
        ("止损价格", stop_loss_price),
        ("止盈价格", take_profit_price),
        ("持仓阶段最高价", holding_peak_price),
    ]:
        if value is not None and value <= 0:
            raise ValueError(f"{label}必须大于 0。")
    if holding_peak_price is not None and holding_peak_price < current_price:
        raise ValueError("持仓阶段最高价不能低于当前价格。")

    signal_entry_price = avg_cost if shares > 0 and avg_cost and avg_cost > 0 else None
    signal = evaluate_latest(
        indicator_df,
        rules=rules,
        entry_price=signal_entry_price,
        market_context=market_context,
        # 显式给出本次复盘所属实例的规则库，不再让 signal_engine 回退到代码所在目录。
        registry=RuleRegistry(root),
    )
    latest = signal.latest
    selection = evaluate_selection(market_df, symbol, stock_name, rules)

    data_blockers = list(quality["blockers"])
    position_notes = str(position_row.get("notes") or "").lower()
    position_is_demo = "demo" in position_notes or "示例" in position_notes
    personal_data_contains_demo = bool(
        use_local_records
        and (position_is_demo or records.get("contains_demo_data"))
    )
    if personal_data_contains_demo:
        data_blockers.append("本地持仓或成交记录包含示例数据，需导入真实个人数据后再作正式审查")
    selection_blockers = list(selection.blockers)
    selection_gate_blockers = [] if intent == "risk" else selection_blockers

    market_value = shares * current_price
    position_pct = market_value / account_asset if account_asset else 0.0
    projected_position_pct = (market_value + planned_add_amount) / account_asset if account_asset else 0.0
    planned_position_value = account_asset * planned_position_pct
    normal_max = float(rules["position_rules"]["normal_position_pct_max"])
    high_risk_pct = float(rules["position_rules"]["high_risk_position_pct"])
    heavy_pct = float(rules["position_rules"]["heavy_position_pct"])
    single_add_pct_max = float(rules["position_rules"].get("single_add_pct_max", 0.10))
    buy_rules = rules.get("trade_rules", {}).get("buy", {})
    sell_rules = rules.get("trade_rules", {}).get("sell", {})
    fundamentals = review_fundamental_payload(payload, review_date)
    raw_screening_context = payload.get("screening_context")
    screening_context = raw_screening_context if isinstance(raw_screening_context, dict) else {}
    target_plan = evaluate_target_plan(
        current_price=current_price,
        stop_loss_price=stop_loss_price,
        target_price=take_profit_price,
        latest=latest,
        min_reward_risk=float(buy_rules.get("min_reward_risk", 2.0)),
    )
    max_single_order_plan_pct = float(buy_rules.get("max_single_order_plan_pct", 0.40))
    capacity = suggest_next_trade_capacity(account_asset or 0, market_value, normal_max, current_price)
    unrealized_pnl = (current_price - avg_cost) * shares if avg_cost else None
    unrealized_pnl_pct = current_price / avg_cost - 1 if avg_cost else None

    emotion_hits = [label for key, label in EMOTION_FLAGS.items() if _to_bool(payload.get(key))]
    buy_reason = str(payload.get("buy_reason") or "").strip()
    sell_condition = str(payload.get("sell_condition") or "").strip()
    feeling_words = ["感觉", "肯定", "一定", "怕错过", "会涨"]
    reason_is_feeling = bool(buy_reason) and any(word in buy_reason for word in feeling_words)
    if reason_is_feeling:
        emotion_hits.append("买入理由包含感觉式或确定性判断")

    behavior_blockers = list(emotion_hits)
    if intent == "open" and shares > 0:
        behavior_blockers.append("当前已有持仓，本次目标不应选择新建仓")
    if intent == "add" and shares <= 0:
        behavior_blockers.append("当前没有持仓，本次目标不能选择加仓")
    if intent == "risk" and shares <= 0:
        behavior_blockers.append("当前没有持仓，本次目标不需要选择风险处理")
    if str(payload.get("decision_window") or "after_close") == "intraday":
        behavior_blockers.append("中线收盘决策型身份与盘中临时决定冲突")
    if position_pct >= high_risk_pct:
        behavior_blockers.append(f"当前单票仓位 {position_pct:.1%} 已达到高风险阈值")
    if planned_add_amount > 0 and projected_position_pct >= high_risk_pct:
        behavior_blockers.append(f"计划金额会使单票仓位达到 {projected_position_pct:.1%}")
    if planned_position_pct >= high_risk_pct:
        behavior_blockers.append(f"计划总仓位 {planned_position_pct:.1%} 已达到高风险阈值")
    if intent in {"open", "add"} and planned_add_amount > account_asset * single_add_pct_max:
        behavior_blockers.append(f"单次计划金额超过账户资产的 {single_add_pct_max:.0%}")
    if intent in {"open", "add"} and planned_position_value > 0 and planned_add_amount > planned_position_value * max_single_order_plan_pct:
        behavior_blockers.append(f"单次计划金额超过计划总仓位的 {max_single_order_plan_pct:.0%}")
    if use_local_records and shares > 0 and "message" in records:
        behavior_blockers.append("存在持仓，但没有可核对的本地成交记录")
    if signal.status == "WATCH":
        behavior_blockers.append("技术信号仅为 WATCH，不允许据此临时操作")

    early_triggers = []
    if shares > 0 and stop_loss_price and current_price <= stop_loss_price:
        early_triggers.append(f"当前价格 {current_price:.2f} 已到达事前止损线 {stop_loss_price:.2f}")
    if shares > 0 and take_profit_price and current_price >= take_profit_price:
        early_triggers.append(f"当前价格 {current_price:.2f} 已到达事前止盈线 {take_profit_price:.2f}")
    trailing_stop_pct = float(rules["signal_rules"]["stop_trigger"].get("trailing_stop_from_high_pct", -0.10))
    trailing_trigger_price = (
        holding_peak_price * (1 + trailing_stop_pct) if holding_peak_price is not None else None
    )
    if shares > 0 and trailing_trigger_price and current_price <= trailing_trigger_price:
        early_triggers.append(
            f"当前价格 {current_price:.2f} 已从持仓阶段最高价 {holding_peak_price:.2f} "
            f"回撤至少 {abs(trailing_stop_pct):.0%}"
        )

    plan_gaps = []
    if intent in {"open", "add"}:
        if not _to_bool(payload.get("has_written_plan")):
            plan_gaps.append("尚未确认已写好交易计划")
        if not buy_reason:
            plan_gaps.append("买入理由为空")
        if not sell_condition:
            plan_gaps.append("失败或退出条件为空")
        if not stop_loss_price:
            plan_gaps.append("没有事前止损价格")
        if not _to_bool(payload.get("max_loss_defined")):
            plan_gaps.append("没有确认最大可承受亏损")
        if market_context == "unknown":
            plan_gaps.append("尚未核对大盘或行业环境")
        if buy_rules.get("require_fundamental_source", True) and fundamentals["status"] in {"MISSING", "PARTIAL"}:
            plan_gaps.append("基本面资料的来源、日期或核心摘要不完整")
        if target_plan["status"] != "CONSISTENT":
            plan_gaps.append(f"目标校验状态为“{target_plan['label']}”")
        if planned_position_pct <= 0:
            plan_gaps.append("计划总仓位必须大于 0%")
        if planned_add_amount <= 0:
            plan_gaps.append("单次计划金额尚未填写")
        if planned_position_pct > normal_max:
            plan_gaps.append(f"计划总仓位 {planned_position_pct:.1%} 超过正常上限 {normal_max:.0%}")
    elif intent == "risk" and not sell_condition:
        plan_gaps.append("尚未填写事前风险处理条件")

    if early_triggers:
        permission = "触发提前规则"
        permission_tone = "danger"
    elif data_blockers or selection_gate_blockers:
        permission = "继续观察"
        permission_tone = "warning"
    elif behavior_blockers:
        permission = "不允许临时操作"
        permission_tone = "danger"
    elif signal.status == "BUY_CANDIDATE":
        permission = "允许进入计划讨论"
        permission_tone = "positive"
    elif signal.status in {"RISK_REDUCE", "STOP_TRIGGER"}:
        permission = "需要复盘后再决定"
        permission_tone = "warning"
    else:
        permission = "继续观察"
        permission_tone = "neutral"

    if early_triggers:
        conclusion = "已触发事前规则；只讨论规则执行与复盘，不讨论临时操作。"
    elif selection_gate_blockers:
        conclusion = "当前未通过选股前置规则，不进入新增或扩大仓位的计划讨论。"
    elif signal.status == "BUY_CANDIDATE" and data_blockers:
        conclusion = "技术信号较强，但行情数据不满足正式审查条件，当前只能观察。"
    elif signal.status == "BUY_CANDIDATE" and behavior_blockers:
        conclusion = "技术信号较强，但交易行为规则不允许立即操作。"
    elif permission == "允许进入计划讨论" and plan_gaps:
        conclusion = "当前允许进入计划讨论，但计划要素尚未完整，仍不满足直接操作条件。"
    elif permission == "允许进入计划讨论":
        conclusion = "当前仅允许进入计划讨论；技术候选不等于买卖命令。"
    elif signal.status == "RISK_REDUCE":
        conclusion = "当前为风险降低候选，需要先复盘仓位和事前规则。"
    elif signal.status == "STOP_TRIGGER":
        conclusion = "默认止损条件出现，需要核对它是否属于事前写明的规则。"
    else:
        conclusion = "当前不满足交易候选条件，维持观察并等待下一次收盘数据。"

    data_items = [
        _item("fact", f"数据源：{quality['source']}"),
        _item("fact", f"{timeframe} 周期有效数据 {quality['rows']} 行，区间 {quality['start_date']} 至 {quality['latest_date']}"),
        _item("metric", f"必需字段完整；重复日期 {quality['duplicate_dates']} 个，OHLC 异常 {quality['ohlc_anomalies']} 行"),
    ]
    data_items.extend(_item("risk", warning) for warning in quality["warnings"])

    technical_items = [
        _item(
            "metric",
            f"技术状态 {signal.status}（{STATUS_LABELS[signal.status]}），"
            f"候选规则 {signal.matched_rule_count}/{signal.active_rule_count}（{signal.score_ratio:.0%}），"
            f"风险得分 {signal.risk_score}",
        ),
        _item("rule", f"候选优先级 {signal.priority['level']} · {signal.priority['label']}；{signal.priority['context_note']}"),
        _item("metric", f"收盘 {latest['close']:.2f}；MA20 {latest['ma20']:.2f}；MA60 {latest['ma60']:.2f}"),
        _item("metric", f"RSI14 {latest['rsi14']:.1f}；量比 {latest['volume_ratio']:.2f}；ATR/价格 {latest['atr_pct']:.2%}"),
        _item("rule", "满足：" + "、".join(CONDITION_LABELS.get(key, key) for key in signal.passed)),
        _item("rule", "未满足：" + "、".join(CONDITION_LABELS.get(key, key) for key in signal.failed)),
    ]
    group_status_labels = {
        "PASS": "达到组要求",
        "PARTIAL": "部分满足",
        "FAIL": "未形成确认",
        "INACTIVE": "本组合未启用",
    }
    for group in signal.groups.values():
        technical_items.append(
            _item(
                "metric",
                f"{group['label']}：{group['score']:.0f}/{group['max_score']:.0f} · {group_status_labels[group['status']]}",
            )
        )
    technical_items.extend(_item("risk", risk) for risk in signal.risks)

    selection_items = [
        _item("rule", f"选股状态 {selection.status}（{SELECTION_LABELS[selection.status]}）")
    ]
    selection_items.extend(_item("rule", item) for item in selection.passed)
    selection_items.extend(_item("risk", item) for item in selection.failed)
    selection_items.extend(_item("risk", item) for item in selection.data_gaps)
    if selection.notes:
        selection_items.append(_item("judgment", f"选股规则备注：{selection.notes}"))
    market_context_labels = {
        "supportive": "市场环境偏支持",
        "neutral": "市场环境中性",
        "defensive": "市场环境偏防守",
        "unknown": "市场环境尚未核对",
    }
    selection_items.append(_item("fact", market_context_labels[market_context]))
    if screening_context:
        score = _to_float(screening_context.get("candidate_score_ratio"))
        source_parts = []
        if screening_context.get("screening_created_at"):
            source_parts.append(f"筛选时间 {screening_context['screening_created_at']}")
        if screening_context.get("snapshot_date"):
            source_parts.append(f"股票池日期 {screening_context['snapshot_date']}")
        if screening_context.get("candidate_priority"):
            source_parts.append(f"优先级 {screening_context['candidate_priority']}")
        if score is not None:
            source_parts.append(f"组合符合比例 {score:.0%}")
        selection_items.append(
            _item("fact", "候选筛选来源：" + ("；".join(source_parts) or "来源批次已保存"))
        )
    if fundamentals["source_name"]:
        source_text = f"基本面来源：{fundamentals['source_name']}"
        if fundamentals["as_of_date"]:
            source_text += f"；截止 {fundamentals['as_of_date']}"
        selection_items.append(_item("fact", source_text))
    selection_items.extend(_item("metric", item) for item in fundamentals["facts"])
    selection_items.extend(_item("risk", item) for item in fundamentals["missing"])
    selection_items.extend(_item("risk", item) for item in fundamentals["warnings"])
    if buy_reason:
        selection_items.append(_item("judgment", f"事前买入候选理由：{buy_reason}"))

    record_items = []
    if "message" in records:
        record_items.append(_item("fact", records["message"]))
    else:
        record_items.extend(
            [
                _item("fact", f"共 {records['trade_count']} 笔记录：买入 {records['buy_count']} 笔，卖出 {records['sell_count']} 笔"),
                _item("metric", f"累计买入 {records['total_buy_amount']:.2f} 元，累计卖出 {records['total_sell_amount']:.2f} 元"),
            ]
        )
        # 台账没记下的数字不得以 0 的面貌出现：那是在陈述一个从未被记录的事实。
        if records.get("ledger_complete", True):
            record_items.append(
                _item(
                    "metric",
                    f"已实现盈亏 {records['realized_pnl']:.2f} 元；成交账本剩余股数 {records['remaining_shares']} 股",
                )
            )
        else:
            record_items.append(
                _item("risk", "成交台账缺少必要字段，已实现盈亏与剩余股数无法确认（历史数据不完整）")
            )
        if records.get("contains_demo_data"):
            record_items.append(_item("risk", "当前成交摘要包含示例记录，不能作为真实交易复盘依据"))

    position_items = [
        _item("fact", f"本次按账户总资产 {account_asset:.2f} 元、持仓 {shares} 股、价格 {current_price:.2f} 元计算"),
        _item("metric", f"持仓市值 {market_value:.2f} 元，单票仓位 {position_pct:.1%}，等级为 {classify_position(position_pct)}"),
        _item("metric", f"计划总仓位 {planned_position_pct:.1%}，对应上限金额 {planned_position_value:.2f} 元"),
        _item("metric", f"按 {normal_max:.0%} 上限测算，剩余容量 {capacity['remaining_buy_capacity_value']:.2f} 元（仅为上限测算）"),
    ]
    if avg_cost:
        position_items.append(
            _item("metric", f"成本 {avg_cost:.4f} 元，浮动盈亏 {unrealized_pnl:.2f} 元（{unrealized_pnl_pct:.1%}）")
        )
    if position_pct >= heavy_pct:
        position_items.append(_item("risk", "单票仓位达到重仓阈值，价格波动可能主导情绪"))
    elif position_pct >= high_risk_pct:
        position_items.append(_item("risk", "单票仓位达到高风险阈值，当前问题不只是选股问题"))
    elif position_pct > normal_max:
        position_items.append(_item("risk", f"单票仓位超过正常上限 {normal_max:.0%}"))
    if planned_add_amount:
        position_items.append(_item("metric", f"计划金额 {planned_add_amount:.2f} 元对应的预计仓位为 {projected_position_pct:.1%}"))
    if buy_rules.get("notes"):
        position_items.append(_item("judgment", f"买入规则备注：{buy_rules['notes']}"))
    price_gap = current_price / latest_market_close - 1
    if abs(price_gap) > 0.02:
        position_items.append(_item("risk", f"持仓估值价格与行情最后收盘价相差 {price_gap:.1%}，请确认价格口径"))

    emotion_items = [_item("fact", "情绪检查由用户本次勾选结果生成，不从行情走势推断")]
    if emotion_hits:
        emotion_items.extend(_item("emotion", hit) for hit in emotion_hits)
        emotion_items.append(_item("rule", "命中高风险情绪规则，本次不允许临时操作"))
    else:
        emotion_items.append(_item("emotion", "未勾选高风险情绪触发项"))

    early_items = [_item("rule", f"目标校验：{target_plan['label']}")]
    early_items.extend(_item("metric", item) for item in target_plan["facts"])
    early_items.extend(_item("risk", item) for item in target_plan["risks"])
    if stop_loss_price:
        early_items.append(_item("fact", f"事前止损线：{stop_loss_price:.2f}"))
    else:
        early_items.append(_item("risk", "没有可核对的事前止损线"))
    if take_profit_price:
        early_items.append(_item("fact", f"事前目标价格：{take_profit_price:.2f}"))
    if sell_condition:
        early_items.append(_item("rule", f"事前失败或退出条件：{sell_condition}"))
    if holding_peak_price is not None and trailing_trigger_price is not None:
        early_items.append(
            _item(
                "fact",
                f"持仓阶段最高价 {holding_peak_price:.2f}；移动止盈回撤触发价 {trailing_trigger_price:.2f}",
            )
        )
    if shares <= 0:
        early_items.append(_item("fact", "当前没有持仓，不判断止损或止盈价格是否触发"))
    elif early_triggers:
        early_items.extend(_item("rule", trigger) for trigger in early_triggers)
    else:
        early_items.append(_item("rule", "当前价格未触发已填写的事前价格规则"))
    if signal.status == "STOP_TRIGGER" and not early_triggers:
        stop_pct = float(rules["signal_rules"]["stop_trigger"]["default_stop_loss_pct"])
        early_items.append(_item("risk", f"技术引擎命中默认 {stop_pct:.0%} 止损条件，但它不能替代事前写明的规则"))
    if sell_rules.get("notes"):
        early_items.append(_item("judgment", f"卖出规则备注：{sell_rules['notes']}"))

    permission_items = [_item("rule", f"结论：{permission}")]
    permission_items.extend(_item("risk", f"数据前置条件：{blocker}") for blocker in data_blockers)
    permission_items.extend(_item("risk", f"选股前置条件：{blocker}") for blocker in selection_gate_blockers)
    permission_items.extend(_item("risk", blocker) for blocker in behavior_blockers)
    permission_items.extend(_item("risk", f"计划缺口：{gap}") for gap in plan_gaps)
    if permission == "允许进入计划讨论":
        permission_items.append(_item("judgment", "只允许进入计划讨论，不代表可以直接执行交易"))

    observations = [
        f"下一次审查先更新至目标日期的 {timeframe} 行情，再比较技术状态是否变化",
        "确认收盘价与 MA20、MA5/MA10、MACD 柱和成交量条件是否同时改善",
        "操作前重新核对单票仓位、事前止损、最大亏损和分批方式",
    ]
    if quality["warnings"]:
        observations.insert(0, "先处理数据完整性提示，避免用过期或 Demo 行情作正式判断")
    if selection_blockers:
        observations.append("先处理未通过的选股条件；风险处理不受候选池门槛阻断")
    if fundamentals["missing"] or fundamentals["warnings"]:
        observations.append("补充或更新基本面资料，并保留来源链接、报告期和原始公告")
    if target_plan["status"] not in {"CONSISTENT", "NOT_SET"}:
        observations.append("重新核对目标、止损、盈亏比和 ATR 距离，不用预期收益倒推目标价")
    if emotion_hits:
        observations.append("离开屏幕 30 分钟，回来后只检查事前规则")
    if plan_gaps:
        observations.append("补全计划缺口后重新运行审查")

    sections = [
        {"number": "01", "id": "data", "title": "数据完整性", "tone": quality["status"], "items": data_items},
        {"number": "02", "id": "technical", "title": "候选规则组合", "tone": "positive" if signal.status == "BUY_CANDIDATE" else "warning" if signal.status in {"RISK_REDUCE", "STOP_TRIGGER"} else "neutral", "items": technical_items},
        {"number": "03", "id": "identity", "title": "准入、市场与基本面", "tone": "danger" if selection.status == "FAIL" else "warning" if selection.status == "DATA_GAP" or fundamentals["status"] in {"MISSING", "PARTIAL", "WARNING"} else "neutral", "items": selection_items + [_item("fact", "默认身份：中线逻辑 + 小仓位 + 日线信号 + 收盘决策"), _item("judgment", "分钟线只用于观察，不用于推翻日线计划"), _item("risk", "盘中连续盯盘和临时决定与当前交易身份不匹配") if str(payload.get("decision_window") or "after_close") == "intraday" else _item("rule", "本次选择的决策时段与收盘决策型身份一致")]},
        {"number": "04", "id": "records", "title": "操作记录摘要", "tone": "neutral", "items": record_items, "timeline": records.get("timeline", [])},
        {"number": "05", "id": "position", "title": "仓位风险", "tone": "danger" if position_pct >= high_risk_pct else "warning" if position_pct > normal_max else "neutral", "items": position_items},
        {"number": "06", "id": "emotion", "title": "情绪风险", "tone": "danger" if emotion_hits else "neutral", "items": emotion_items},
        {"number": "07", "id": "early", "title": "目标与提前规则", "tone": "danger" if early_triggers else "warning" if target_plan["status"] not in {"CONSISTENT", "NOT_SET"} else "neutral", "items": early_items},
        {"number": "08", "id": "permission", "title": "是否允许进入操作计划", "tone": permission_tone, "items": permission_items},
        {"number": "09", "id": "observations", "title": "下一步观察清单", "tone": "neutral", "items": [_item("rule", item) for item in observations]},
        {"number": "10", "id": "conclusion", "title": "复盘结论", "tone": permission_tone, "items": [_item("judgment", conclusion), _item("risk", "本结果是规则审查，不是买卖命令，也不替用户作出最终决定")]},
    ]

    return {
        "meta": {
            "symbol": symbol,
            "stock_name": stock_name,
            "timeframe": timeframe,
            "review_date": str(review_date),
            "data_date": quality["latest_date"],
            "market_context": market_context,
            "screening_context": screening_context or None,
        },
        "summary": {
            "technical_status": signal.status,
            "technical_label": STATUS_LABELS[signal.status],
            "candidate_priority": signal.priority["level"],
            "candidate_priority_label": signal.priority["label"],
            "candidate_group_pass_count": signal.group_pass_count,
            "selection_status": selection.status,
            "selection_label": SELECTION_LABELS[selection.status],
            "selection_blocker_count": len(selection_blockers),
            "permission": permission,
            "permission_tone": permission_tone,
            "position_pct": round(position_pct, 4),
            "planned_position_pct": round(planned_position_pct, 4),
            "planned_add_amount": round(planned_add_amount, 2),
            "projected_position_pct": round(projected_position_pct, 4),
            "position_level": classify_position(position_pct),
            "emotion_hit_count": len(emotion_hits),
            "early_trigger_count": len(early_triggers),
            "data_blocker_count": len(data_blockers),
            "plan_gap_count": len(plan_gaps),
            "target_status": target_plan["status"],
            "target_label": target_plan["label"],
            "fundamental_status": fundamentals["status"],
            "personal_data_status": "DEMO" if personal_data_contains_demo else "READY" if use_local_records else "NOT_USED",
            "conclusion": conclusion,
        },
        "candidate_groups": signal.groups,
        "target_plan": target_plan,
        "fundamentals": fundamentals,
        "sections": sections,
        "chart": _chart_rows(indicator_df),
        "condition_labels": CONDITION_LABELS,
        "disclaimer": "本面板用于交易规则审查，不构成投资建议。",
    }
