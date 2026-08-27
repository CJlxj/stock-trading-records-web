"""Read-only lookup of the combinations that still reference a rule.

Deleting a rule is safe exactly when no rule set still points at it.  Every
version of every combination counts, not just the current one: old versions
stay readable and resolvable, so a rule one of them names has to stay too.

Saved screening batches deliberately do *not* count.  Each batch freezes a
complete copy of the rules it was decided by (``rule_set_snapshot`` plus the
per-row evidence), so it keeps rendering and auditing correctly whether or not
the rule still exists in the library.  Blocking on history would instead mean
one screening run makes a rule undeletable forever.

Combination families the user deleted are skipped: every read path in
``RuleSetStore`` already hides them, so a pointer left inside one can never be
resolved again.  Errors are never swallowed — an unreadable rule-set file
propagates, so a store we cannot inspect blocks the deletion instead of being
mistaken for "nothing references this rule".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.rules.version_store import RuleSetStore


RULE_SET_SECTIONS = ("base_gates", "required", "veto", "scored", "secondary")
RULE_SET_STAGE_LABELS = {
    "base_gates": "系统检查",
    "required": "必须满足",
    "veto": "命中即排除",
    "scored": "一级",
    "secondary": "二级",
}


def _reference_id(raw_ref: Any) -> str:
    ref = str(raw_ref or "").strip()
    return ref.rsplit("@", 1)[0] if "@" in ref else ref


def _section_refs(definition: dict[str, Any], section: str) -> list[Any]:
    group = definition.get(section)
    if not isinstance(group, dict):
        return []
    rules = group.get("rules")
    return list(rules) if isinstance(rules, list) else []


def rule_references(
    project_root: str | Path,
    rule_id: str,
) -> list[dict[str, Any]]:
    """Every combination version that still names ``rule_id``."""

    target = _reference_id(rule_id)
    if not target:
        return []
    references: list[dict[str, Any]] = []
    for definition in RuleSetStore(project_root).all_versions():
        stages = [
            RULE_SET_STAGE_LABELS[section]
            for section in RULE_SET_SECTIONS
            if any(
                _reference_id(ref) == target
                for ref in _section_refs(definition, section)
            )
        ]
        if not stages:
            continue
        references.append(
            {
                "kind": "rule_set",
                "id": str(definition["id"]),
                "name": str(definition.get("name") or definition["id"]),
                "version": int(definition["version"]),
                "stages": stages,
            }
        )
    return references


def describe_rule_references(references: list[dict[str, Any]]) -> str:
    """Render the reference list as one readable Chinese clause.

    Versions of the same combination collapse into a single entry: the user
    needs to know *which* combination to edit, not how many of its versions
    happen to mention the rule.
    """

    parts: list[str] = []
    seen: set[str] = set()
    for item in references:
        stages = "、".join(item.get("stages") or []) or "规则"
        label = f"{item.get('name') or item.get('id')}（{stages}）"
        if label in seen:
            continue
        seen.add(label)
        parts.append(label)
    return "、".join(parts)
