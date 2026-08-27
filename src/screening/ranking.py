from __future__ import annotations

import math
from typing import Any


RANKING_VERSION = "priority-strength-v1"
DEFAULT_TOP_N = 5


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _rule_ref(definition: dict[str, Any]) -> str:
    return f"{definition['id']}@{int(definition.get('version', 1))}"


def _evidence_ref(evidence: dict[str, Any]) -> str:
    return f"{evidence.get('rule_id')}@{int(evidence.get('rule_version', 1))}"


def _is_primary_candidate(row: dict[str, Any]) -> bool:
    return (
        str(row.get("selection_status") or "").upper() == "PASS"
        and str(row.get("technical_status") or "").upper() == "BUY_CANDIDATE"
    )


def _threshold_margin(evidence: dict[str, Any] | None) -> float | None:
    """Return a comparable signed threshold margin when the evidence exposes one.

    The direction comes only from an explicit numeric comparison. Rules expressed as
    opaque booleans or functions such as ``between`` deliberately receive no margin
    instead of an invented notion of "better".
    """

    if not evidence:
        return None
    margins: list[float] = []
    for comparison in evidence.get("comparisons") or []:
        left = _number(comparison.get("left"))
        right = _number(comparison.get("right"))
        if left is None or right is None:
            continue
        operator = str(comparison.get("operator") or "")
        scale = max(abs(right), 1.0)
        if operator in {">", ">="}:
            margins.append((left - right) / scale)
        elif operator in {"<", "<="}:
            margins.append((right - left) / scale)
        elif operator == "==":
            margins.append(-abs(left - right) / scale)
    return min(margins) if margins else None


def _percentile_map(values: dict[str, float | None]) -> dict[str, float]:
    numeric = sorted(value for value in values.values() if value is not None)
    if not numeric:
        return {key: 0.5 for key in values}
    if len(numeric) == 1:
        only = numeric[0]
        return {key: 0.5 if value is None or value == only else 0.5 for key, value in values.items()}

    positions: dict[float, float] = {}
    index = 0
    while index < len(numeric):
        end = index
        while end + 1 < len(numeric) and numeric[end + 1] == numeric[index]:
            end += 1
        positions[numeric[index]] = ((index + end) / 2) / (len(numeric) - 1)
        index = end + 1
    return {
        key: 0.5 if value is None else positions[value]
        for key, value in values.items()
    }


def _evidence_by_ref(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        _evidence_ref(item): item
        for item in row.get("rule_evidence") or []
        if isinstance(item, dict) and item.get("rule_id")
    }


