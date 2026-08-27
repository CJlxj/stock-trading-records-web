from __future__ import annotations

from datetime import date
import importlib.util
import math
from typing import Any, Callable

import pandas as pd

from src.io_loader import validate_symbol


class FundamentalDataError(RuntimeError):
    pass


def provider_status() -> dict[str, Any]:
    available = importlib.util.find_spec("akshare") is not None
    return {
        "provider": "AKShare",
        "available": available,
        "message": (
            "可读取公司概况和公开财务指标，仍需核对报告期与原始公告。"
            if available
            else "当前 ai_runtime_env 未安装 AKShare；可以按面板步骤手工录入来源和指标。"
        ),
    }


def _normalized(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "").replace("_", "")


def _column(frame: pd.DataFrame, aliases: list[str]) -> str | None:
    normalized = {_normalized(column): str(column) for column in frame.columns}
    for alias in aliases:
        if _normalized(alias) in normalized:
            return normalized[_normalized(alias)]
    for column in frame.columns:
        name = _normalized(column)
        if any(_normalized(alias) in name for alias in aliases):
            return str(column)
    return None


def _number(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip().replace(",", "").replace("%", "")
    if not text or text in {"--", "-", "nan", "None"}:
        return None
    try:
        parsed = float(text)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _latest_financial_row(frame: pd.DataFrame) -> tuple[pd.Series, str | None]:
    if frame is None or frame.empty:
        raise FundamentalDataError("公开财务指标返回空数据。")
    date_column = _column(frame, ["报告期", "报告日期", "reportdate", "日期"])
    if date_column is None:
        return frame.iloc[0], None
    working = frame.copy()
    working["__date"] = pd.to_datetime(working[date_column], errors="coerce")
    working = working.sort_values("__date", ascending=False, na_position="last")
    row = working.iloc[0]
    parsed = row.get("__date")
    return row, None if pd.isna(parsed) else str(parsed.date())


def _row_value(row: pd.Series, aliases: list[str]) -> Any:
    frame = pd.DataFrame([row.to_dict()])
    column = _column(frame, aliases)
    return None if column is None else row.get(column)


def _profile_value(frame: pd.DataFrame, aliases: list[str]) -> str:
    if frame is None or frame.empty:
        return ""
    column = _column(frame, aliases)
    if column is None:
        return ""
    value = frame.iloc[0].get(column)
    return "" if value is None or pd.isna(value) else str(value).strip()


def fetch_fundamental_data(
    symbol: str,
    analysis_fetcher: Callable[..., pd.DataFrame] | None = None,
    profile_fetcher: Callable[..., pd.DataFrame] | None = None,
) -> dict[str, Any]:
    normalized_symbol = validate_symbol(symbol)
    code = normalized_symbol.split(".", 1)[0]
    provider_version = None
    if analysis_fetcher is None or profile_fetcher is None:
        if importlib.util.find_spec("akshare") is None:
            raise FundamentalDataError(
                "当前 ai_runtime_env 未安装 AKShare，无法联网读取基本面；请切换为手工录入。"
            )
        import akshare as ak

        provider_version = getattr(ak, "__version__", None)
        if analysis_fetcher is None:
            analysis_fetcher = ak.stock_financial_analysis_indicator_em
        if profile_fetcher is None:
            profile_fetcher = ak.stock_profile_cninfo

    try:
        analysis = analysis_fetcher(symbol=code, start_year=str(date.today().year - 3))
    except TypeError:
        analysis = analysis_fetcher(symbol=code)
    except Exception as exc:
        raise FundamentalDataError(f"公开财务指标读取失败：{exc}") from exc

    try:
        profile = profile_fetcher(symbol=code)
    except Exception:
        profile = pd.DataFrame()

    row, as_of_date = _latest_financial_row(analysis)
    metrics = {
        "revenue_growth": _number(
            _row_value(row, ["营业总收入同比增长率", "营业收入同比增长率", "totaloperaterevetz"])
        ),
        "profit_growth": _number(
            _row_value(row, ["归属净利润同比增长率", "净利润同比增长率", "parentnetprofittz"])
        ),
        "roe": _number(_row_value(row, ["加权净资产收益率", "净资产收益率", "roejq"])),
        "debt_ratio": _number(_row_value(row, ["资产负债率", "zcfzl"])),
        "gross_margin": _number(_row_value(row, ["销售毛利率", "毛利率", "xsmll"])),
        "pe_ttm": None,
        "pb": None,
    }
    return {
        "symbol": normalized_symbol,
        "source_type": "online",
        "source_name": "AKShare · 东方财富财务指标 / 巨潮公司概况",
        "source_url": "https://github.com/akfamily/akshare",
        "as_of_date": as_of_date or str(date.today()),
        "industry": _profile_value(profile, ["所属行业", "行业"]),
        "business_summary": _profile_value(profile, ["主营业务", "经营范围"]),
        "metrics": metrics,
        "provider": "AKShare",
        "provider_version": provider_version,
        "notice": "联网结果是二次整理数据，正式审查前仍应核对交易所或公司原始公告。",
    }


def review_fundamental_payload(
    payload: dict[str, Any],
    reference_date: date,
    max_age_days: int = 200,
) -> dict[str, Any]:
    source_type = str(payload.get("fundamental_source_type") or "none")
    source_name = str(payload.get("fundamental_source") or "").strip()
    source_url = str(payload.get("fundamental_source_url") or "").strip()
    summary = str(payload.get("fundamental_summary") or "").strip()
    industry = str(payload.get("fundamental_industry") or "").strip()
    raw_date = payload.get("fundamental_as_of")
    parsed_date = pd.to_datetime(raw_date, errors="coerce") if raw_date else pd.NaT
    as_of_date = None if pd.isna(parsed_date) else parsed_date.date()
    metrics = {
        "revenue_growth": _number(payload.get("fundamental_revenue_growth")),
        "profit_growth": _number(payload.get("fundamental_profit_growth")),
        "roe": _number(payload.get("fundamental_roe")),
        "debt_ratio": _number(payload.get("fundamental_debt_ratio")),
        "gross_margin": _number(payload.get("fundamental_gross_margin")),
        "pe_ttm": _number(payload.get("fundamental_pe_ttm")),
        "pb": _number(payload.get("fundamental_pb")),
    }

    missing: list[str] = []
    warnings: list[str] = []
    facts: list[str] = []
    if source_type == "none":
        missing.append("未选择基本面资料来源")
    if source_type != "none" and not source_name:
        missing.append("缺少资料名称或来源机构")
    if source_type != "none" and as_of_date is None:
        missing.append("缺少资料截止日期")
    if source_type != "none" and not summary:
        missing.append("缺少主营业务或核心逻辑摘要")
    if source_type == "online" and not source_url:
        warnings.append("联网资料没有保存原始链接")
    if as_of_date is not None:
        age_days = (reference_date - as_of_date).days
        if age_days < 0:
            warnings.append("基本面资料日期晚于本次审查日期")
        elif age_days > max_age_days:
            warnings.append(f"基本面资料距审查日 {age_days} 天，可能需要更新")
    else:
        age_days = None

    if industry:
        facts.append(f"所属行业：{industry}")
    if summary:
        facts.append(f"业务与逻辑摘要：{summary}")
    metric_labels = {
        "revenue_growth": "营业收入同比",
        "profit_growth": "净利润同比",
        "roe": "ROE",
        "debt_ratio": "资产负债率",
        "gross_margin": "毛利率",
        "pe_ttm": "PE(TTM)",
        "pb": "PB",
    }
    for key, value in metrics.items():
        if value is None:
            continue
        suffix = "%" if key not in {"pe_ttm", "pb"} else ""
        facts.append(f"{metric_labels[key]}：{value:.2f}{suffix}")

    if missing:
        status = "MISSING" if source_type == "none" else "PARTIAL"
    elif warnings:
        status = "WARNING"
    else:
        status = "READY"
    return {
        "status": status,
        "source_type": source_type,
        "source_name": source_name,
        "source_url": source_url,
        "as_of_date": None if as_of_date is None else str(as_of_date),
        "age_days": age_days,
        "industry": industry,
        "summary": summary,
        "metrics": metrics,
        "facts": facts,
        "missing": missing,
        "warnings": warnings,
    }
