from __future__ import annotations

from typing import Any


TARGET_LABELS = {
    "NOT_SET": "尚未设置",
    "INCOMPLETE": "信息不完整",
    "INVALID": "价格关系不成立",
    "UNFAVORABLE": "盈亏结构不足",
    "REVIEW": "需要补充依据",
    "CONSISTENT": "结构可复核",
}


def evaluate_target_plan(
    current_price: float,
    stop_loss_price: float | None,
    target_price: float | None,
    latest: dict[str, Any],
    min_reward_risk: float = 2.0,
) -> dict[str, Any]:
    facts: list[str] = []
    risks: list[str] = []
    if stop_loss_price is None and target_price is None:
        return {
            "status": "NOT_SET",
            "label": TARGET_LABELS["NOT_SET"],
            "facts": [],
            "risks": ["没有同时设置止损价格和目标价格，无法校验目标合理性"],
            "reward_pct": None,
            "risk_pct": None,
            "reward_risk": None,
            "target_atr": None,
            "stop_atr": None,
        }
    if stop_loss_price is None or target_price is None:
        missing = "止损价格" if stop_loss_price is None else "目标价格"
        return {
            "status": "INCOMPLETE",
            "label": TARGET_LABELS["INCOMPLETE"],
            "facts": [],
            "risks": [f"缺少{missing}，无法同时比较收益空间和风险空间"],
            "reward_pct": None,
            "risk_pct": None,
            "reward_risk": None,
            "target_atr": None,
            "stop_atr": None,
        }

    reward = target_price - current_price
    risk = current_price - stop_loss_price
    if reward <= 0:
        risks.append("目标价格不高于当前价格，不符合新增仓位的目标定义")
    if risk <= 0:
        risks.append("止损价格不低于当前价格，无法形成向下风险区间")
    if risks:
        return {
            "status": "INVALID",
            "label": TARGET_LABELS["INVALID"],
            "facts": [],
            "risks": risks,
            "reward_pct": reward / current_price,
            "risk_pct": risk / current_price,
            "reward_risk": None,
            "target_atr": None,
            "stop_atr": None,
        }

    reward_pct = reward / current_price
    risk_pct = risk / current_price
    reward_risk = reward / risk
    facts.append(
        f"目标空间 {reward_pct:.1%}，止损空间 {risk_pct:.1%}，计划盈亏比 {reward_risk:.2f}"
    )

    high_20 = latest.get("high_20")
    high_60 = latest.get("high_60")
    if high_20 and target_price <= float(high_20) * 1.01:
        facts.append(f"目标价格位于近 20 日高点 {float(high_20):.2f} 附近或以内")
    elif high_60 and target_price <= float(high_60) * 1.02:
        facts.append(
            f"目标价格高于近 20 日高点，但仍位于近 60 日高点 {float(high_60):.2f} 附近或以内"
        )
    elif high_60:
        risks.append(
            f"目标价格明显高于近 60 日高点 {float(high_60):.2f}，需要额外的突破或基本面依据"
        )

    atr = float(latest.get("atr14") or 0)
    target_atr = reward / atr if atr > 0 else None
    stop_atr = risk / atr if atr > 0 else None
    if target_atr is not None:
        facts.append(f"目标距离约 {target_atr:.1f} 个 ATR，止损距离约 {stop_atr:.1f} 个 ATR")
        if target_atr > 5:
            risks.append("目标距离超过 5 个 ATR，当前数据无法给出充分的波动依据")
        if stop_atr < 0.5:
            risks.append("止损距离小于 0.5 个 ATR，可能容易被正常波动触发")
        elif stop_atr > 3:
            risks.append("止损距离超过 3 个 ATR，单次风险区间偏宽")

    if reward_risk < 1:
        status = "UNFAVORABLE"
        risks.append("目标收益空间小于止损风险空间")
    elif reward_risk + 1e-9 < min_reward_risk:
        status = "REVIEW"
        risks.append(f"计划盈亏比低于事前下限 {min_reward_risk:.2f}")
    elif risks:
        status = "REVIEW"
    else:
        status = "CONSISTENT"

    return {
        "status": status,
        "label": TARGET_LABELS[status],
        "facts": facts,
        "risks": risks,
        "reward_pct": round(reward_pct, 6),
        "risk_pct": round(risk_pct, 6),
        "reward_risk": round(reward_risk, 4),
        "target_atr": None if target_atr is None else round(target_atr, 4),
        "stop_atr": None if stop_atr is None else round(stop_atr, 4),
    }