def _stage_scores(
    candidates: list[dict[str, Any]],
    definitions: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    refs = [_rule_ref(definition) for definition in definitions]
    evidence_maps = {
        str(row.get("symbol")): _evidence_by_ref(row)
        for row in candidates
    }
    percentiles: dict[str, dict[str, float]] = {}
    margins: dict[str, dict[str, float | None]] = {}
    for ref in refs:
        values = {
            symbol: _threshold_margin(evidence.get(ref))
            for symbol, evidence in evidence_maps.items()
        }
        margins[ref] = values
        percentiles[ref] = _percentile_map(values)

    results: dict[str, dict[str, Any]] = {}
    for row in candidates:
        symbol = str(row.get("symbol"))
        evidence_map = evidence_maps[symbol]
        details: list[dict[str, Any]] = []
        matched = 0
        has_gap = False
        sort_vector: list[tuple[int, float]] = []
        for index, (ref, definition) in enumerate(zip(refs, definitions)):
            priority = index + 1
            evidence = evidence_map.get(ref)
            status = str((evidence or {}).get("status") or "DATA_GAP").upper()
            passed = status == "PASS"
            matched += int(passed)
            has_gap = has_gap or status in {"DATA_GAP", "ERROR", "SKIPPED"}
            percentile = percentiles[ref].get(symbol, 0.5)
            sort_vector.append((int(passed), percentile))
            raw_margin = margins[ref].get(symbol)
            details.append(
                {
                    "priority": priority,
                    "rule_ref": ref,
                    "name": definition.get("name") or definition.get("id"),
                    "status": status,
                    "comparable_strength": raw_margin is not None,
                    "threshold_margin": (
                        None if raw_margin is None else round(raw_margin, 8)
                    ),
                    "strength_percentile": round(percentile * 100, 1),
                    "strength_explanation": (
                        "按该规则明确比较式的门槛余量，在本批次一级候选中换算百分位。"
                        if raw_margin is not None
                        else "该规则没有可安全比较的数值方向，本批次按同分处理。"
                    ),
                }
            )
        results[symbol] = {
            "matched": matched,
            "total": len(refs),
            "ratio": round(matched / len(refs), 4) if refs else None,
            "has_gap": has_gap,
            "details": details,
            "sort_vector": tuple(sort_vector),
        }
    return results


def apply_candidate_ranking(
    rows: list[dict[str, Any]],
    rule_set: dict[str, Any],
    resolved_rule_set: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Annotate rows with frozen two-stage status, transparent score and rank."""

    primary_definitions = list(resolved_rule_set.get("scored") or [])
    secondary_definitions = list(resolved_rule_set.get("secondary") or [])
    secondary_config = rule_set.get("secondary") or {}
    simple_two_stage = str(rule_set.get("editor_mode") or "") == "simple_all"
    secondary_minimum = (
        len(secondary_definitions)
        if simple_two_stage
        else int(secondary_config.get("minimum_match") or 0)
    )
    top_n = int((rule_set.get("ranking") or {}).get("top_n") or DEFAULT_TOP_N)
    candidates = [row for row in rows if _is_primary_candidate(row)]
    primary_scores = _stage_scores(candidates, primary_definitions)
    secondary_scores = _stage_scores(candidates, secondary_definitions)

    for row in rows:
        primary_candidate = _is_primary_candidate(row)
        row["primary_candidate"] = primary_candidate
        row["final_candidate"] = False
        row["secondary_qualified"] = False
        row["primary_priority_score"] = None
        row["secondary_priority_score"] = None
        row["ranking_score"] = None
        row["primary_rank_position"] = None
        row["rank_position"] = None
        row["is_top_candidate"] = False
        row["ranking_version"] = RANKING_VERSION
        row["ranking_formula"] = (
            "先按一级规则列表顺序逐条比较，通过状态优先，再比较可比门槛余量；"
            "排序分是该顺序结果在本批次一级候选中的相对百分位，二级规则只过滤"
        )
        row["ranking_evidence"] = {"primary": [], "secondary": []}
        if not primary_candidate:
            row["secondary_status"] = "SKIPPED"
            row["secondary_matched_rule_count"] = 0
            row["secondary_rule_count"] = len(secondary_definitions)
            row["secondary_score_ratio"] = None
            continue

        symbol = str(row.get("symbol"))
        primary = primary_scores[symbol]
        row["ranking_evidence"]["primary"] = primary["details"]
        if secondary_definitions:
            secondary = secondary_scores[symbol]
            if secondary["has_gap"]:
                secondary_status = "DATA_GAP"
            elif secondary["matched"] >= secondary_minimum:
                secondary_status = "PASS"
            else:
                secondary_status = "FAIL"
            secondary_qualified = secondary_status == "PASS"
            row["secondary_matched_rule_count"] = secondary["matched"]
            row["secondary_rule_count"] = secondary["total"]
            row["secondary_score_ratio"] = secondary["ratio"]
            row["ranking_evidence"]["secondary"] = secondary["details"]
        else:
            secondary_status = "INACTIVE"
            secondary_qualified = True
            row["secondary_matched_rule_count"] = 0
            row["secondary_rule_count"] = 0
            row["secondary_score_ratio"] = None
        row["secondary_status"] = secondary_status
        row["secondary_qualified"] = secondary_qualified
        row["final_candidate"] = secondary_qualified

    def primary_order_key(row: dict[str, Any]) -> tuple[Any, ...]:
        vector = primary_scores[str(row.get("symbol"))]["sort_vector"]
        priority_vector = [
            value
            for passed, percentile in vector
            for value in (-passed, -percentile)
        ]
        return (
            *priority_vector,
            str(row.get("symbol") or ""),
        )

    primary_order = sorted(
        candidates,
        key=primary_order_key,
    )
    for position, row in enumerate(primary_order, start=1):
        row["primary_rank_position"] = position

    # “排序分”只是上述字典序结果在本批次一级候选池中的相对百分位。
    # 完全相同的规则向量保持同分，股票代码只用于稳定显示顺序。
    total_primary = len(primary_order)
    index = 0
    while index < total_primary:
        vector = primary_scores[str(primary_order[index].get("symbol"))]["sort_vector"]
        end = index
        while end + 1 < total_primary:
            next_vector = primary_scores[
                str(primary_order[end + 1].get("symbol"))
            ]["sort_vector"]
            if next_vector != vector:
                break
            end += 1
        average_index = (index + end) / 2
        score = round((total_primary - average_index) / total_primary * 100, 1)
        for score_index in range(index, end + 1):
            primary_order[score_index]["primary_priority_score"] = score
            primary_order[score_index]["ranking_score"] = score
        index = end + 1

    refined = sorted(
        (row for row in candidates if row.get("secondary_qualified")),
        key=lambda row: (
            int(row.get("primary_rank_position") or 10**9),
            str(row.get("symbol") or ""),
        ),
    )
    for position, row in enumerate(refined, start=1):
        row["rank_position"] = position
        row["is_top_candidate"] = position <= top_n

    return {
        "ranking_version": RANKING_VERSION,
        "ranking_mode": "priority_strength",
        "ranking_formula": (
            "先按一级规则列表顺序逐条比较，通过状态优先，再比较可比门槛余量；"
            "排序分是该顺序结果在本批次一级候选中的相对百分位，二级规则只过滤"
        ),
        "primary_candidate_count": len(candidates),
        "secondary_enabled": bool(secondary_definitions),
        "secondary_rule_count": len(secondary_definitions),
        "secondary_minimum_match": secondary_minimum,
        "secondary_match_mode": "ALL" if simple_two_stage else "LEGACY_MINIMUM_MATCH",
        "secondary_pass_count": len(refined),
        "secondary_filtered_count": sum(
            row.get("secondary_status") == "FAIL" for row in candidates
        ),
        "secondary_data_gap_count": sum(
            row.get("secondary_status") == "DATA_GAP" for row in candidates
        ),
        "final_candidate_count": len(refined),
        "top_n": top_n,
        "top_candidate_count": min(top_n, len(refined)),
        "overflow_candidate_count": max(0, len(refined) - top_n),
    }
