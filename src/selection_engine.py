from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass
class SelectionResult:
    status: str
    passed: list[str]
    failed: list[str]
    blockers: list[str]
    metrics: dict[str, Any]
    notes: str
    data_gaps: list[str]


def evaluate_selection_snapshot(
    symbol: str,
    stock_name: str,
    latest_close: float | None,
    latest_volume: float | None,
    avg_amount_20d: float | None,
    avg_amount_available: bool,
    rules: dict[str, Any],
) -> SelectionResult:
    selection = rules.get("selection_rules", {})
    notes = str(selection.get("notes", "") or "").strip()
    if not selection.get("enabled", True):
        return SelectionResult("DISABLED", [], [], [], {}, notes, [])

    passed: list[str] = []
    failed: list[str] = []
    data_gaps: list[str] = []
    market = symbol.rsplit(".", 1)[-1].upper() if "." in symbol else ""
    allowed_markets = set(selection.get("allowed_markets", ["SH", "SZ", "BJ"]))

    if market in allowed_markets:
        passed.append(f"市场 {market} 位于候选范围")
    else:
        failed.append(f"市场 {market or '未知'} 不在选股范围")

    normalized_name = stock_name.upper().replace(" ", "")
    is_st = normalized_name.startswith("ST") or normalized_name.startswith("*ST")
    if selection.get("exclude_st", True) and is_st:
        failed.append("股票名称命中 ST 排除规则")
    elif selection.get("exclude_st", True):
        passed.append("未命中 ST 排除规则")

    if latest_volume is None:
        data_gaps.append("最新成交量缺失")
    elif selection.get("exclude_suspended", True) and latest_volume <= 0:
        failed.append("最后交易日成交量为 0，命中停牌或无成交排除规则")
    elif selection.get("exclude_suspended", True):
        passed.append("最后交易日存在成交量")

    min_price = float(selection.get("min_price", 0) or 0)
    max_price = float(selection.get("max_price", 0) or 0)
    if latest_close is None:
        data_gaps.append("最新价格缺失")
    else:
        if min_price and latest_close < min_price:
            failed.append(f"收盘价 {latest_close:.2f} 低于选股下限 {min_price:.2f}")
        elif min_price:
            passed.append(f"收盘价不低于 {min_price:.2f}")
        if max_price and latest_close > max_price:
            failed.append(f"收盘价 {latest_close:.2f} 高于选股上限 {max_price:.2f}")
        elif max_price:
            passed.append(f"收盘价不高于 {max_price:.2f}")

    min_avg_amount = float(selection.get("min_avg_amount_20d", 0) or 0)
    if min_avg_amount:
        if not avg_amount_available or avg_amount_20d is None:
            data_gaps.append("缺少完整的 20 日成交额，无法核对 20 日平均成交额门槛")
        elif avg_amount_20d < min_avg_amount:
            failed.append(f"20 日平均成交额 {avg_amount_20d:.0f} 低于下限 {min_avg_amount:.0f}")
        else:
            passed.append(f"20 日平均成交额不低于 {min_avg_amount:.0f}")

    status = "FAIL" if failed else "DATA_GAP" if data_gaps else "PASS"
    blockers = [*failed, *data_gaps]
    return SelectionResult(
        status=status,
        passed=passed,
        failed=failed,
        blockers=blockers,
        metrics={
            "market": market,
            "latest_close": None if latest_close is None else round(latest_close, 4),
            "latest_volume": None if latest_volume is None else round(latest_volume, 4),
            "avg_amount_20d": None if avg_amount_20d is None else round(avg_amount_20d, 2),
        },
        notes=notes,
        data_gaps=data_gaps,
    )


def evaluate_selection(
    market_df: pd.DataFrame,
    symbol: str,
    stock_name: str,
    rules: dict[str, Any],
) -> SelectionResult:
    latest = market_df.iloc[-1]
    latest_close = float(latest["close"]) if pd.notna(latest.get("close")) else None
    latest_volume = float(latest["volume"]) if pd.notna(latest.get("volume")) else None
    avg_amount_20d = None
    avg_amount_available = False
    if "amount" in market_df.columns:
        amounts = pd.to_numeric(market_df["amount"], errors="coerce").tail(20)
        avg_amount_available = amounts.notna().sum() >= min(20, len(market_df))
        if avg_amount_available:
            avg_amount_20d = float(amounts.mean())
    return evaluate_selection_snapshot(
        symbol=symbol,
        stock_name=stock_name,
        latest_close=latest_close,
        latest_volume=latest_volume,
        avg_amount_20d=avg_amount_20d,
        avg_amount_available=avg_amount_available,
        rules=rules,
    )
