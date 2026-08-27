from __future__ import annotations

import ast
from copy import deepcopy
import json
from pathlib import Path
import re
import threading
from typing import Any

from src.rules.expression_parser import (
    ALLOWED_FUNCTIONS,
    ExpressionSyntaxError,
    parse_expression,
)
from src.rules.technical_fields import (
    TECHNICAL_FIELD_LOOKBACKS,
    technical_field_contract,
)
from src.rules.references import describe_rule_references, rule_references
from src.rules.registry import RuleRegistry, RuleRegistryError
from src.rules.schema import RULE_ID_PATTERN
from src.rules.storage import atomic_write_text, canonical_hash
from src.rules import tags
from src.rules.tags import RuleTagError
from src.rules.version_store import (
    LEGACY_EDITOR_MODE,
    SIMPLE_EDITOR_MODE,
    RuleSetError,
    RuleSetStore,
)


class SimpleRuleEditorError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "RULE_EDITOR_INVALID",
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        # Extra structured context the panel renders next to the message,
        # e.g. exactly where a rule is still in use.
        self.details = details or {}


_EDITOR_LOCK = threading.RLock()
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
_RULE_REF_PATTERN = re.compile(
    r"^(?:[a-z][a-z0-9_]*\.)?[a-z][a-z0-9_]{1,80}@\d+$"
)
_DRAFT_ID_PATTERN = re.compile(r"[^a-z0-9_]+")
_PERIOD_FUNCTIONS = {
    "ma",
    "ema",
    "rsi",
    "atr",
    "prev",
    "highest",
    "lowest",
    "pct_change",
}


def _text(
    value: Any,
    label: str,
    *,
    maximum: int,
    required: bool = True,
) -> str:
    parsed = str(value or "").strip()
    if required and not parsed:
        raise SimpleRuleEditorError(
            f"{label}不能为空。",
            field=label,
        )
    if len(parsed) > maximum:
        raise SimpleRuleEditorError(
            f"{label}不能超过 {maximum} 个字符。",
            field=label,
        )
    return parsed


def _rule_ref(value: Any, label: str = "规则引用") -> tuple[str, int]:
    ref = str(value or "").strip()
    if not _RULE_REF_PATTERN.fullmatch(ref):
        raise SimpleRuleEditorError(f"{label}不合法。", field=label)
    rule_id, version_text = ref.rsplit("@", 1)
    return rule_id, int(version_text)


def ensure_rule_catalog_seeded(project_root: str | Path) -> dict[str, Any]:
    """Import the factory rules once during service startup."""

    registry = RuleRegistry(project_root)
    result = registry.ensure_seeded()
    _bind_imported_tags(project_root, result.get("imported") or [])
    return {
        "seeded": bool(result.get("seeded")),
        "imported_count": int(result.get("imported_count", 0)),
    }


def import_default_rules(project_root: str | Path) -> dict[str, Any]:
    """Put back whichever factory rules and tags are currently missing.

    Additive on purpose: an id that still exists is skipped whole, so a rule
    the user has edited keeps their version and only genuine gaps are filled.
    """

    with _EDITOR_LOCK:
        registry = RuleRegistry(project_root)
        result = registry.import_default_rules()
        imported = result.get("imported") or []
        tag_labels = _bind_imported_tags(project_root, imported)
        return {
            "imported_count": len(imported),
            "imported_rules": [
                {"id": str(item["id"]), "name": str(item.get("name") or item["id"])}
                for item in imported
            ],
            "tag_labels": tag_labels,
        }


def _bind_imported_tags(
    project_root: str | Path,
    imported: list[dict[str, Any]],
) -> list[str]:
    """Turn each imported rule's shipped category into an ordinary tag.

    Reuses a tag that already carries the same label, so re-importing after a
    rename binds to the renamed tag instead of creating a duplicate.
    """

    labels: list[str] = []
    for definition in imported:
        label = str(
            definition.get("group_label") or definition.get("group") or ""
        ).strip()
        if not label:
            continue
        try:
            tag_id = tags.ensure_tag_label(project_root, label)
        except RuleTagError:
            continue
        tags.assign_tag(project_root, str(definition["id"]), tag_id)
        if label not in labels:
            labels.append(label)
    return labels


def _request_path(project_root: str | Path, request_id: str) -> Path:
    return (
        Path(project_root)
        / "history"
        / "rule_editor_requests"
        / f"{request_id}.json"
    )


def _custom_rule_request_path(
    project_root: str | Path,
    request_id: str,
) -> Path:
    return (
        Path(project_root)
        / "history"
        / "custom_rule_requests"
        / f"{request_id}.json"
    )


