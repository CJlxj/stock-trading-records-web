"""Catalog tags for the rule library.

A tag is nothing but a label plus an opaque id, and a rule "has a tag" only
because ``rules/tags.yaml`` says so.  Keeping the mapping outside the rule
definitions is what makes the four operations the panel offers cheap and
reversible:

* tagging a rule never creates a new rule version;
* renaming a tag never rewrites a single rule file;
* deleting a tag just drops the bindings — the rules fall back to "未分类";
* deleting a rule drops its binding and nothing else.

It also keeps the library's categorisation away from the rule definition's
``group`` field, which the screening engine still reads when it decides how
many rule groups a candidate has to satisfy.  Those two must not be the same
knob: re-filing a rule in the library must never change a screening outcome.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import threading
from typing import Any
import uuid
from zoneinfo import ZoneInfo

import yaml

from src.rules.storage import atomic_write_text


class RuleTagError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "RULE_TAG_INVALID",
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


TAG_LABEL_MAXIMUM = 12
UNASSIGNED_LABEL = "未分类"

_TAGS_LOCK = threading.RLock()
_LABEL_REJECTED_PATTERN = re.compile(r"[\s　\x00-\x1f\x7f]")


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _path(project_root: str | Path) -> Path:
    return Path(project_root) / "rules" / "tags.yaml"


def _empty() -> dict[str, Any]:
    return {"schema_version": 1, "tags": [], "assignments": {}}


def _read(project_root: str | Path) -> dict[str, Any]:
    path = _path(project_root)
    if not path.is_file():
        return _empty()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return _empty()
    if not isinstance(raw, dict):
        return _empty()
    tags = [
        {
            "id": str(item.get("id") or ""),
            "label": str(item.get("label") or ""),
            "created_at": str(item.get("created_at") or ""),
        }
        for item in (raw.get("tags") or [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    assignments = {
        str(rule_id): str(tag_id)
        for rule_id, tag_id in (raw.get("assignments") or {}).items()
        if str(rule_id).strip() and str(tag_id).strip()
    }
    return {"schema_version": 1, "tags": tags, "assignments": assignments}


def _write(project_root: str | Path, store: dict[str, Any]) -> None:
    atomic_write_text(
        _path(project_root),
        yaml.safe_dump(store, allow_unicode=True, sort_keys=False),
    )


def _label(value: Any, store: dict[str, Any], *, excluding_id: str = "") -> str:
    label = str(value or "").strip()
    if not label:
        raise RuleTagError("标签名称不能为空。", field="label")
    if _LABEL_REJECTED_PATTERN.search(label):
        raise RuleTagError("标签名称不能包含空格或控制字符。", field="label")
    if len(label) > TAG_LABEL_MAXIMUM:
        raise RuleTagError(
            f"标签名称不能超过 {TAG_LABEL_MAXIMUM} 个字符。",
            field="label",
        )
    if label == UNASSIGNED_LABEL:
        raise RuleTagError(
            f"“{UNASSIGNED_LABEL}”是系统保留名称，请换一个。",
            code="RULE_TAG_NAME_RESERVED",
            field="label",
        )
    for tag in store["tags"]:
        if tag["label"] == label and tag["id"] != excluding_id:
            raise RuleTagError(
                f"已经有一个叫“{label}”的标签了。",
                code="RULE_TAG_NAME_EXISTS",
                field="label",
            )
    return label


def _find(store: dict[str, Any], tag_id: str) -> dict[str, Any]:
    for tag in store["tags"]:
        if tag["id"] == tag_id:
            return tag
    raise RuleTagError("没有找到该标签。", code="RULE_TAG_NOT_FOUND", field="tag_id")


def _new_tag_id(store: dict[str, Any]) -> str:
    existing = {tag["id"] for tag in store["tags"]}
    while True:
        candidate = f"tag_{uuid.uuid4().hex[:12]}"
        if candidate not in existing:
            return candidate


def tag_payload(
    project_root: str | Path,
    rule_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Tags in creation order, with how many of ``rule_ids`` each one holds."""

    store = _read(project_root)
    known = {tag["id"] for tag in store["tags"]}
    ids = list(rule_ids or [])
    assignments = {
        rule_id: tag_id
        for rule_id, tag_id in store["assignments"].items()
        if tag_id in known and (not ids or rule_id in ids)
    }
    counts: dict[str, int] = {tag["id"]: 0 for tag in store["tags"]}
    for tag_id in assignments.values():
        counts[tag_id] += 1
    return {
        "items": [
            {"id": tag["id"], "label": tag["label"], "count": counts[tag["id"]]}
            for tag in store["tags"]
        ],
        "unassigned_label": UNASSIGNED_LABEL,
        "unassigned_count": max(0, len(ids) - sum(counts.values())),
        "assignments": assignments,
    }


