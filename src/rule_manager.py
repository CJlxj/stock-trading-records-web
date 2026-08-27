from __future__ import annotations

from datetime import datetime
import math
from pathlib import Path
import threading
from typing import Any

import yaml

from src.signal_engine import (
    active_candidate_rules,
    candidate_framework_diagnostics,
    candidate_rule_catalog,
)


class RuleValidationError(ValueError):
    pass


ALLOWED_MARKETS = {"SH", "SZ", "BJ"}
_WRITE_LOCK = threading.Lock()


def _rules_path(project_root: str | Path) -> Path:
    return Path(project_root) / "config" / "strategy_rules.yaml"


def _read(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _number(value: Any, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise RuleValidationError(f"{label}必须是数字。") from None
    if not math.isfinite(parsed):
        raise RuleValidationError(f"{label}必须是有限数字。")
    return parsed


def _ratio(value: Any, label: str, *, allow_zero: bool = False) -> float:
    parsed = _number(value, label)
    lower = 0 if allow_zero else 0.000001
    if not lower <= parsed <= 1:
        raise RuleValidationError(f"{label}必须位于 0% 到 100% 之间。")
    return parsed


def _negative_ratio(value: Any, label: str) -> float:
    parsed = _number(value, label)
    if not -0.50 <= parsed < 0:
        raise RuleValidationError(f"{label}必须是 -50% 到 0% 之间的负数。")
    return parsed


def _note(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if len(text) > 2000:
        raise RuleValidationError(f"{label}不能超过 2000 个字符。")
    return text


def _public_rules(raw: dict[str, Any], path: Path) -> dict[str, Any]:
    selection = raw.get("selection_rules", {})
    data_quality = raw.get("data_quality", {})
    position = raw.get("position_rules", {})
    trade = raw.get("trade_rules", {})
    buy = trade.get("buy", {})
    sell = trade.get("sell", {})
    stop = raw.get("signal_rules", {}).get("stop_trigger", {})
    thresholds = raw.get("thresholds", {})
    candidate = raw.get("candidate_framework", {})
    diagnostics = candidate_framework_diagnostics(raw)

    return {
        "selection": {
            "enabled": bool(selection.get("enabled", True)),
            "allowed_markets": list(selection.get("allowed_markets", ["SH", "SZ", "BJ"])),
            "exclude_st": bool(selection.get("exclude_st", True)),
            "exclude_suspended": bool(selection.get("exclude_suspended", True)),
            "minimum_history_rows": int(data_quality.get("minimum_rows", 60)),
            "max_day_age_days": int(data_quality.get("max_age_days", {}).get("day", 7)),
            "min_price": float(selection.get("min_price", 0)),
            "max_price": float(selection.get("max_price", 0)),
            "min_avg_amount_20d": float(selection.get("min_avg_amount_20d", 0)),
            "notes": str(selection.get("notes", "")),
        },
        "technical": {
            "scoring_mode": "equal_vote",
            "active_rules": active_candidate_rules(raw),
            "catalog": candidate_rule_catalog(),
            "min_active_rules": int(candidate.get("min_active_rules", 4)),
            "max_active_rules": int(candidate.get("max_active_rules", 12)),
            "pass_score_ratio": float(candidate.get("pass_score_ratio", 0.65)),
            "watch_score_ratio": float(candidate.get("watch_score_ratio", 0.45)),
            "priority_high_ratio": float(candidate.get("priority_high_ratio", 0.80)),
            "min_group_passes": int(candidate.get("min_group_passes", 3)),
            "group_pass_ratio": float(candidate.get("group_pass_ratio", 0.50)),
            "groups": candidate.get("groups", {}),
            "diagnostics": diagnostics,
            "volume_ratio_buy": float(thresholds.get("volume_ratio_buy", 1.3)),
            "rsi_low": float(thresholds.get("rsi_low", 45)),
            "rsi_high": float(thresholds.get("rsi_high", 70)),
            "extension_from_ma20_max": float(thresholds.get("extension_from_ma20_max", 0.10)),
        },
        "buy": {
            "required_technical_status": "BUY_CANDIDATE",
            "require_written_plan": True,
            "require_stop_loss": True,
            "require_max_loss": True,
            "require_sell_condition": True,
            "require_fundamental_source": bool(buy.get("require_fundamental_source", True)),
            "min_reward_risk": float(buy.get("min_reward_risk", 2.0)),
            "normal_position_pct_max": float(position.get("normal_position_pct_max", 0.30)),
            "high_risk_position_pct": float(position.get("high_risk_position_pct", 0.40)),
            "heavy_position_pct": float(position.get("heavy_position_pct", 0.50)),
            "max_single_order_account_pct": float(position.get("single_add_pct_max", 0.10)),
            "max_single_order_plan_pct": float(buy.get("max_single_order_plan_pct", 0.40)),
            "notes": str(buy.get("notes", "")),
        },
        "sell": {
            "require_predefined_trigger": True,
            "prohibit_emotion_exit": True,
            "require_sell_condition": True,
            "default_stop_loss_pct": float(stop.get("default_stop_loss_pct", -0.08)),
            "trailing_stop_from_high_pct": float(stop.get("trailing_stop_from_high_pct", -0.10)),
            "notes": str(sell.get("notes", "")),
        },
        "meta": {
            "path": str(path),
            "updated_at": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
        },
    }


def get_editable_rules(project_root: str | Path) -> dict[str, Any]:
    path = _rules_path(project_root)
    return _public_rules(_read(path), path)


def save_editable_rules(project_root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    path = _rules_path(project_root)
    current = _read(path)
    selection_input = payload.get("selection") or {}
    technical_input = payload.get("technical") or {}
    buy_input = payload.get("buy") or {}
    sell_input = payload.get("sell") or {}
    if not all(
        isinstance(value, dict)
        for value in [selection_input, technical_input, buy_input, sell_input]
    ):
        raise RuleValidationError("规则提交格式不正确。")

    markets = [str(value).upper() for value in selection_input.get("allowed_markets", [])]
    if not markets or any(market not in ALLOWED_MARKETS for market in markets):
        raise RuleValidationError("选股市场至少选择一个，并且只能是 SH、SZ 或 BJ。")
    minimum_rows = int(_number(selection_input.get("minimum_history_rows"), "最少历史行数"))
    if not 60 <= minimum_rows <= 2000:
        raise RuleValidationError("最少历史行数必须位于 60 到 2000 之间。")
    min_price = _number(selection_input.get("min_price", 0), "最低价格")
    max_price = _number(selection_input.get("max_price", 0), "最高价格")
    min_amount = _number(selection_input.get("min_avg_amount_20d", 0), "20 日平均成交额")
    if min_price < 0 or max_price < 0 or min_amount < 0:
        raise RuleValidationError("价格和成交额筛选值不能为负数。")
    if max_price and max_price <= min_price:
        raise RuleValidationError("最高价格必须大于最低价格；填写 0 表示不限制。")

    normal_pct = _ratio(buy_input.get("normal_position_pct_max"), "正常仓位上限")
    high_pct = _ratio(buy_input.get("high_risk_position_pct"), "高风险仓位阈值")
    heavy_pct = _ratio(buy_input.get("heavy_position_pct"), "重仓阈值")
    if not normal_pct < high_pct < heavy_pct:
        raise RuleValidationError("仓位阈值必须满足：正常上限 < 高风险阈值 < 重仓阈值。")
    max_account_order = _ratio(
        buy_input.get("max_single_order_account_pct"), "单次金额占账户上限"
    )
    max_plan_order = _ratio(
        buy_input.get("max_single_order_plan_pct"), "单次金额占计划仓位上限"
    )
    if max_account_order > normal_pct:
        raise RuleValidationError("单次金额占账户上限不能高于正常仓位上限。")
    active_rules_input = technical_input.get("active_rules")
    if not isinstance(active_rules_input, list):
        raise RuleValidationError("候选规则必须使用列表格式提交。")
    active_rules = [str(value) for value in active_rules_input]
    volume_ratio = _number(technical_input.get("volume_ratio_buy"), "成交量倍数")
    if not 0.1 <= volume_ratio <= 10:
        raise RuleValidationError("成交量倍数必须位于 0.1 到 10 之间。")
    rsi_low = _number(technical_input.get("rsi_low"), "RSI 下限")
    rsi_high = _number(technical_input.get("rsi_high"), "RSI 上限")
    if not 0 <= rsi_low < rsi_high <= 100:
        raise RuleValidationError("RSI 必须满足 0 <= 下限 < 上限 <= 100。")
    extension_max = _ratio(
        technical_input.get("extension_from_ma20_max"), "偏离 MA20 上限", allow_zero=True
    )
    min_group_passes = int(_number(technical_input.get("min_group_passes", 3), "最少通过规则组"))
    if not 1 <= min_group_passes <= 4:
        raise RuleValidationError("最少通过规则组必须位于 1 到 4 之间。")
    group_pass_ratio = _ratio(
        technical_input.get("group_pass_ratio", 0.50), "组内通过比例"
    )
    watch_score_ratio = _ratio(
        technical_input.get("watch_score_ratio", 0.45), "近似候选比例"
    )
    pass_score_ratio = _ratio(
        technical_input.get("pass_score_ratio", 0.65), "最终候选比例"
    )
    priority_high_ratio = _ratio(
        technical_input.get("priority_high_ratio", 0.80), "P1 比例"
    )
    if not watch_score_ratio < pass_score_ratio <= priority_high_ratio:
        raise RuleValidationError("比例必须满足：近似候选 < 最终候选 <= P1。")
    if pass_score_ratio > 0.90:
        raise RuleValidationError("最终候选比例不能高于 90%；过严组合应先做样本外验证。")
    min_reward_risk = _number(buy_input.get("min_reward_risk", 2.0), "计划盈亏比下限")
    if not 0.5 <= min_reward_risk <= 10:
        raise RuleValidationError("计划盈亏比下限必须位于 0.5 到 10 之间。")

    default_stop = _negative_ratio(sell_input.get("default_stop_loss_pct"), "默认止损比例")
    trailing_stop = _negative_ratio(
        sell_input.get("trailing_stop_from_high_pct"), "移动止盈回撤比例"
    )

    selection = {
        "enabled": bool(selection_input.get("enabled", True)),
        "allowed_markets": sorted(set(markets), key=["SH", "SZ", "BJ"].index),
        "exclude_st": bool(selection_input.get("exclude_st", True)),
        "exclude_suspended": bool(selection_input.get("exclude_suspended", True)),
        "min_price": min_price,
        "max_price": max_price,
        "min_avg_amount_20d": min_amount,
        "notes": _note(selection_input.get("notes"), "选股规则备注"),
    }
    buy = {
        "required_technical_status": "BUY_CANDIDATE",
        "require_written_plan": True,
        "require_stop_loss": True,
        "require_max_loss": True,
        "require_sell_condition": True,
        "require_fundamental_source": bool(buy_input.get("require_fundamental_source", True)),
        "min_reward_risk": min_reward_risk,
        "max_single_order_plan_pct": max_plan_order,
        "notes": _note(buy_input.get("notes"), "买入规则备注"),
    }
    sell = {
        "require_predefined_trigger": True,
        "prohibit_emotion_exit": True,
        "require_sell_condition": True,
        "notes": _note(sell_input.get("notes"), "卖出规则备注"),
    }

    framework_limits = current.get("candidate_framework", {})
    candidate_update = {
        "scoring_mode": "equal_vote",
        "active_rules": active_rules,
        "min_active_rules": int(framework_limits.get("min_active_rules", 4)),
        "max_active_rules": int(framework_limits.get("max_active_rules", 12)),
        "pass_score_ratio": pass_score_ratio,
        "watch_score_ratio": watch_score_ratio,
        "priority_high_ratio": priority_high_ratio,
        "min_group_passes": min_group_passes,
        "group_pass_ratio": group_pass_ratio,
    }
    diagnostic_rules = dict(current)
    diagnostic_rules["candidate_framework"] = {
        **framework_limits,
        **candidate_update,
    }
    diagnostics = candidate_framework_diagnostics(diagnostic_rules)
    if diagnostics["errors"]:
        raise RuleValidationError("；".join(diagnostics["errors"]))

    with _WRITE_LOCK:
        raw = _read(path)
        raw["selection_rules"] = selection
        raw.setdefault("data_quality", {})["minimum_rows"] = minimum_rows
        raw.setdefault("position_rules", {}).update(
            {
                "normal_position_pct_max": normal_pct,
                "high_risk_position_pct": high_pct,
                "heavy_position_pct": heavy_pct,
                "single_add_pct_max": max_account_order,
            }
        )
        raw.setdefault("trade_rules", {})["buy"] = buy
        raw["trade_rules"]["sell"] = sell
        raw.setdefault("candidate_framework", {}).update(candidate_update)
        raw.setdefault("thresholds", {}).update(
            {
                "volume_ratio_buy": volume_ratio,
                "rsi_low": rsi_low,
                "rsi_high": rsi_high,
                "extension_from_ma20_max": extension_max,
            }
        )
        raw.setdefault("signal_rules", {}).setdefault("stop_trigger", {}).update(
            {
                "default_stop_loss_pct": default_stop,
                "trailing_stop_from_high_pct": trailing_stop,
            }
        )

        temporary = path.with_suffix(".yaml.tmp")
        temporary.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
        temporary.replace(path)

    return _public_rules(raw, path)