def _load_receipt(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _draft_rule_id(value: Any) -> str:
    draft_id = _DRAFT_ID_PATTERN.sub(
        "_",
        str(value or "").lower(),
    ).strip("_")
    if not draft_id:
        raise SimpleRuleEditorError(
            "新增规则缺少稳定标识，请重新打开编辑窗口。",
            field="draft_id",
        )
    return f"user.rule_{draft_id[:52]}"


def _lookback(parsed: Any) -> int:
    required = max(
        [TECHNICAL_FIELD_LOOKBACKS.get(name, 1) for name in parsed.names],
        default=1,
    )
    for node in ast.walk(parsed.tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in _PERIOD_FUNCTIONS:
            continue
        period_index = 3 if node.func.id == "atr" else 1
        if len(node.args) <= period_index:
            continue
        period_node = node.args[period_index]
        if not isinstance(period_node, ast.Constant) or not isinstance(
            period_node.value,
            (int, float),
        ):
            continue
        period = int(period_node.value)
        if period < 1 or period > 5_000:
            raise SimpleRuleEditorError(
                "规则周期必须位于 1 到 5000 个交易日之间。",
                field="expression",
            )
        required = max(required, period + (1 if node.func.id == "prev" else 0))
    return required


def simple_rule_editor_contract() -> dict[str, Any]:
    return {
        "fields": technical_field_contract(),
        "functions": sorted(ALLOWED_FUNCTIONS),
        "examples": [
            {
                "label": "收盘价高于 20 日均线",
                "expression": "close > ma20",
            },
            {
                "label": "RSI 不高于 70",
                "expression": "rsi14 <= 70",
            },
            {
                "label": "量比不低于 1.3",
                "expression": "volume_ratio >= 1.3",
            },
        ],
        "notice": "只支持列出的行情字段、比较运算和安全函数，不执行任意代码。",
    }


def _definition_payload(
    registry: RuleRegistry,
    item: dict[str, Any],
    request_id: str,
    index: int,
) -> tuple[str, dict[str, Any]]:
    name = _text(item.get("name"), "规则名称", maximum=80)
    description = _text(
        item.get("description"),
        "规则说明",
        maximum=500,
        required=False,
    ) or f"{name}；仅用于收盘后生成观察候选，不构成买卖指令。"
    expression = _text(item.get("expression"), "执行条件", maximum=2_000)
    try:
        parsed = parse_expression(expression)
    except ExpressionSyntaxError as exc:
        raise SimpleRuleEditorError(
            str(exc),
            code=exc.code,
            field="expression",
        ) from None
    if not parsed.names:
        raise SimpleRuleEditorError(
            "执行条件至少需要引用一个行情字段。",
            field="expression",
        )

    base_ref = str(item.get("base_ref") or "").strip()
    base: dict[str, Any] | None = None
    if base_ref:
        base_id, base_version = _rule_ref(base_ref, "原规则")
        try:
            base = registry.get(base_id, base_version)
        except RuleRegistryError as exc:
            raise SimpleRuleEditorError(
                f"原规则不存在：{exc}",
                code=exc.code,
                field="base_ref",
            ) from None
        # Editing any rule produces a new version of that same rule.  The old
        # version stays on disk, so saved combinations and frozen history keep
        # resolving exactly what they were decided by.
        target_id = str(base["id"])
    else:
        target_id = _draft_rule_id(
            item.get("draft_id") or f"{request_id}_{index}"
        )

    # ``group`` stays whatever the source rule declared: the screening engine
    # reads it when it counts how many rule groups a candidate satisfied, so
    # the library's own categorisation lives in the tag store instead.
    group = str((base or {}).get("group") or "custom")
    group_label = str((base or {}).get("group_label") or group)
    family = str((base or {}).get("family") or "manual_expression")
    base_visual = (base or {}).get("visual_definition") or {}
    origin_ref = (
        base_visual.get("origin_ref")
        or base_visual.get("base_ref")
        or base_ref
        or None
    )
    payload = {
        "id": target_id,
        "name": name,
        "description": description,
        "kind": "scored",
        "group": group,
        "group_label": group_label,
        "family": family,
        "level": "自定义",
        "timeframe": "day",
        "lookback": _lookback(parsed),
        "inputs": list(parsed.names),
        "params_schema": {},
        "implementation": {
            "type": "expression",
            "expression": expression,
        },
        "missing_policy": "DATA_GAP",
        "plain_template": description,
        "tests": [],
        "visual_definition": {
            "editor": "simple_expression",
            "base_ref": base_ref or None,
            "origin_ref": origin_ref,
            "request_id": request_id,
        },
    }
    return target_id, payload


def _same_simple_definition(
    definition: dict[str, Any],
    payload: dict[str, Any],
) -> bool:
    return all(
        [
            definition.get("name") == payload.get("name"),
            definition.get("description") == payload.get("description"),
            definition.get("group") == payload.get("group"),
            definition.get("family") == payload.get("family"),
            int(definition.get("lookback", 1)) == int(payload.get("lookback", 1)),
            definition.get("inputs") == payload.get("inputs"),
            definition.get("implementation", {}).get("normalized_expression")
            == parse_expression(payload["implementation"]["expression"]).normalized,
        ]
    )


def _create_validate_rule(
    registry: RuleRegistry,
    item: dict[str, Any],
    request_id: str,
    index: int,
) -> dict[str, Any]:
    target_id, payload = _definition_payload(
        registry,
        item,
        request_id,
        index,
    )
    matches = [
        definition
        for definition in registry.versions(target_id)
        if _same_simple_definition(definition, payload)
    ] if any(
        definition.get("id") == target_id
        for definition in registry.all_versions()
    ) else []
    if matches:
        definition = max(matches, key=lambda value: int(value["version"]))
    else:
        definition = registry.create(payload)
    if definition.get("status") == "DRAFT":
        definition = registry.validate(
            definition["id"],
            int(definition["version"]),
        )
    return definition


def save_library_rule(
    project_root: str | Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Create a rule, or save a new version of an existing one, in one command.

    ``base_ref`` is what makes it an edit: the new version lands under that
    same rule id, so the library shows one rule with a history rather than a
    family of copies.  The catalog tag is applied separately from the
    definition, so re-filing a rule under a different tag does not manufacture
    a version nobody asked for.
    """

    allowed_fields = {
        "request_id",
        "name",
        "description",
        "expression",
        "base_ref",
        "tag_id",
    }
    unexpected_fields = sorted(set(payload) - allowed_fields)
    if unexpected_fields:
        raise SimpleRuleEditorError(
            "保存规则包含不支持的字段：" + "、".join(unexpected_fields) + "。",
            code="CUSTOM_RULE_FIELDS_INVALID",
        )

    request_id = str(payload.get("request_id") or "").strip()
    if not _REQUEST_ID_PATTERN.fullmatch(request_id):
        raise SimpleRuleEditorError(
            "请求标识不合法，请刷新页面后重试。",
            field="request_id",
        )
    name = _text(payload.get("name"), "规则名称", maximum=80)
    description = _text(
        payload.get("description"),
        "规则说明",
        maximum=500,
        required=False,
    ) or f"{name}；仅用于收盘后生成观察候选，不构成买卖指令。"
    expression = _text(payload.get("expression"), "执行条件", maximum=2_000)
    base_ref = str(payload.get("base_ref") or "").strip()
    tag_id = str(payload.get("tag_id") or "").strip()
    request_content = {
        "name": name,
        "description": description,
        "expression": expression,
        "base_ref": base_ref,
        "tag_id": tag_id,
    }
    request_hash = canonical_hash(request_content)
    receipt_path = _custom_rule_request_path(project_root, request_id)

    with _EDITOR_LOCK:
        receipt = _load_receipt(receipt_path)
        if receipt is not None:
            if receipt.get("request_hash") != request_hash:
                raise SimpleRuleEditorError(
                    "同一请求标识对应了不同规则内容，请重新提交。",
                    code="IDEMPOTENCY_CONFLICT",
                    field="request_id",
                )
            return {**deepcopy(receipt), "deduplicated": True}

        registry = RuleRegistry(project_root)
        definition = _create_validate_rule(
            registry,
            {
                "name": name,
                "description": description,
                "expression": expression,
                "base_ref": base_ref,
                "draft_id": request_id,
            },
            request_id,
            0,
        )
        if definition.get("status") != "ACTIVE":
            definition = registry.activate(
                definition["id"],
                int(definition["version"]),
            )
        try:
            tags.assign_tag(project_root, str(definition["id"]), tag_id)
        except RuleTagError as exc:
            raise SimpleRuleEditorError(
                str(exc),
                code=exc.code,
                field="tag_id",
            ) from None
        definition["tag_id"] = tag_id
        ref = f"{definition['id']}@{definition['version']}"
        result = {
            "request_id": request_id,
            "request_hash": request_hash,
            "deduplicated": False,
            "ref": ref,
            "rule": definition,
        }
        atomic_write_text(
            receipt_path,
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n",
        )
        return result


def delete_library_rule(
    project_root: str | Path,
    rule_id: str,
) -> dict[str, Any]:
    """Delete one rule, but only when no combination still points at it.

    Every rule in the library is treated the same — the guard is purely
    referential.  Rule-set versions stay byte-for-byte unchanged, so a rule any
    of them names must stay too; saved screening batches carry their own frozen
    copy of every rule they were decided by, so they never hold a rule hostage.
    The shared editor lock serializes this with rule and combination writes,
    which is what makes "scan, then delete" a single decision rather than a
    race.
    """

    target = str(rule_id or "").strip()
    if not RULE_ID_PATTERN.fullmatch(target):
        raise SimpleRuleEditorError(
            "规则标识不合法。",
            code="RULE_NOT_FOUND",
            field="rule_id",
        )

    with _EDITOR_LOCK:
        registry = RuleRegistry(project_root)
        try:
            versions = registry.versions(target)
        except RuleRegistryError as exc:
            raise SimpleRuleEditorError(
                f"没有找到该规则：{exc}",
                code="RULE_NOT_FOUND",
                field="rule_id",
            ) from None

        references = rule_references(project_root, target)
        if references:
            raise SimpleRuleEditorError(
                "无法删除：" + describe_rule_references(references) + "正在引用这条规则。",
                code="RULE_IN_USE",
                field="rule_id",
                details={"references": references},
            )

        name = str(
            max(versions, key=lambda item: int(item["version"])).get("name") or target
        )
        result = registry.delete(target)
        tags.drop_rule(project_root, target)
        return {**result, "name": name}


def _active_rule_ref(
    registry: RuleRegistry,
    raw_ref: Any,
    *,
    field: str = "rules",
) -> str:
    rule_id, version = _rule_ref(raw_ref)
    try:
        definition = registry.get(rule_id, version)
    except RuleRegistryError as exc:
        raise SimpleRuleEditorError(
            f"规则不存在：{exc}",
            code=exc.code,
            field=field,
        ) from None
    if definition.get("status") != "ACTIVE":
        raise SimpleRuleEditorError(
            f"规则 {definition['name']} 当前不可用，请重新打开规则页。",
            code="RULE_NOT_ACTIVE",
            field=field,
        )
    if definition.get("kind") != "scored":
        raise SimpleRuleEditorError(
            "当前页面只支持候选评分规则。",
            code="RULE_KIND_UNSUPPORTED",
            field=field,
        )
    return f"{definition['id']}@{definition['version']}"


def _lineage_key(definition: dict[str, Any]) -> str:
    visual = definition.get("visual_definition") or {}
    origin_ref = str(
        visual.get("origin_ref")
        or visual.get("base_ref")
        or ""
    ).strip()
    if origin_ref and "@" in origin_ref:
        return origin_ref.rsplit("@", 1)[0]
    return str(definition["id"])


def _rule_set_payload(
    active: dict[str, Any] | None,
    refs: list[str],
    minimum: int,
    secondary_refs: list[str] | None = None,
    secondary_minimum: int = 0,
) -> dict[str, Any]:
    secondary_refs = list(secondary_refs or [])
    pass_ratio = minimum / len(refs)
    near_ratio = (
        (minimum - 1) / len(refs)
        if minimum > 1
        else max(0.01, pass_ratio / 2)
    )
    return {
        "id": "simple_after_close",
        "name": "日常筛选规则",
        # This compatibility endpoint supports N-of-M plus a second stage.  It
        # remains explicit legacy/advanced data and is never reinterpreted as
        # the new one-layer ALL editor.
        "editor_mode": LEGACY_EDITOR_MODE,
        "base_gates": {"rules": (active or {}).get("base_gates", {}).get("rules", [])},
        "required": {"rules": (active or {}).get("required", {}).get("rules", [])},
        "veto": {"rules": (active or {}).get("veto", {}).get("rules", [])},
        "scored": {
            "mode": "equal_score",
            "rules": refs,
            "pass_ratio": pass_ratio,
            "error_policy": "block_candidate",
        },
        "secondary": {
            "mode": "minimum_match",
            "rules": secondary_refs,
            "minimum_match": secondary_minimum,
            "error_policy": "exclude_from_refined_candidates",
        },
        "ranking": {
            "mode": "priority_strength",
            "top_n": 5,
        },
        "near_policy": {
            "score_ratio": min(
                near_ratio,
                max(0.01, pass_ratio - 0.01),
            ),
            "require_no_veto": True,
        },
        "group_policy": {
            "minimum_groups": 1,
            "group_pass_ratio": 0.5,
        },
    }


def _same_active_rule_set(
    active: dict[str, Any] | None,
    refs: list[str],
    minimum: int,
    secondary_refs: list[str] | None = None,
    secondary_minimum: int = 0,
) -> bool:
    if not active or active.get("status") != "ACTIVE":
        return False
    active_refs = list(active.get("scored", {}).get("rules") or [])
    active_ratio = float(active.get("scored", {}).get("pass_ratio") or 0)
    active_secondary = active.get("secondary") or {}
    return all(
        [
            active_refs == refs,
            abs(active_ratio - minimum / len(refs)) < 1e-12,
            list(active_secondary.get("rules") or []) == list(secondary_refs or []),
            int(active_secondary.get("minimum_match") or 0) == secondary_minimum,
        ]
    )


def apply_simple_rule_editor(
    project_root: str | Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    request_id = str(payload.get("request_id") or "").strip()
    expected_rule_set_hash = str(
        payload.get("expected_rule_set_hash") or ""
    ).strip()
    if not _REQUEST_ID_PATTERN.fullmatch(request_id):
        raise SimpleRuleEditorError(
            "请求标识不合法，请刷新页面后重试。",
            field="request_id",
        )
    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, list) or not 1 <= len(raw_rules) <= 30:
        raise SimpleRuleEditorError(
            "当前方案必须包含 1 到 30 条规则。",
            field="rules",
        )
    raw_secondary_rules = payload.get("secondary_rules") or []
    if not isinstance(raw_secondary_rules, list) or len(raw_secondary_rules) > 30:
        raise SimpleRuleEditorError(
            "二级筛选规则必须是数组，且不能超过 30 条。",
            field="secondary_rules",
        )
    try:
        minimum = int(payload.get("minimum_match"))
    except (TypeError, ValueError):
        raise SimpleRuleEditorError(
            "通过门槛必须是整数。",
            field="minimum_match",
        ) from None
    if not 1 <= minimum <= len(raw_rules):
        raise SimpleRuleEditorError(
            "通过门槛不能超过当前规则数量。",
            field="minimum_match",
        )
    try:
        secondary_minimum = int(
            payload.get(
                "secondary_minimum_match",
                0 if not raw_secondary_rules else len(raw_secondary_rules),
            )
        )
    except (TypeError, ValueError):
        raise SimpleRuleEditorError(
            "二级筛选门槛必须是整数。",
            field="secondary_minimum_match",
        ) from None
    if raw_secondary_rules and not 1 <= secondary_minimum <= len(raw_secondary_rules):
        raise SimpleRuleEditorError(
            "二级筛选门槛不能超过二级规则数量。",
            field="secondary_minimum_match",
        )
    if not raw_secondary_rules and secondary_minimum != 0:
        raise SimpleRuleEditorError(
            "没有二级规则时，二级筛选门槛必须为 0。",
            field="secondary_minimum_match",
        )
    request_hash = canonical_hash(
        {
            "expected_rule_set_hash": expected_rule_set_hash,
            "minimum_match": minimum,
            "rules": raw_rules,
            "secondary_minimum_match": secondary_minimum,
            "secondary_rules": raw_secondary_rules,
        }
    )
    legacy_request_hash = canonical_hash(
        {
            "expected_rule_set_hash": expected_rule_set_hash,
            "minimum_match": minimum,
            "rules": raw_rules,
        }
    )

    receipt_path = _request_path(project_root, request_id)
    with _EDITOR_LOCK:
        receipt = _load_receipt(receipt_path)
        if receipt is not None:
            accepted_hashes = {request_hash}
            if not raw_secondary_rules:
                accepted_hashes.add(legacy_request_hash)
            if receipt.get("request_hash") not in accepted_hashes:
                raise SimpleRuleEditorError(
                    "同一请求标识对应了不同规则内容，请重新提交。",
                    code="IDEMPOTENCY_CONFLICT",
                    field="request_id",
                )
            return {**deepcopy(receipt), "deduplicated": True}

        registry = RuleRegistry(project_root)
        store = RuleSetStore(project_root)
        try:
            active_before = store.active()
        except RuleSetError as exc:
            if exc.code != "NO_ACTIVE_RULE_SET":
                raise
            active_before = None

        current_rule_set_hash = str(
            (active_before or {}).get("rule_set_hash") or ""
        )
        if (
            expected_rule_set_hash
            and expected_rule_set_hash != current_rule_set_hash
        ):
            raise SimpleRuleEditorError(
                "当前规则已在另一页面更新，请读取最新版并重新核对后再应用。",
                code="RULE_SET_CHANGED",
                field="rules",
            )

        refs: list[str] = []
        secondary_refs: list[str] = []
        lineages: set[str] = set()
        changed_rules: list[dict[str, Any]] = []
        pending_activation: list[dict[str, Any]] = []
        for index, raw_item in enumerate(raw_rules):
            if not isinstance(raw_item, dict):
                raise SimpleRuleEditorError(
                    "规则提交格式不正确。",
                    field="rules",
                )
            if raw_item.get("expression") is not None:
                definition = _create_validate_rule(
                    registry,
                    raw_item,
                    request_id,
                    index,
                )
                ref = f"{definition['id']}@{definition['version']}"
                pending_activation.append(definition)
                changed_rules.append(
                    {
                        "ref": ref,
                        "name": definition["name"],
                        "normalized_expression": definition["implementation"][
                            "normalized_expression"
                        ],
                        "base_ref": raw_item.get("base_ref"),
                    }
                )
            else:
                ref = _active_rule_ref(registry, raw_item.get("ref"))
                rule_id, version = _rule_ref(ref)
                definition = registry.get(rule_id, version)
            if ref in refs:
                raise SimpleRuleEditorError(
                    "同一规则不能重复加入当前方案。",
                    code="RULE_DUPLICATED",
                    field="rules",
                )
            lineage = _lineage_key(definition)
            if lineage in lineages:
                raise SimpleRuleEditorError(
                    "原规则和它的本人版本不能同时计分，请只保留一条。",
                    code="RULE_LINEAGE_DUPLICATED",
                    field="rules",
                )
            refs.append(ref)
            lineages.add(lineage)

        for index, raw_item in enumerate(raw_secondary_rules, start=len(raw_rules)):
            if not isinstance(raw_item, dict):
                raise SimpleRuleEditorError(
                    "二级筛选规则提交格式不正确。",
                    field="secondary_rules",
                )
            if raw_item.get("expression") is not None:
                definition = _create_validate_rule(
                    registry,
                    raw_item,
                    request_id,
                    index,
                )
                ref = f"{definition['id']}@{definition['version']}"
                pending_activation.append(definition)
                changed_rules.append(
                    {
                        "ref": ref,
                        "name": definition["name"],
                        "normalized_expression": definition["implementation"][
                            "normalized_expression"
                        ],
                        "base_ref": raw_item.get("base_ref"),
                        "stage": "secondary",
                    }
                )
            else:
                ref = _active_rule_ref(registry, raw_item.get("ref"))
                rule_id, version = _rule_ref(ref)
                definition = registry.get(rule_id, version)
            if ref in refs or ref in secondary_refs:
                raise SimpleRuleEditorError(
                    "同一规则不能同时用于一级和二级筛选。",
                    code="RULE_STAGE_DUPLICATED",
                    field="secondary_rules",
                )
            lineage = _lineage_key(definition)
            if lineage in lineages:
                raise SimpleRuleEditorError(
                    "同一规则来源不能同时用于一级和二级筛选。",
                    code="RULE_LINEAGE_DUPLICATED",
                    field="secondary_rules",
                )
            secondary_refs.append(ref)
            lineages.add(lineage)

        rule_set_payload = _rule_set_payload(
            active_before,
            refs,
            minimum,
            secondary_refs,
            secondary_minimum,
        )
        if _same_active_rule_set(
            active_before,
            refs,
            minimum,
            secondary_refs,
            secondary_minimum,
        ) and not pending_activation:
            active_rule_set = active_before
        else:
            created_set = store.create(rule_set_payload)
            store.validate(
                created_set["id"],
                int(created_set["version"]),
                allow_validated_rules=True,
            )
            for definition in pending_activation:
                if definition.get("status") != "ACTIVE":
                    registry.activate(
                        definition["id"],
                        int(definition["version"]),
                    )
            active_rule_set = store.activate(
                created_set["id"],
                int(created_set["version"]),
            )

        result = {
            "request_id": request_id,
            "request_hash": request_hash,
            "deduplicated": False,
            "rule_set": active_rule_set,
            "rule_refs": refs,
            "secondary_rule_refs": secondary_refs,
            "changed_rules": changed_rules,
            "editor_hash": canonical_hash(
                {
                    "refs": refs,
                    "minimum_match": minimum,
                    "secondary_refs": secondary_refs,
                    "secondary_minimum_match": secondary_minimum,
                }
            ),
        }
        atomic_write_text(
            receipt_path,
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n",
        )
        return result


def _named_request(
    payload: dict[str, Any],
    *,
    operation: str,
    rule_set_id: str | None,
) -> tuple[str, str, str, list[dict[str, Any]], list[dict[str, Any]], str]:
    request_id = str(payload.get("request_id") or "").strip()
    if not _REQUEST_ID_PATTERN.fullmatch(request_id):
        raise SimpleRuleEditorError(
            "请求标识不合法，请刷新页面后重试。",
            field="request_id",
        )
    editor_mode = str(payload.get("editor_mode") or SIMPLE_EDITOR_MODE)
    if editor_mode != SIMPLE_EDITOR_MODE:
        raise SimpleRuleEditorError(
            "日常组合只支持两级 ALL 规则。",
            code="RULE_SET_EDITOR_MODE_INVALID",
            field="editor_mode",
        )
    forbidden = {
        "minimum_match",
        "secondary_minimum_match",
    } & set(payload)
    if forbidden:
        raise SimpleRuleEditorError(
            "日常简单组合的一级和二级都固定为全部满足，不接受数量门槛。",
            code="RULE_SET_SIMPLE_ALL_INVALID",
            field=sorted(forbidden)[0],
        )
    name = _text(payload.get("name"), "组合名称", maximum=80)
    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, list) or not 1 <= len(raw_rules) <= 30:
        raise SimpleRuleEditorError(
            "日常组合必须包含 1 到 30 条规则。",
            field="rules",
        )
    if not all(isinstance(item, dict) for item in raw_rules):
        raise SimpleRuleEditorError("规则提交格式不正确。", field="rules")
    raw_secondary_rules = payload.get("secondary_rules") or []
    if not isinstance(raw_secondary_rules, list) or len(raw_secondary_rules) > 30:
        raise SimpleRuleEditorError(
            "二级规则必须是数组，且不能超过 30 条。",
            field="secondary_rules",
        )
    if not all(isinstance(item, dict) for item in raw_secondary_rules):
        raise SimpleRuleEditorError(
            "二级规则提交格式不正确。",
            field="secondary_rules",
        )
    legacy_conversion = payload.get("legacy_conversion")
    if legacy_conversion is not None and not isinstance(legacy_conversion, dict):
        raise SimpleRuleEditorError(
            "旧版组合转换确认格式不正确。",
            code="RULE_SET_CONVERSION_INVALID",
            field="legacy_conversion",
        )
    request_hash = canonical_hash(
        {
            "operation": operation,
            "rule_set_id": rule_set_id,
            "base_version": payload.get("base_version"),
            "expected_rule_set_hash": payload.get("expected_rule_set_hash"),
            "name": name,
            "editor_mode": editor_mode,
            "rules": raw_rules,
            "secondary_rules": raw_secondary_rules,
            "legacy_conversion": legacy_conversion,
        }
    )
    return request_id, name, raw_rules, raw_secondary_rules, request_hash


def _materialize_named_rules(
    registry: RuleRegistry,
    raw_rules: list[dict[str, Any]],
    *,
    stage: str = "primary",
    occupied_refs: set[str] | None = None,
    occupied_lineages: set[str] | None = None,
) -> tuple[list[str], set[str]]:
    """Resolve a combination's rule list to active refs.

    Combinations only ever *reference* rules.  Writing a rule is the library's
    job, so a payload carrying an inline expression is rejected rather than
    quietly creating a rule as a side effect of saving a combination.
    """

    refs: list[str] = []
    lineages: set[str] = set(occupied_lineages or set())
    unavailable_refs = set(occupied_refs or set())
    field = "secondary_rules" if stage == "secondary" else "rules"
    for raw_item in raw_rules:
        if raw_item.get("expression") is not None:
            raise SimpleRuleEditorError(
                "组合只能引用规则库里的规则，请先在规则库新增或编辑它。",
                code="RULE_SET_INLINE_RULE_UNSUPPORTED",
                field=field,
            )
        ref = _active_rule_ref(registry, raw_item.get("ref"), field=field)
        rule_id, version = _rule_ref(ref)
        definition = registry.get(rule_id, version)
        if ref in unavailable_refs:
            raise SimpleRuleEditorError(
                "同一规则不能重复加入，也不能同时用于一级和二级筛选。",
                code="RULE_STAGE_DUPLICATED",
                field=field,
            )
        if ref in refs:
            raise SimpleRuleEditorError(
                "同一规则不能重复加入当前筛选级别。",
                code="RULE_DUPLICATED",
                field=field,
            )
        lineage = _lineage_key(definition)
        if lineage in lineages:
            raise SimpleRuleEditorError(
                "同一条规则的两个版本不能同时计分，请只保留一条。",
                code="RULE_LINEAGE_DUPLICATED",
                field=field,
            )
        refs.append(ref)
        lineages.add(lineage)
    return refs, lineages


def apply_named_simple_rule_set(
    project_root: str | Path,
    payload: dict[str, Any],
    *,
    rule_set_id: str | None = None,
) -> dict[str, Any]:
    """Create or edit a named two-stage ALL rule set without activating it."""

    operation = "edit" if rule_set_id else "create"
    (
        request_id,
        name,
        raw_rules,
        raw_secondary_rules,
        request_hash,
    ) = _named_request(
        payload,
        operation=operation,
        rule_set_id=rule_set_id,
    )
    receipt_path = _request_path(project_root, request_id)
    with _EDITOR_LOCK:
        receipt = _load_receipt(receipt_path)
        if receipt is not None:
            if receipt.get("request_hash") != request_hash:
                raise SimpleRuleEditorError(
                    "同一请求标识对应了不同组合内容，请重新提交。",
                    code="IDEMPOTENCY_CONFLICT",
                    field="request_id",
                )
            return {**deepcopy(receipt), "deduplicated": True}

        store = RuleSetStore(project_root)
        if rule_set_id:
            try:
                base_version = int(payload.get("base_version"))
            except (TypeError, ValueError):
                raise SimpleRuleEditorError(
                    "编辑组合必须提供基准版本。",
                    field="base_version",
                ) from None
            expected_hash = str(payload.get("expected_rule_set_hash") or "").strip()
            if not expected_hash:
                raise SimpleRuleEditorError(
                    "编辑组合必须提供读取时的版本摘要。",
                    code="RULE_SET_HASH_REQUIRED",
                    field="expected_rule_set_hash",
                )
            detail = store.detail(rule_set_id)
            base = store.get(rule_set_id, base_version)
            RuleSetStore._assert_hash(base, expected_hash)
            if int(detail["latest_version"]) != base_version:
                raise RuleSetError(
                    "该组合已有更新版本，请从最新版继续编辑。",
                    code="RULE_SET_CHANGED",
                )
            if base.get("editor_mode") == LEGACY_EDITOR_MODE:
                # Validate the explicit semantic conversion before creating
                # any custom rule versions from this request.  A rejected
                # confirmation therefore leaves both the rule-set family and
                # the rule registry untouched.
                store.legacy_conversion_plan(
                    rule_set_id,
                    base_version=base_version,
                    expected_rule_set_hash=expected_hash,
                    legacy_conversion=payload.get("legacy_conversion"),
                )
            store._assert_name_available(name, excluding_id=rule_set_id)
        else:
            base_version = None
            expected_hash = None
            store._assert_name_available(name)

        registry = RuleRegistry(project_root)
        refs, lineages = _materialize_named_rules(registry, raw_rules)
        secondary_refs, _ = _materialize_named_rules(
            registry,
            raw_secondary_rules,
            stage="secondary",
            occupied_refs=set(refs),
            occupied_lineages=lineages,
        )
        conversion: dict[str, Any] | None = None
        if rule_set_id and base.get("editor_mode") == LEGACY_EDITOR_MODE:
            converted = store.create_simple_conversion_version(
                rule_set_id,
                base_version=int(base_version),
                expected_rule_set_hash=expected_hash,
                legacy_conversion=payload.get("legacy_conversion"),
                name=name,
                rules=refs,
                secondary_rules=secondary_refs,
            )
            created = converted["version"]
            conversion = converted["conversion"]
        elif rule_set_id:
            created = store.create_simple_version(
                rule_set_id,
                base_version=int(base_version),
                expected_rule_set_hash=expected_hash,
                name=name,
                rules=refs,
                secondary_rules=secondary_refs,
            )
        else:
            created = store.create_simple(
                name=name,
                rules=refs,
                secondary_rules=secondary_refs,
            )
        validated = store.validate(
            created["id"],
            int(created["version"]),
            allow_validated_rules=True,
        )

        try:
            active = store.active()
        except RuleSetError as exc:
            if exc.code != "NO_ACTIVE_RULE_SET":
                raise
            active = None
        result = {
            "request_id": request_id,
            "request_hash": request_hash,
            "deduplicated": False,
            "rule_set": store.detail(validated["id"]),
            "version": store.get(validated["id"], int(validated["version"])),
            "active": active,
            "rule_refs": refs,
            "secondary_rule_refs": secondary_refs,
            "editor_mode": SIMPLE_EDITOR_MODE,
            "simple_semantics": "TWO_STAGE_ALL",
        }
        if conversion is not None:
            result["conversion"] = conversion
        atomic_write_text(
            receipt_path,
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        )
        return result
