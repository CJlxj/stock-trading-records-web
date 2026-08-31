from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import yaml

from src.rules.registry import RuleRegistry


# 出厂规则定义随代码走：出厂框架由项目发布了哪些规则决定，与任何实例的运行目录无关。
# 刻意不叫 PROJECT_ROOT——它只用于读版本化的 rules/catalog/builtin/，
# 绝不用于读实例数据（用户规则库、行情、成交、历史）。读实例数据的一律显式传根。
SHIPPED_RULES_ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=None)
def _shipped_scored_catalog(root_key: str) -> tuple[dict[str, Any], ...]:
    # The legacy default framework is defined by the rules the project ships,
    # not by whatever the user's library currently holds — editing or deleting
    # a library rule must not silently redefine the shipped default.
    return tuple(
        item
        for item in RuleRegistry(root_key).factory_definitions()
        if item.get("kind") == "scored"
    )


def candidate_rule_catalog(
    shipped_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """出厂候选规则定义。

    惰性读取并按根缓存：import 本模块不再做任何文件 I/O，
    因此不需要靠改写模块全局来让它读别的目录。
    """
    key = str(Path(shipped_root) if shipped_root is not None else SHIPPED_RULES_ROOT)
    return [dict(item) for item in _shipped_scored_catalog(key)]


def candidate_catalog_by_id(
    shipped_root: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in candidate_rule_catalog(shipped_root)}


DEFAULT_ACTIVE_RULES = [
    "close_above_ma20",
    "close_above_ma60",
    "ma5_above_ma10",
    "ma20_slope_positive",
    "volume_ratio_above_1_3",
    "price_volume_confirmed",
    "macd_hist_improving",
    "rsi_healthy",
    "not_over_extended",
    "atr_not_high",
]


@dataclass
class SignalResult:
    status: str
    score: int
    max_score: int
    score_ratio: float
    active_rule_count: int
    matched_rule_count: int
    active_rules: list[str]
    risk_score: int
    passed: list[str]
    failed: list[str]
    risks: list[str]
    latest: dict[str, Any]
    explanation: str
    groups: dict[str, dict[str, Any]]
    group_pass_count: int
    priority: dict[str, Any]
    framework_diagnostics: dict[str, Any]
    rule_results: list[dict[str, Any]]
    required_passed: bool
    veto_triggered: bool
    blocking_rule_error: bool


def load_rules(path: str = "config/strategy_rules.yaml") -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def safe_bool(value: Any) -> bool:
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    return bool(value)


def active_candidate_rules(rules: dict[str, Any]) -> list[str]:
    configured = rules.get("candidate_framework", {}).get("active_rules")
    if not isinstance(configured, list):
        return list(DEFAULT_ACTIVE_RULES)
    catalog = candidate_catalog_by_id()
    unique: list[str] = []
    for value in configured:
        rule_id = str(value)
        if rule_id in catalog and rule_id not in unique:
            unique.append(rule_id)
    return unique or list(DEFAULT_ACTIVE_RULES)


def candidate_framework_diagnostics(
    rules: dict[str, Any],
    *,
    catalog_by_id: dict[str, dict[str, Any]] | None = None,
    active_rules_override: list[str] | None = None,
    framework_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    catalog = catalog_by_id or candidate_catalog_by_id()
    framework = framework_override or rules.get("candidate_framework", {})
    configured = (
        active_rules_override
        if active_rules_override is not None
        else framework.get("active_rules")
    )
    raw_rules = (
        list(configured)
        if isinstance(configured, list)
        else list(DEFAULT_ACTIVE_RULES)
    )
    normalized = [str(value) for value in raw_rules]
    active_rules = [
        rule_id
        for index, rule_id in enumerate(normalized)
        if rule_id in catalog and rule_id not in normalized[:index]
    ]
    invalid_rules = sorted({rule_id for rule_id in normalized if rule_id not in catalog})
    duplicate_rules = sorted(
        rule_id for rule_id, count in Counter(normalized).items() if count > 1
    )
    minimum = int(framework.get("min_active_rules", 1))
    maximum = int(framework.get("max_active_rules", 50))
    min_group_passes = int(
        framework.get("min_group_passes", framework.get("minimum_groups", 1))
    )
    pass_ratio = float(framework.get("pass_score_ratio", framework.get("pass_ratio", 0.65)))
    watch_ratio = float(
        framework.get("watch_score_ratio", framework.get("near_ratio", 0.45))
    )
    high_ratio = float(framework.get("priority_high_ratio", max(pass_ratio, 0.80)))
    group_pass_ratio = float(framework.get("group_pass_ratio", 0.50))

    group_counts = Counter(catalog[rule_id]["group"] for rule_id in active_rules)
    family_counts = Counter(catalog[rule_id]["family"] for rule_id in active_rules)
    selected_groups = sorted(group_counts)
    errors: list[str] = []
    warnings: list[str] = []
    if invalid_rules:
        errors.append("存在无法识别的候选规则：" + "、".join(invalid_rules))
    if duplicate_rules:
        errors.append("候选规则不能重复：" + "、".join(duplicate_rules))
    if len(active_rules) < minimum:
        errors.append(f"至少选择 {minimum} 条候选规则。")
    if len(active_rules) > maximum:
        errors.append(f"最多选择 {maximum} 条候选规则。")
    if len(selected_groups) < min(min_group_passes, 3):
        errors.append(f"候选组合至少覆盖 {min(min_group_passes, 3)} 个不同规则组。")
    if min_group_passes > len(selected_groups):
        errors.append("最少通过规则组不能高于当前已覆盖的规则组数。")
    if not 0 < watch_ratio < pass_ratio <= high_ratio <= 1:
        errors.append("比例必须满足：近似候选 < 最终候选 <= 高优先级 <= 100%。")
    if not 0 < group_pass_ratio <= 1:
        errors.append("组内通过比例必须位于 0% 到 100% 之间。")
    concentrated_families = sorted(
        family for family, count in family_counts.items() if count > 2
    )
    if concentrated_families:
        warnings.append(
            "同类规则较集中：" + "、".join(concentrated_families) + "；可能重复计算同一信息。"
        )
    if pass_ratio > 0.75:
        warnings.append("最终候选比例高于 75%，可能长期出现零候选。")
    status = "INVALID" if errors else "WARNING" if warnings else "READY"
    group_labels = [
        next(
            (
                item.get("group_label", group)
                for item in catalog.values()
                if item.get("group") == group
            ),
            group,
        )
        for group in selected_groups
    ]
    return {
        "status": status,
        "active_rule_count": len(active_rules),
        "minimum_active_rules": minimum,
        "maximum_active_rules": maximum,
        "selected_group_count": len(selected_groups),
        "selected_groups": selected_groups,
        "selected_group_labels": group_labels,
        "group_counts": dict(group_counts),
        "errors": errors,
        "warnings": warnings,
        "pass_score_ratio": pass_ratio,
        "watch_score_ratio": watch_ratio,
        "priority_high_ratio": high_ratio,
        "group_pass_ratio": group_pass_ratio,
        "formula": (
            f"系统基础检查 AND 候选规则达到 {pass_ratio:.0%} "
            f"AND 至少 {min_group_passes} 组达到组内门槛"
        ),
        "validation_status": "STRUCTURE_ONLY",
        "validation_message": "结构检查不能证明策略有效；规则仍需样本外或滚动验证。",
    }


def latest_valid_rows(df: pd.DataFrame) -> pd.DataFrame:
    needed = [
        "date",
        "open",
        "close",
        "volume",
        "ma5",
        "ma10",
        "ma20",
        "ma60",
        "macd_hist",
        "rsi14",
        "volume_ratio",
        "vol_ma5",
        "vol_ma20",
        "obv_slope_10",
        "distance_from_ma20",
        "atr_pct",
        "ma20_slope",
        "ret_1d",
        "roc20",
        "gap_pct",
        "high_20",
        "low_20",
        "drawdown_from_20d_high",
    ]
    missing_columns = [column for column in needed if column not in df.columns]
    if missing_columns:
        raise ValueError("缺少指标字段：" + "、".join(missing_columns) + "。")
    if len(df) < 2:
        raise ValueError("有效指标行不足。请导入更多历史数据，建议至少 60 行以上。")
    missing_latest = [
        column for column in needed if pd.isna(df.iloc[-1].get(column))
    ]
    if missing_latest:
        raise ValueError(
            "目标收盘日指标未完整写入：" + "、".join(missing_latest) + "。"
        )
    return df


def _group_results(
    evidences: list[dict[str, Any]],
    definitions: list[dict[str, Any]],
    pass_ratio: float,
) -> tuple[dict[str, dict[str, Any]], int]:
    by_id = {item["id"]: item for item in definitions}
    evidence_by_id = {item["rule_id"]: item for item in evidences}
    groups: dict[str, dict[str, Any]] = {}
    for definition in definitions:
        group = groups.setdefault(
            definition["group"],
            {
                "label": definition.get("group_label", definition["group"]),
                "rule_ids": [],
            },
        )
        group["rule_ids"].append(definition["id"])
    results: dict[str, dict[str, Any]] = {}
    passed_groups = 0
    for key, group in groups.items():
        ids = group["rule_ids"]
        scored = [
            evidence_by_id[rule_id]
            for rule_id in ids
            if rule_id in evidence_by_id
            and evidence_by_id[rule_id]["status"] in {"PASS", "FAIL"}
        ]
        score = sum(item["status"] == "PASS" for item in scored)
        maximum = len(scored)
        ratio = score / maximum if maximum else 0.0
        has_gap = any(
            evidence_by_id.get(rule_id, {}).get("status") in {"DATA_GAP", "ERROR"}
            for rule_id in ids
        )
        status = (
            "DATA_GAP"
            if has_gap
            else "PASS"
            if maximum and ratio >= pass_ratio
            else "PARTIAL"
            if score
            else "FAIL"
        )
        if status == "PASS":
            passed_groups += 1
        results[key] = {
            "label": group["label"],
            "status": status,
            "score": score,
            "max_score": maximum,
            "ratio": round(ratio, 4),
            "passed": [
                rule_id
                for rule_id in ids
                if evidence_by_id.get(rule_id, {}).get("status") == "PASS"
            ],
            "failed": [
                rule_id
                for rule_id in ids
                if evidence_by_id.get(rule_id, {}).get("status") == "FAIL"
            ],
        }
    return results, passed_groups


def _candidate_priority(
    status: str,
    score_ratio: float,
    risk_score: int,
    group_pass_count: int,
    rules: dict[str, Any],
    market_context: str,
) -> dict[str, Any]:
    high_ratio = float(
        rules.get("candidate_framework", {}).get("priority_high_ratio", 0.80)
    )
    context = (
        market_context
        if market_context in {"supportive", "neutral", "defensive"}
        else "unknown"
    )
    context_adjustment = {
        "supportive": 5,
        "neutral": 0,
        "defensive": -10,
        "unknown": -5,
    }[context]
    priority_score = round(
        max(0, min(100, score_ratio * 100 - risk_score * 8 + context_adjustment))
    )
    if status in {"RISK_REDUCE", "STOP_TRIGGER"}:
        level, label = "RISK", "风险优先"
    elif status in {"NO_TRADE", "DATA_GAP"}:
        level, label = "HOLD", "暂缓"
    elif status == "WATCH":
        level, label = "P3", "近似候选"
    elif (
        score_ratio >= high_ratio
        and group_pass_count >= 3
        and risk_score <= 1
        and context in {"supportive", "neutral"}
    ):
        level, label = "P1", "优先复核"
    else:
        level, label = "P2", "进入复核"
    context_note = {
        "supportive": "市场环境偏支持",
        "neutral": "市场环境中性",
        "defensive": "市场环境偏防守",
        "unknown": "市场环境尚未核对",
    }[context]
    return {
        "level": level,
        "label": label,
        "score": int(priority_score),
        "market_context": context,
        "context_note": context_note,
    }


def _legacy_parameter_overrides(
    definition: dict[str, Any], rules: dict[str, Any]
) -> dict[str, Any]:
    thresholds = rules.get("thresholds", {})
    mapping = {
        "volume_ratio_buy": thresholds.get("volume_ratio_buy"),
        "rsi_low": thresholds.get("rsi_low"),
        "rsi_high": thresholds.get("rsi_high"),
        "rsi_overbought": thresholds.get("rsi_overbought"),
        "extension_from_ma20_max": thresholds.get("extension_from_ma20_max"),
        "atr_risk_pct_high": thresholds.get("atr_risk_pct_high"),
    }
    return {
        name: mapping[name]
        for name in definition.get("params_schema", {})
        if mapping.get(name) is not None
    }


def evaluate_latest(
    df: pd.DataFrame,
    rules: Optional[dict[str, Any]] = None,
    entry_price: Optional[float] = None,
    market_context: str = "unknown",
    *,
    registry: RuleRegistry,
    rule_set: dict[str, Any] | None = None,
    resolved_rule_set: dict[str, list[dict[str, Any]]] | None = None,
) -> SignalResult:
    # registry 必须由调用方显式给出：它读的是**某个实例**的可编辑规则库
    # （rules/catalog/user/、rules/registry_state.yaml）。以前这里在缺省时
    # 回退到代码所在目录，等于悄悄拿开发者自己的规则库去评估别人的实例，
    # 测试也只能靠改写模块全局来绕开。现在缺参数会直接报错，不会静默走错根。
    if rules is None:
        rules = load_rules()
    valid = latest_valid_rows(df)
    latest = valid.iloc[-1]
    explicit_rule_set = rule_set is not None

    if rule_set is None:
        scored_definitions = [
            registry.get(rule_id) for rule_id in active_candidate_rules(rules)
        ]
        required_definitions: list[dict[str, Any]] = []
        veto_definitions: list[dict[str, Any]] = []
        secondary_definitions: list[dict[str, Any]] = []
        pass_ratio = float(rules["candidate_framework"].get("pass_score_ratio", 0.65))
        watch_ratio = float(rules["candidate_framework"].get("watch_score_ratio", 0.45))
        min_group_passes = int(rules["candidate_framework"].get("min_group_passes", 3))
        group_pass_ratio = float(
            rules["candidate_framework"].get("group_pass_ratio", 0.5)
        )
    else:
        resolved = resolved_rule_set or {
            "required": [],
            "veto": [],
            "scored": [],
            "secondary": [],
        }
        required_definitions = resolved.get("required", [])
        veto_definitions = resolved.get("veto", [])
        scored_definitions = resolved.get("scored", [])
        secondary_definitions = resolved.get("secondary", [])
        pass_ratio = float(rule_set["scored"]["pass_ratio"])
        watch_ratio = float(rule_set["near_policy"]["score_ratio"])
        min_group_passes = int(rule_set["group_policy"]["minimum_groups"])
        group_pass_ratio = float(rule_set["group_policy"]["group_pass_ratio"])

    catalog = {item["id"]: item for item in scored_definitions}
    framework = {
        "active_rules": [item["id"] for item in scored_definitions],
        "min_active_rules": 1,
        "max_active_rules": 50,
        "pass_ratio": pass_ratio,
        "near_ratio": watch_ratio,
        "minimum_groups": min_group_passes,
        "group_pass_ratio": group_pass_ratio,
        "priority_high_ratio": max(
            pass_ratio,
            float(rules.get("candidate_framework", {}).get("priority_high_ratio", 0.8)),
        ),
    }
    diagnostics = candidate_framework_diagnostics(
        rules,
        catalog_by_id=catalog,
        active_rules_override=[item["id"] for item in scored_definitions],
        framework_override=framework,
    )
    if diagnostics["errors"]:
        raise ValueError("候选规则组合无效：" + "；".join(diagnostics["errors"]))

    data_date = str(latest["date"])
    evidences: list[dict[str, Any]] = []
    for stage, definitions in (
        ("required", required_definitions),
        ("veto", veto_definitions),
        ("primary", scored_definitions),
    ):
        for definition in definitions:
            evidence = registry.evaluate(
                definition,
                valid,
                params=_legacy_parameter_overrides(definition, rules),
                data_date=data_date,
            )
            evidence["screening_stage"] = stage
            evidence["rule_ref"] = f"{definition['id']}@{int(definition['version'])}"
            evidences.append(evidence)
    evidence_by_id = {item["rule_id"]: item for item in evidences}
    scored_evidence = [evidence_by_id[item["id"]] for item in scored_definitions]
    passed = [item["rule_id"] for item in scored_evidence if item["status"] == "PASS"]
    failed = [item["rule_id"] for item in scored_evidence if item["status"] == "FAIL"]
    score = len(passed)
    evaluated_count = sum(
        item["status"] in {"PASS", "FAIL"} for item in scored_evidence
    )
    score_ratio = score / evaluated_count if evaluated_count else 0.0
    groups, group_pass_count = _group_results(
        scored_evidence, scored_definitions, group_pass_ratio
    )
    required_passed = all(
        evidence_by_id[item["id"]]["status"] == "PASS"
        for item in required_definitions
    )
    veto_triggered = any(
        evidence_by_id[item["id"]]["status"] == "PASS" for item in veto_definitions
    )
    blocking_rule_error = any(
        item["status"] in {"DATA_GAP", "ERROR"} for item in evidences
    )

    thresholds = rules["thresholds"]
    risks: list[str] = []
    risk_score = 0
    if latest["close"] < latest["ma20"]:
        risks.append("收盘价低于 MA20，短期趋势偏弱")
        risk_score += 1
    if latest["ma5"] < latest["ma10"]:
        risks.append("MA5 低于 MA10，短期修复不强")
        risk_score += 1
    if latest["macd_hist"] < 0:
        risks.append("MACD 柱子仍为负，上行动能未确认")
        risk_score += 1
    if latest["rsi14"] > thresholds["rsi_overbought"]:
        risks.append("RSI 高于过热阈值，短线追高风险增加")
        risk_score += 1
    if latest["distance_from_ma20"] > thresholds["extension_from_ma20_max"]:
        risks.append("价格偏离 MA20 过远，追高风险增加")
        risk_score += 1
    if latest["atr_pct"] > thresholds["atr_risk_pct_high"]:
        risks.append("ATR 占价格比例较高，波动风险较大")
        risk_score += 1
    if entry_price is not None:
        pnl_pct = latest["close"] / entry_price - 1
        if pnl_pct <= rules["signal_rules"]["stop_trigger"]["default_stop_loss_pct"]:
            risks.append(f"相对成本价回撤 {pnl_pct:.2%}，触发默认止损线")
            risk_score += 3

    if entry_price is not None and any("触发默认止损线" in risk for risk in risks):
        status = "STOP_TRIGGER"
    elif (
        not explicit_rule_set
        and risk_score >= rules["signal_rules"]["risk_reduce"]["min_risk_score"]
    ):
        status = "RISK_REDUCE"
    elif blocking_rule_error:
        status = "DATA_GAP"
    elif (
        required_passed
        and not veto_triggered
        and score_ratio >= pass_ratio
        and group_pass_count >= min_group_passes
    ):
        status = "BUY_CANDIDATE"
    elif (
        required_passed
        and not veto_triggered
        and score_ratio >= watch_ratio
    ):
        status = "WATCH"
    else:
        status = "NO_TRADE"

    # 二级规则只对已经形成一级候选的股票执行。它的结果由候选排序层
    # 独立解释为 PASS / FAIL / DATA_GAP，不反向改写这里的一级状态。
    if status == "BUY_CANDIDATE":
        for definition in secondary_definitions:
            evidence = registry.evaluate(
                definition,
                valid,
                params=_legacy_parameter_overrides(definition, rules),
                data_date=data_date,
            )
            evidence["screening_stage"] = "secondary"
            evidence["rule_ref"] = (
                f"{definition['id']}@{int(definition['version'])}"
            )
            evidences.append(evidence)

    range_width = latest["high_20"] - latest["low_20"]
    range_position = (
        (latest["close"] - latest["low_20"]) / range_width if range_width else 0.5
    )
    latest_small = {
        "date": str(latest["date"]),
        "close": round(float(latest["close"]), 4),
        "volume": round(float(latest["volume"]), 4),
        "ma5": round(float(latest["ma5"]), 4),
        "ma10": round(float(latest["ma10"]), 4),
        "ma20": round(float(latest["ma20"]), 4),
        "ma60": round(float(latest["ma60"]), 4),
        "high_20": round(float(latest["high_20"]), 4),
        "low_20": round(float(latest["low_20"]), 4),
        "high_60": (
            None
            if pd.isna(latest.get("high_60"))
            else round(float(latest["high_60"]), 4)
        ),
        "macd_hist": round(float(latest["macd_hist"]), 6),
        "rsi14": round(float(latest["rsi14"]), 4),
        "volume_ratio": round(float(latest["volume_ratio"]), 4),
        "distance_from_ma20": round(float(latest["distance_from_ma20"]), 4),
        "drawdown_from_20d_high": round(
            float(latest["drawdown_from_20d_high"]), 4
        ),
        "roc20": round(float(latest["roc20"]), 4),
        "gap_pct": round(float(latest["gap_pct"]), 4),
        "atr14": round(float(latest["atr14"]), 4),
        "atr_pct": round(float(latest["atr_pct"]), 4),
        "range_position_20d": round(float(range_position), 4),
    }
    priority = _candidate_priority(
        status, score_ratio, risk_score, group_pass_count, rules, market_context
    )
    explanation = build_explanation(
        status,
        latest_small,
        risks,
        score,
        evaluated_count,
        score_ratio,
        risk_score,
        groups,
        priority,
    )
    return SignalResult(
        status=status,
        score=score,
        max_score=evaluated_count,
        score_ratio=round(score_ratio, 4),
        active_rule_count=len(scored_definitions),
        matched_rule_count=score,
        active_rules=[item["id"] for item in scored_definitions],
        risk_score=int(risk_score),
        passed=passed,
        failed=failed,
        risks=risks,
        latest=latest_small,
        explanation=explanation,
        groups=groups,
        group_pass_count=group_pass_count,
        priority=priority,
        framework_diagnostics=diagnostics,
        rule_results=evidences,
        required_passed=required_passed,
        veto_triggered=veto_triggered,
        blocking_rule_error=blocking_rule_error,
    )


def build_explanation(
    status: str,
    latest: dict[str, Any],
    risks: list[str],
    score: int,
    max_score: int,
    score_ratio: float,
    risk_score: int,
    groups: dict[str, dict[str, Any]],
    priority: dict[str, Any],
) -> str:
    lines = [
        f"规则状态：{status}",
        f"候选规则：{score}/{max_score}（{score_ratio:.0%}）；风险得分：{risk_score}",
        f"日期：{latest['date']}",
        f"收盘价：{latest['close']}",
        f"成交量/20日均量：{latest['volume_ratio']}",
        f"RSI14：{latest['rsi14']}",
        f"MACD柱：{latest['macd_hist']}",
        f"距离MA20：{latest['distance_from_ma20']:.2%}",
        "",
        "规则组：",
    ]
    for group in groups.values():
        lines.append(
            f"- {group['label']}：{group['score']}/{group['max_score']}（{group['status']}）"
        )
    lines.extend(["", "白话解释："])
    if status == "BUY_CANDIDATE":
        lines.append("多组技术条件共同进入最终候选，但仍需要单股计划、仓位和情绪规则许可。")
    elif status == "WATCH":
        lines.append("组合达到近似候选区，但尚未跨过最终候选门槛，不能自动放宽规则。")
    elif status == "DATA_GAP":
        lines.append("至少一条规则缺少数据或执行异常，本次不能进入正式候选。")
    elif status == "RISK_REDUCE":
        lines.append("风险条件较多，当前优先核对持仓和事前退出规则。")
    elif status == "STOP_TRIGGER":
        lines.append("出现默认止损条件，需要核对它是否属于事前写明的退出规则。")
    else:
        lines.append("当前技术规则组合不足，不进入交易计划讨论。")
    if risks:
        lines.extend(["", "主要风险：", *(f"- {risk}" for risk in risks)])
    return "\n".join(lines)