def rule_tag_id(project_root: str | Path, rule_id: str) -> str:
    """The tag bound to one rule, or "" when it is unassigned or dangling."""

    store = _read(project_root)
    tag_id = store["assignments"].get(str(rule_id), "")
    return tag_id if any(tag["id"] == tag_id for tag in store["tags"]) else ""


def create_tag(project_root: str | Path, label: Any) -> dict[str, Any]:
    with _TAGS_LOCK:
        store = _read(project_root)
        checked = _label(label, store)
        tag = {
            "id": _new_tag_id(store),
            "label": checked,
            "created_at": _now(),
        }
        store["tags"].append(tag)
        _write(project_root, store)
        return dict(tag)


def rename_tag(
    project_root: str | Path,
    tag_id: str,
    label: Any,
) -> dict[str, Any]:
    with _TAGS_LOCK:
        store = _read(project_root)
        tag = _find(store, str(tag_id))
        tag["label"] = _label(label, store, excluding_id=tag["id"])
        _write(project_root, store)
        return dict(tag)


def delete_tag(project_root: str | Path, tag_id: str) -> dict[str, Any]:
    """Drop a tag and release every rule it held back to "未分类"."""

    with _TAGS_LOCK:
        store = _read(project_root)
        tag = _find(store, str(tag_id))
        released = sorted(
            rule_id
            for rule_id, bound in store["assignments"].items()
            if bound == tag["id"]
        )
        store["tags"] = [item for item in store["tags"] if item["id"] != tag["id"]]
        store["assignments"] = {
            rule_id: bound
            for rule_id, bound in store["assignments"].items()
            if bound != tag["id"]
        }
        _write(project_root, store)
        return {
            "deleted": True,
            "tag": dict(tag),
            "released_rule_ids": released,
        }


def assign_tag(
    project_root: str | Path,
    rule_id: str,
    tag_id: Any,
) -> str:
    """Bind one rule to a tag, or unbind it when ``tag_id`` is empty."""

    target = str(rule_id).strip()
    if not target:
        raise RuleTagError("规则标识不能为空。", field="rule_id")
    wanted = str(tag_id or "").strip()
    with _TAGS_LOCK:
        store = _read(project_root)
        if wanted:
            _find(store, wanted)
            store["assignments"][target] = wanted
        else:
            store["assignments"].pop(target, None)
        _write(project_root, store)
        return wanted


def drop_rule(project_root: str | Path, rule_id: str) -> None:
    """Forget a deleted rule.  Never fails: a missing binding is the goal."""

    with _TAGS_LOCK:
        store = _read(project_root)
        if store["assignments"].pop(str(rule_id), None) is None:
            return
        _write(project_root, store)


def ensure_tag_label(project_root: str | Path, label: Any) -> str:
    """Return the id of the tag with this label, creating it when missing.

    Used when importing the factory rules: their shipped categories become
    ordinary tags, and a re-import reuses whatever the user has renamed them to
    rather than piling up duplicates.
    """

    wanted = str(label or "").strip()
    if not wanted:
        return ""
    # The lock is reentrant, so holding it across create_tag keeps
    # "look up, else create" a single decision.
    with _TAGS_LOCK:
        for tag in _read(project_root)["tags"]:
            if tag["label"] == wanted:
                return tag["id"]
        return create_tag(project_root, wanted)["id"]
