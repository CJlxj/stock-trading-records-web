from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import math
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any
import uuid
from zoneinfo import ZoneInfo

import yaml

from src.rules.registry import RuleRegistry, RuleRegistryError
from src.rules.storage import (
    append_audit_event,
    atomic_write_text,
    canonical_hash,
    file_sha256,
    relative_project_path,
)


_RULE_SET_LOCK = threading.RLock()
SIMPLE_EDITOR_MODE = "simple_all"
LEGACY_EDITOR_MODE = "legacy_advanced"
LEGACY_CONVERSION_POLICY = "legacy_to_two_stage_all_v1"
DEFAULT_BASE_GATES = [
    "system.allowed_market@1",
    "system.not_suspended@1",
    "system.minimum_history@1",
    "system.same_as_of_date@1",
]


class RuleSetError(ValueError):
    def __init__(self, message: str, *, code: str = "RULE_SET_ERROR") -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    return normalized[:60] or "rule_set"


def _ratio(value: Any, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise RuleSetError(f"{label}必须是数字。") from None
    if not 0 < parsed <= 1:
        raise RuleSetError(f"{label}必须位于 0% 到 100% 之间。")
    return parsed


def _rule_refs(raw: Any, label: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RuleSetError(f"{label}必须是规则引用数组。")
    refs: list[str] = []
    for item in raw:
        ref = str(item.get("ref") if isinstance(item, dict) else item).strip()
        if not re.fullmatch(r"(?:[a-z][a-z0-9_]*\.)?[a-z][a-z0-9_]{1,80}@\d+", ref):
            raise RuleSetError(f"规则引用不合法：{ref}。")
        if ref not in refs:
            refs.append(ref)
    return refs


def _unique_refs(*groups: list[str]) -> list[str]:
    refs: list[str] = []
    for group in groups:
        for ref in group:
            if ref not in refs:
                refs.append(ref)
    return refs


def normalize_rule_set(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RuleSetError("规则方案必须是对象。")
    rule_set_id = str(raw.get("id") or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,80}", rule_set_id):
        raise RuleSetError("规则方案 ID 只能使用小写字母、数字和下划线。")
    try:
        version = int(raw.get("version", 1))
    except (TypeError, ValueError):
        raise RuleSetError("规则方案版本必须是整数。") from None
    if version < 1:
        raise RuleSetError("规则方案版本必须大于 0。")
    name = str(raw.get("name") or "").strip()
    if not name or len(name) > 80:
        raise RuleSetError("规则方案名称不能为空且不能超过 80 字。")
    status = str(raw.get("status") or "DRAFT").upper()
    if status not in {"DRAFT", "VALIDATED", "ACTIVE", "SUPERSEDED", "ARCHIVED"}:
        raise RuleSetError("规则方案状态不受支持。")

    base_raw = raw.get("base_gates") or {}
    required_raw = raw.get("required") or {}
    veto_raw = raw.get("veto") or {}
    scored_raw = raw.get("scored") or {}
    secondary_raw = raw.get("secondary") or {}
    ranking_raw = raw.get("ranking") or {}
    near_raw = raw.get("near_policy") or {}
    group_raw = raw.get("group_policy") or {}
    explicit_editor_mode = raw.get("editor_mode")
    editor_mode = (
        str(explicit_editor_mode).strip()
        if explicit_editor_mode is not None
        else LEGACY_EDITOR_MODE
    )
    if editor_mode not in {SIMPLE_EDITOR_MODE, LEGACY_EDITOR_MODE}:
        raise RuleSetError("规则方案编辑模式不受支持。", code="RULE_SET_EDITOR_MODE_INVALID")
    try:
        raw_schema_version = int(raw.get("schema_version") or 0)
    except (TypeError, ValueError):
        raw_schema_version = 0
    base_refs = _rule_refs(base_raw.get("rules"), "基础门槛")
    required_refs = _rule_refs(required_raw.get("rules"), "必须规则")
    veto_refs = _rule_refs(veto_raw.get("rules"), "否决规则")
    scored_refs = _rule_refs(scored_raw.get("rules"), "评分规则")
    if not scored_refs:
        raise RuleSetError("规则方案至少需要一条评分规则。")
    scored_mode = str(scored_raw.get("mode") or "equal_score")
    pass_ratio = _ratio(scored_raw.get("pass_ratio", 0.65), "最终候选比例")
    near_ratio = _ratio(near_raw.get("score_ratio", 0.45), "近似候选比例")
    if near_ratio >= pass_ratio:
        raise RuleSetError("近似候选比例必须低于最终候选比例。")
    secondary_refs = _rule_refs(secondary_raw.get("rules"), "二级筛选规则")
    overlap = sorted(set(scored_refs) & set(secondary_refs))
    if overlap:
        raise RuleSetError(
            "一级规则和二级筛选规则不能重复：" + "、".join(overlap) + "。"
        )
    try:
        secondary_minimum = int(
            secondary_raw.get("minimum_match", 0 if not secondary_refs else len(secondary_refs))
        )
    except (TypeError, ValueError):
        raise RuleSetError("二级筛选门槛必须是整数。") from None
    if secondary_refs and not 1 <= secondary_minimum <= len(secondary_refs):
        raise RuleSetError("二级筛选门槛不能超过二级规则数量。")
    if not secondary_refs and secondary_minimum != 0:
        raise RuleSetError("没有二级规则时，二级筛选门槛必须为 0。")
    if editor_mode == SIMPLE_EDITOR_MODE:
        if required_refs or veto_refs:
            raise RuleSetError(
                "日常简单组合不支持必须规则或否决规则。",
                code="RULE_SET_SIMPLE_ALL_INVALID",
            )
        if abs(pass_ratio - 1.0) > 1e-12:
            raise RuleSetError(
                "日常简单组合固定为全部规则同时满足。",
                code="RULE_SET_SIMPLE_ALL_INVALID",
            )
        if scored_mode != "all":
            raise RuleSetError(
                "日常简单组合的一级规则固定为全部满足。",
                code="RULE_SET_SIMPLE_ALL_INVALID",
            )
        if secondary_refs and str(secondary_raw.get("mode") or "all") != "all":
            raise RuleSetError(
                "日常简单组合的二级规则固定为全部满足。",
                code="RULE_SET_SIMPLE_ALL_INVALID",
            )
        if secondary_refs and secondary_minimum != len(secondary_refs):
            raise RuleSetError(
                "日常简单组合的二级规则固定为全部满足。",
                code="RULE_SET_SIMPLE_ALL_INVALID",
            )
    ranking_mode = str(ranking_raw.get("mode") or "priority_strength")
    if ranking_mode != "priority_strength":
        raise RuleSetError("候选排序只支持规则优先级与指标强度模式。")
    try:
        top_n = int(ranking_raw.get("top_n", 5))
    except (TypeError, ValueError):
        raise RuleSetError("候选默认展示数量必须是整数。") from None
    if not 1 <= top_n <= 20:
        raise RuleSetError("候选默认展示数量必须位于 1 到 20 之间。")
    if editor_mode == SIMPLE_EDITOR_MODE and top_n != 5:
        raise RuleSetError(
            "日常简单组合固定默认展示前 5 条。",
            code="RULE_SET_SIMPLE_ALL_INVALID",
        )
    try:
        minimum_groups = int(group_raw.get("minimum_groups", 1))
    except (TypeError, ValueError):
        raise RuleSetError("最少规则组必须是整数。") from None
    if not 1 <= minimum_groups <= 20:
        raise RuleSetError("最少规则组超出范围。")
    simple_schema_version = (
        3
        if editor_mode == SIMPLE_EDITOR_MODE
        and (raw_schema_version >= 3 or bool(secondary_refs))
        else 2
    )
    return {
        "schema_version": simple_schema_version if editor_mode == SIMPLE_EDITOR_MODE else 1,
        "id": rule_set_id,
        "version": version,
        "name": name,
        "display_name": name,
        "editor_mode": editor_mode,
        "simple_semantics": (
            "TWO_STAGE_ALL"
            if editor_mode == SIMPLE_EDITOR_MODE and simple_schema_version >= 3
            else "ALL" if editor_mode == SIMPLE_EDITOR_MODE else None
        ),
        "status": status,
        "timeframe": "day",
        "base_gates": {
            "mode": "all",
            "rules": base_refs,
        },
        "required": {
            "mode": "all",
            "rules": required_refs,
        },
        "veto": {
            "mode": "any",
            "rules": veto_refs,
        },
        "scored": {
            "mode": scored_mode,
            "rules": scored_refs,
            "pass_ratio": pass_ratio,
            "error_policy": str(scored_raw.get("error_policy") or "block_candidate"),
        },
        "secondary": {
            "mode": (
                "all"
                if editor_mode == SIMPLE_EDITOR_MODE and simple_schema_version >= 3
                else "minimum_match"
            ),
            "rules": secondary_refs,
            "minimum_match": secondary_minimum,
            "error_policy": "exclude_from_refined_candidates",
        },
        "ranking": {
            "mode": ranking_mode,
            "top_n": top_n,
            "rule_priority": "listed_order",
            "strength_policy": "batch_percentile_of_threshold_margin",
        },
        "near_policy": {
            "score_ratio": near_ratio,
            "require_no_veto": bool(near_raw.get("require_no_veto", True)),
        },
        "group_policy": {
            "minimum_groups": minimum_groups,
            "group_pass_ratio": _ratio(
                group_raw.get("group_pass_ratio", 0.5), "规则组通过比例"
            ),
        },
    }


def simple_all_rule_set_payload(
    *,
    rule_set_id: str,
    name: str,
    rules: list[str],
    secondary_rules: list[str] | None = None,
    base_gates: list[str] | None = None,
) -> dict[str, Any]:
    if not rules:
        raise RuleSetError("日常简单组合至少需要一条规则。")
    secondary_rules = list(secondary_rules or [])
    overlap = sorted(set(rules) & set(secondary_rules))
    if overlap:
        raise RuleSetError(
            "同一规则不能同时用于一级和二级筛选：" + "、".join(overlap) + "。",
            code="RULE_STAGE_DUPLICATED",
        )
    near_ratio = (len(rules) - 1) / len(rules) if len(rules) > 1 else 0.5
    return {
        "schema_version": 3,
        "id": rule_set_id,
        "name": name,
        "editor_mode": SIMPLE_EDITOR_MODE,
        "base_gates": {"rules": list(base_gates or DEFAULT_BASE_GATES)},
        "required": {"rules": []},
        "veto": {"rules": []},
        "scored": {
            "mode": "all",
            "rules": list(rules),
            "pass_ratio": 1.0,
            "error_policy": "block_candidate",
        },
        "secondary": {
            "mode": "all",
            "rules": secondary_rules,
            "minimum_match": len(secondary_rules),
            "error_policy": "exclude_from_refined_candidates",
        },
        "ranking": {"mode": "priority_strength", "top_n": 5},
        "near_policy": {
            "score_ratio": max(0.01, min(near_ratio, 0.99)),
            "require_no_veto": True,
        },
        "group_policy": {"minimum_groups": 1, "group_pass_ratio": 1.0},
    }


class RuleSetStore:
    def __init__(self, project_root: str | Path) -> None:
        self.root = Path(project_root)
        self.rule_sets_root = self.root / "rules" / "rule_sets"
        self.state_path = self.rule_sets_root / "state.yaml"
        self.registry = RuleRegistry(self.root)

    def _state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"schema_version": 1, "sets": {}, "active": None}
        try:
            raw = yaml.safe_load(self.state_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return {"schema_version": 1, "sets": {}, "active": None}
        return raw if isinstance(raw, dict) else {"schema_version": 1, "sets": {}, "active": None}

    def _write_state(self, state: dict[str, Any]) -> None:
        atomic_write_text(
            self.state_path,
            yaml.safe_dump(state, allow_unicode=True, sort_keys=False),
        )

    def _entry(
        self, state: dict[str, Any], rule_set_id: str, version: int
    ) -> dict[str, Any]:
        versions = state.setdefault("sets", {}).setdefault(rule_set_id, {}).setdefault(
            "versions", {}
        )
        return versions.setdefault(str(version), {})

    @staticmethod
    def _family(state: dict[str, Any], rule_set_id: str) -> dict[str, Any]:
        return state.setdefault("sets", {}).setdefault(rule_set_id, {})

    @staticmethod
    def _family_deleted(family: dict[str, Any]) -> bool:
        # 旧版“归档”没有恢复到新生命周期：升级后直接视为已经删除。
        return bool(family.get("deleted_at") or family.get("archived"))

    def _new_rule_set_id(self) -> str:
        existing = {
            str(item["id"])
            for item in self.all_versions()
        } | set(self._state().get("sets", {}))
        while True:
            candidate = f"rs_{uuid.uuid4().hex[:12]}"
            if candidate not in existing:
                return candidate

    def all_versions(self, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        state = self._state()
        pointer = state.get("active") if isinstance(state.get("active"), dict) else None
        fallback_current: tuple[str, int] | None = None
        versions: list[dict[str, Any]] = []
        for path in sorted(self.rule_sets_root.glob("*/v*.yaml")):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                definition = normalize_rule_set(raw)
            except (OSError, yaml.YAMLError, RuleSetError) as exc:
                raise RuleSetError(f"规则方案文件无效：{path.name}：{exc}。") from None
            entry = (
                state.get("sets", {})
                .get(definition["id"], {})
                .get("versions", {})
                .get(str(definition["version"]), {})
            )
            family = state.get("sets", {}).get(definition["id"], {})
            deleted = self._family_deleted(family)
            if deleted and not include_deleted:
                continue
            pointer_match = bool(
                not deleted
                and pointer
                and str(pointer.get("id")) == definition["id"]
                and int(pointer.get("version", -1)) == int(definition["version"])
            )
            stored_status = str(entry.get("status") or "")
            file_status = str(definition["status"])
            active_marker = stored_status == "ACTIVE" or file_status == "ACTIVE"
            if (
                not deleted
                and pointer is None
                and active_marker
                and fallback_current is None
            ):
                fallback_current = (definition["id"], int(definition["version"]))
                pointer_match = True
            if deleted:
                effective_status = stored_status or file_status
            elif pointer_match:
                effective_status = "ACTIVE"
            elif active_marker:
                # The state pointer is the sole source of truth for the global
                # current selection.  Switching to a *different* rule-set
                # family must keep the former version reusable; only a newer
                # version in the same family supersedes its predecessor.
                current_id = (
                    str(pointer.get("id"))
                    if pointer
                    else (fallback_current or ("", 0))[0]
                )
                effective_status = (
                    "SUPERSEDED"
                    if current_id == definition["id"]
                    else "VALIDATED"
                )
            else:
                effective_status = stored_status or file_status
            definition["status"] = effective_status
            definition["validated_at"] = entry.get("validated_at")
            definition["activated_at"] = entry.get("activated_at")
            definition["is_current"] = pointer_match
            definition["is_active"] = pointer_match
            definition["deleted"] = deleted
            definition["source_path"] = relative_project_path(self.root, path)
            definition["source_hash"] = file_sha256(path)
            definition["rule_set_hash"] = canonical_hash(
                {
                    key: value
                    for key, value in definition.items()
                    if key
                    not in {
                        "status",
                        "validated_at",
                        "activated_at",
                        "is_current",
                        "is_active",
                        "deleted",
                        "source_path",
                        "source_hash",
                    }
                }
            )
            versions.append(definition)
        return sorted(versions, key=lambda item: (item["id"], int(item["version"])))

    def list(self) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in self.all_versions():
            grouped.setdefault(item["id"], []).append(item)
        return [
            deepcopy(max(versions, key=lambda item: int(item["version"])))
            for versions in grouped.values()
        ]

    def _usage_counts(self) -> dict[tuple[str, int], int]:
        path = self.root / "history" / "screenings.sqlite3"
        if not path.is_file():
            return {}
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
            rows = connection.execute(
                """
                SELECT rule_set_id, rule_set_version, COUNT(*)
                FROM screening_runs
                WHERE rule_set_id IS NOT NULL AND rule_set_version IS NOT NULL
                GROUP BY rule_set_id, rule_set_version
                """
            ).fetchall()
        except sqlite3.Error:
            return {}
        finally:
            if connection is not None:
                connection.close()
        return {
            (str(rule_set_id), int(version)): int(count)
            for rule_set_id, version, count in rows
        }

    def _ever_activated(self, rule_set_id: str, version: int) -> bool:
        entry = (
            self._state()
            .get("sets", {})
            .get(rule_set_id, {})
            .get("versions", {})
            .get(str(version), {})
        )
        return bool(
            entry.get("activated_at")
            or entry.get("superseded_at")
            or str(entry.get("status") or "") in {"ACTIVE", "SUPERSEDED"}
        )

    def _is_pure_draft_family(
        self,
        rule_set_id: str,
        versions: list[dict[str, Any]],
        usage: dict[tuple[str, int], int],
    ) -> bool:
        if len(versions) != 1:
            return False
        definition = versions[0]
        version = int(definition["version"])
        return bool(
            definition["status"] == "DRAFT"
            and not definition.get("validated_at")
            and not definition.get("activated_at")
            and not self._ever_activated(rule_set_id, version)
            and usage.get((rule_set_id, version), 0) == 0
            and not definition.get("is_current")
        )

    def summaries(self) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        all_versions = self.all_versions()
        for item in all_versions:
            grouped.setdefault(item["id"], []).append(item)
        usage = self._usage_counts()
        state = self._state()
        pointer = state.get("active") if isinstance(state.get("active"), dict) else None
        if pointer is None:
            current = next((item for item in all_versions if item["is_current"]), None)
            if current is not None:
                pointer = {"id": current["id"], "version": int(current["version"])}
        results: list[dict[str, Any]] = []
        for rule_set_id, versions in grouped.items():
            family = state.get("sets", {}).get(rule_set_id, {})
            if self._family_deleted(family):
                continue
            latest = max(versions, key=lambda item: int(item["version"]))
            active_version = (
                int(pointer["version"])
                if pointer and str(pointer.get("id")) == rule_set_id
                else None
            )
            is_current = active_version is not None
            usage_count = sum(
                usage.get((rule_set_id, int(item["version"])), 0)
                for item in versions
            )
            pure_draft = self._is_pure_draft_family(rule_set_id, versions, usage)
            usable_versions = [
                item
                for item in versions
                if item["status"] in {"VALIDATED", "ACTIVE"}
            ]
            usable_version = (
                active_version
                if active_version is not None
                else (
                    int(max(usable_versions, key=lambda item: int(item["version"]))["version"])
                    if usable_versions
                    else None
                )
            )
            editor_mode = str(latest.get("editor_mode") or LEGACY_EDITOR_MODE)
            edit_strategy = (
                "new_simple_version_same_id"
                if editor_mode == LEGACY_EDITOR_MODE
                else "new_version_same_id"
            )
            # Both families expose one user-facing edit action.  A legacy
            # family is never overwritten: its edit action requires an
            # explicit conversion receipt and creates a simple version under
            # the same stable family ID.
            can_edit = True
            capabilities = {
                "edit": can_edit,
                "clone": True,
                "activate": bool(
                    latest["status"] in {"VALIDATED", "ACTIVE"}
                    and not (
                        is_current and active_version == int(latest["version"])
                    )
                ),
                "delete": not is_current,
            }
            status = "ACTIVE" if is_current else latest["status"]
            results.append(
                {
                    "id": rule_set_id,
                    "name": latest["name"],
                    "display_name": latest["name"],
                    "editor_mode": editor_mode,
                    "edit_strategy": edit_strategy,
                    "conversion_required": editor_mode == LEGACY_EDITOR_MODE,
                    "simple_semantics": latest.get("simple_semantics"),
                    "is_legacy": editor_mode == LEGACY_EDITOR_MODE,
                    "is_current": is_current,
                    "is_active": is_current,
                    "status": status,
                    "latest_version": int(latest["version"]),
                    "active_version": active_version,
                    "usable_version": usable_version,
                    "version_count": len(versions),
                    "usage_count": usage_count,
                    "has_history": usage_count > 0,
                    "primary_rule_count": len(latest["scored"]["rules"]),
                    "secondary_rule_count": len(latest["secondary"]["rules"]),
                    "rule_set_hash": latest["rule_set_hash"],
                    "latest": deepcopy(latest),
                    "capabilities": capabilities,
                    "can_edit": capabilities["edit"],
                    "can_clone": capabilities["clone"],
                    "can_activate": capabilities["activate"],
                    "can_delete": capabilities["delete"],
                    "delete_mode": "physical" if pure_draft else "tombstone",
                }
            )
        return sorted(
            results,
            key=lambda item: (
                not item["is_current"],
                str(item["name"]),
                str(item["id"]),
            ),
        )

    def detail(self, rule_set_id: str) -> dict[str, Any]:
        matches = [
            item
            for item in self.summaries()
            if item["id"] == rule_set_id
        ]
        if not matches:
            raise RuleSetError("没有找到该规则方案。", code="RULE_SET_NOT_FOUND")
        result = deepcopy(matches[0])
        result["versions"] = self.versions(rule_set_id)
        return result

    def versions(self, rule_set_id: str) -> list[dict[str, Any]]:
        versions = [
            item for item in self.all_versions() if item["id"] == rule_set_id
        ]
        if not versions:
            raise RuleSetError("没有找到该规则方案。", code="RULE_SET_NOT_FOUND")
        return sorted(
            (deepcopy(item) for item in versions),
            key=lambda item: int(item["version"]),
            reverse=True,
        )

    def get(self, rule_set_id: str, version: int | None = None) -> dict[str, Any]:
        matches = [
            item
            for item in self.all_versions()
            if item["id"] == rule_set_id
            and (version is None or int(item["version"]) == int(version))
        ]
        if not matches:
            raise RuleSetError("没有找到该规则方案。", code="RULE_SET_NOT_FOUND")
        if version is None:
            active = [item for item in matches if item["status"] == "ACTIVE"]
            return deepcopy(max(active or matches, key=lambda item: int(item["version"])))
        return deepcopy(matches[0])

    def active(self) -> dict[str, Any]:
        state = self._state()
        pointer = state.get("active")
        if isinstance(pointer, dict):
            try:
                return self.get(str(pointer["id"]), int(pointer["version"]))
            except (KeyError, RuleSetError):
                pass
        active = [item for item in self.all_versions() if item["status"] == "ACTIVE"]
        if not active:
            raise RuleSetError("没有已激活规则方案。", code="NO_ACTIVE_RULE_SET")
        return deepcopy(max(active, key=lambda item: (item["id"], int(item["version"]))))

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        with _RULE_SET_LOCK:
            requested_id = str(payload.get("id") or _slug(str(payload.get("name") or "")))
            family = self._state().get("sets", {}).get(requested_id, {})
            if self._family_deleted(family):
                raise RuleSetError(
                    "已删除的规则组合不能恢复或覆盖。",
                    code="RULE_SET_DELETED",
                )
            existing = [item for item in self.all_versions() if item["id"] == requested_id]
            state_versions = (
                self._state()
                .get("sets", {})
                .get(requested_id, {})
                .get("versions", {})
            )
            known_versions = [int(item["version"]) for item in existing]
            known_versions.extend(
                int(value) for value in state_versions if str(value).isdigit()
            )
            next_version = max(known_versions, default=0) + 1
            raw = {
                **payload,
                "id": requested_id,
                "version": next_version,
                "status": "DRAFT",
            }
            definition = normalize_rule_set(raw)
            path = self.rule_sets_root / requested_id / f"v{next_version}.yaml"
            if path.exists():
                raise RuleSetError("规则方案版本已存在，不能覆盖。")
            atomic_write_text(
                path,
                yaml.safe_dump(definition, allow_unicode=True, sort_keys=False),
            )
            state = self._state()
            self._entry(state, requested_id, next_version).update(
                {"status": "DRAFT", "created_at": _now()}
            )
            self._write_state(state)
            append_audit_event(
                self.root,
                "RULE_SET_VERSION_CREATED",
                subject_id=requested_id,
                version=next_version,
            )
            return self.get(requested_id, next_version)

    def _assert_name_available(self, name: str, *, excluding_id: str | None = None) -> None:
        normalized = str(name or "").strip().casefold()
        if not normalized:
            raise RuleSetError("规则方案名称不能为空。")
        for item in self.summaries():
            if item["id"] != excluding_id and str(item["name"]).strip().casefold() == normalized:
                raise RuleSetError(
                    "已有同名规则组合，请使用另一个名称。",
                    code="RULE_SET_NAME_EXISTS",
                )

    def create_simple(
        self,
        *,
        name: str,
        rules: list[str],
        secondary_rules: list[str] | None = None,
    ) -> dict[str, Any]:
        with _RULE_SET_LOCK:
            self._assert_name_available(name)
            rule_set_id = self._new_rule_set_id()
            return self.create(
                simple_all_rule_set_payload(
                    rule_set_id=rule_set_id,
                    name=name,
                    rules=rules,
                    secondary_rules=secondary_rules,
                )
            )

    @staticmethod
    def _assert_hash(
        definition: dict[str, Any], expected_hash: str | None, *, code: str = "RULE_SET_CHANGED"
    ) -> None:
        expected = str(expected_hash or "").strip()
        if expected and expected != str(definition.get("rule_set_hash") or ""):
            raise RuleSetError(
                "规则组合已发生变化，请重新读取后再操作。",
                code=code,
            )

    def create_simple_version(
        self,
        rule_set_id: str,
        *,
        base_version: int,
        expected_rule_set_hash: str | None,
        name: str,
        rules: list[str],
        secondary_rules: list[str] | None = None,
    ) -> dict[str, Any]:
        with _RULE_SET_LOCK:
            detail = self.detail(rule_set_id)
            base = self.get(rule_set_id, base_version)
            self._assert_hash(base, expected_rule_set_hash)
            if base.get("editor_mode") != SIMPLE_EDITOR_MODE:
                raise RuleSetError(
                    "旧版高级组合只能查看或复制为新的简单组合。",
                    code="RULE_SET_ADVANCED_READ_ONLY",
                )
            latest_version = int(detail["latest_version"])
            if int(base_version) != latest_version:
                raise RuleSetError(
                    "该组合已有更新版本，请从最新版继续编辑。",
                    code="RULE_SET_CHANGED",
                )
            self._assert_name_available(name, excluding_id=rule_set_id)
            return self.create(
                simple_all_rule_set_payload(
                    rule_set_id=rule_set_id,
                    name=name,
                    rules=rules,
                    secondary_rules=secondary_rules,
                    base_gates=base["base_gates"]["rules"],
                )
            )

    @staticmethod
    def _legacy_conversion_plan_from_source(
        source: dict[str, Any],
    ) -> dict[str, Any]:
        scored_refs = list(source.get("scored", {}).get("rules") or [])
        required_refs = list(source.get("required", {}).get("rules") or [])
        veto_refs = list(source.get("veto", {}).get("rules") or [])
        secondary_refs = list(source.get("secondary", {}).get("rules") or [])
        primary_refs = _unique_refs(scored_refs, required_refs)
        if not primary_refs:
            raise RuleSetError(
                "旧版组合没有可转换的一级规则。",
                code="RULE_SET_CONVERSION_EMPTY",
            )
        if len(primary_refs) > 30 or len(secondary_refs) > 30:
            raise RuleSetError(
                "旧版组合规则数量超过日常编辑器上限，不能直接转换。",
                code="RULE_SET_CONVERSION_TOO_LARGE",
            )

        scored_ratio = float(source.get("scored", {}).get("pass_ratio") or 0)
        old_primary_minimum = (
            max(1, min(len(scored_refs), math.ceil(scored_ratio * len(scored_refs))))
            if scored_refs
            else 0
        )
        raw_secondary_minimum = int(
            source.get("secondary", {}).get("minimum_match") or 0
        )
        old_secondary_minimum = (
            max(1, min(len(secondary_refs), raw_secondary_minimum))
            if secondary_refs
            else 0
        )
        return {
            "policy": LEGACY_CONVERSION_POLICY,
            "source_id": str(source["id"]),
            "source_version": int(source["version"]),
            "source_rule_set_hash": str(source.get("rule_set_hash") or ""),
            "default_primary_rules": primary_refs,
            "default_secondary_rules": secondary_refs,
            "kept_scored_rules": scored_refs,
            "moved_required_rules": [
                ref for ref in required_refs if ref not in scored_refs
            ],
            "dropped_veto_refs": veto_refs,
            "threshold_changes": {
                "primary": {
                    "from_mode": str(source.get("scored", {}).get("mode") or "equal_score"),
                    "from_minimum": old_primary_minimum,
                    "from_total": len(scored_refs),
                    "to_mode": "all",
                    "to_minimum": len(primary_refs),
                    "to_total": len(primary_refs),
                },
                "secondary": {
                    "from_mode": str(
                        source.get("secondary", {}).get("mode") or "minimum_match"
                    ),
                    "from_minimum": old_secondary_minimum,
                    "from_total": len(secondary_refs),
                    "to_mode": "all",
                    "to_minimum": len(secondary_refs),
                    "to_total": len(secondary_refs),
                },
            },
            "dropped_group_policy": deepcopy(source.get("group_policy") or {}),
        }

    def _validated_legacy_conversion_plan(
        self,
        rule_set_id: str,
        *,
        base_version: int,
        expected_rule_set_hash: str | None,
        legacy_conversion: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        detail = self.detail(rule_set_id)
        source = self.get(rule_set_id, base_version)
        expected_hash = str(expected_rule_set_hash or "").strip()
        if not expected_hash:
            raise RuleSetError(
                "转换旧版组合必须提供读取时的版本摘要。",
                code="RULE_SET_HASH_REQUIRED",
            )
        self._assert_hash(source, expected_hash)
        if int(detail["latest_version"]) != int(base_version):
            raise RuleSetError(
                "该组合已有更新版本，请从最新版继续编辑。",
                code="RULE_SET_CHANGED",
            )
        if source.get("editor_mode") != LEGACY_EDITOR_MODE:
            raise RuleSetError(
                "当前组合不是需要转换的旧版组合。",
                code="RULE_SET_CONVERSION_NOT_APPLICABLE",
            )
        if not isinstance(legacy_conversion, dict):
            raise RuleSetError(
                "保存旧版组合前必须确认转换为简化规则。",
                code="RULE_SET_CONVERSION_CONFIRMATION_REQUIRED",
            )
        if legacy_conversion.get("confirmed") is not True:
            raise RuleSetError(
                "保存旧版组合前必须明确确认门槛变化。",
                code="RULE_SET_CONVERSION_CONFIRMATION_REQUIRED",
            )
        if str(legacy_conversion.get("policy") or "") != LEGACY_CONVERSION_POLICY:
            raise RuleSetError(
                "旧版组合转换策略不受支持，请重新载入组合。",
                code="RULE_SET_CONVERSION_INVALID",
            )
        raw_source_version = legacy_conversion.get("source_version")
        if isinstance(raw_source_version, bool):
            source_version = 0
        else:
            try:
                source_version = int(raw_source_version)
            except (TypeError, ValueError):
                source_version = 0
        submitted_source_hash = str(
            legacy_conversion.get("source_rule_set_hash") or ""
        ).strip()
        if (
            source_version != int(base_version)
            or submitted_source_hash != expected_hash
            or submitted_source_hash != str(source.get("rule_set_hash") or "")
        ):
            raise RuleSetError(
                "旧版组合转换来源已变化，请重新载入后核对。",
                code="RULE_SET_CHANGED",
            )

        plan = self._legacy_conversion_plan_from_source(source)
        submitted_veto = legacy_conversion.get("dropped_veto_refs")
        if (
            not isinstance(submitted_veto, list)
            or submitted_veto != plan["dropped_veto_refs"]
        ):
            raise RuleSetError(
                "旧版否决条件尚未完整确认，不能转换。",
                code="RULE_SET_CONVERSION_VETO_UNCONFIRMED",
            )
        return source, plan

    def legacy_conversion_plan(
        self,
        rule_set_id: str,
        *,
        base_version: int,
        expected_rule_set_hash: str | None,
        legacy_conversion: Any,
    ) -> dict[str, Any]:
        """Validate an explicit legacy conversion without writing a version."""

        with _RULE_SET_LOCK:
            _, plan = self._validated_legacy_conversion_plan(
                rule_set_id,
                base_version=base_version,
                expected_rule_set_hash=expected_rule_set_hash,
                legacy_conversion=legacy_conversion,
            )
            return deepcopy(plan)

    def create_simple_conversion_version(
        self,
        rule_set_id: str,
        *,
        base_version: int,
        expected_rule_set_hash: str | None,
        legacy_conversion: Any,
        name: str,
        rules: list[str],
        secondary_rules: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a same-family simple version while preserving legacy bytes."""

        with _RULE_SET_LOCK:
            source, plan = self._validated_legacy_conversion_plan(
                rule_set_id,
                base_version=base_version,
                expected_rule_set_hash=expected_rule_set_hash,
                legacy_conversion=legacy_conversion,
            )
            if not rules:
                raise RuleSetError(
                    "日常简单组合至少需要一条规则。",
                    code="RULE_SET_CONVERSION_EMPTY",
                )
            if len(rules) > 30 or len(list(secondary_rules or [])) > 30:
                raise RuleSetError(
                    "转换后的规则数量超过日常编辑器上限。",
                    code="RULE_SET_CONVERSION_TOO_LARGE",
                )
            self._assert_name_available(name, excluding_id=rule_set_id)
            created = self.create(
                simple_all_rule_set_payload(
                    rule_set_id=rule_set_id,
                    name=name,
                    rules=rules,
                    secondary_rules=secondary_rules,
                    base_gates=list(source["base_gates"]["rules"]),
                )
            )
            conversion = {
                **plan,
                "confirmed": True,
                "target_id": str(created["id"]),
                "target_version": int(created["version"]),
                "target_rule_set_hash": str(created["rule_set_hash"]),
                "final_primary_rules": list(rules),
                "final_secondary_rules": list(secondary_rules or []),
            }
            conversion["threshold_changes"]["primary"].update(
                {
                    "to_minimum": len(rules),
                    "to_total": len(rules),
                }
            )
            conversion["threshold_changes"]["secondary"].update(
                {
                    "to_minimum": len(list(secondary_rules or [])),
                    "to_total": len(list(secondary_rules or [])),
                }
            )
            append_audit_event(
                self.root,
                "RULE_SET_LEGACY_CONVERTED",
                subject_id=rule_set_id,
                version=int(created["version"]),
                details=conversion,
            )
            return {"version": created, "conversion": conversion}

    def clone(
        self,
        rule_set_id: str,
        *,
        source_version: int,
        name: str,
        editor_mode: str | None = None,
        expected_rule_set_hash: str | None = None,
    ) -> dict[str, Any]:
        with _RULE_SET_LOCK:
            source = self.get(rule_set_id, source_version)
            self._assert_hash(source, expected_rule_set_hash)
            self._assert_name_available(name)
            target_mode = str(editor_mode or source.get("editor_mode") or LEGACY_EDITOR_MODE)
            new_id = self._new_rule_set_id()
            conversion: dict[str, Any] | None = None
            if target_mode == SIMPLE_EDITOR_MODE:
                keep_secondary = source.get("editor_mode") == SIMPLE_EDITOR_MODE
                payload = simple_all_rule_set_payload(
                    rule_set_id=new_id,
                    name=name,
                    rules=list(source["scored"]["rules"]),
                    secondary_rules=(
                        list(source["secondary"]["rules"])
                        if keep_secondary
                        else []
                    ),
                    base_gates=list(source["base_gates"]["rules"]),
                )
                if source.get("editor_mode") != SIMPLE_EDITOR_MODE:
                    conversion = {
                        "requested": True,
                        "from_editor_mode": source.get("editor_mode"),
                        "to_editor_mode": SIMPLE_EDITOR_MODE,
                        "kept_scored_rules": list(source["scored"]["rules"]),
                        "dropped_required_rules": list(source["required"]["rules"]),
                        "dropped_veto_rules": list(source["veto"]["rules"]),
                        "dropped_secondary_rules": list(source["secondary"]["rules"]),
                        "dropped_thresholds": True,
                    }
            elif target_mode == LEGACY_EDITOR_MODE:
                payload = {
                    key: deepcopy(value)
                    for key, value in source.items()
                    if key
                    in {
                        "editor_mode",
                        "base_gates",
                        "required",
                        "veto",
                        "scored",
                        "secondary",
                        "ranking",
                        "near_policy",
                        "group_policy",
                    }
                }
                payload.update(
                    {
                        "id": new_id,
                        "name": name,
                        "editor_mode": LEGACY_EDITOR_MODE,
                    }
                )
            else:
                raise RuleSetError(
                    "复制目标编辑模式不受支持。",
                    code="RULE_SET_EDITOR_MODE_INVALID",
                )
            created = self.create(payload)
            append_audit_event(
                self.root,
                "RULE_SET_CLONED",
                subject_id=created["id"],
                version=int(created["version"]),
                details={
                    "source_id": rule_set_id,
                    "source_version": int(source_version),
                    "source_hash": source["rule_set_hash"],
                    "conversion": conversion or {},
                },
            )
            return {"version": created, "conversion": conversion}

    def _resolve_ref(self, ref: str) -> dict[str, Any] | None:
        rule_id, version_text = ref.rsplit("@", 1)
        if rule_id.startswith("system."):
            return None
        try:
            return self.registry.get(rule_id, int(version_text))
        except RuleRegistryError as exc:
            raise RuleSetError(f"规则方案引用无效：{ref}：{exc}。") from None

    def validate(
        self,
        rule_set_id: str,
        version: int | None = None,
        *,
        allow_validated_rules: bool = False,
    ) -> dict[str, Any]:
        definition = self.get(rule_set_id, version)
        refs = []
        for section in ("required", "veto", "scored", "secondary"):
            refs.extend(definition[section]["rules"])
        resolved = [item for item in (self._resolve_ref(ref) for ref in refs) if item]
        allowed_statuses = (
            {"VALIDATED", "ACTIVE"}
            if allow_validated_rules
            else {"ACTIVE"}
        )
        inactive = [
            f"{item['id']}@{item['version']}"
            for item in resolved
            if item.get("status") not in allowed_statuses
        ]
        if inactive:
            raise RuleSetError(
                "规则方案只能引用已激活规则：" + "、".join(inactive) + "。",
                code="RULE_SET_INACTIVE_RULE",
            )
        groups = {
            item["group"]
            for item in resolved
            if item["id"] + f"@{item['version']}" in definition["scored"]["rules"]
        }
        if len(groups) < definition["group_policy"]["minimum_groups"]:
            raise RuleSetError(
                "评分规则覆盖的规则组少于方案要求。",
                code="RULE_SET_GROUP_COVERAGE",
            )
        state = self._state()
        entry = self._entry(state, rule_set_id, int(definition["version"]))
        entry.update(
            {
                "status": "VALIDATED",
                "validated_at": _now(),
                "validation": {
                    "rule_count": len(refs),
                    "scored_rule_count": len(definition["scored"]["rules"]),
                    "group_count": len(groups),
                },
            }
        )
        self._write_state(state)
        append_audit_event(
            self.root,
            "RULE_SET_VALIDATED",
            subject_id=rule_set_id,
            version=int(definition["version"]),
        )
        return self.get(rule_set_id, int(definition["version"]))

    def activate(
        self,
        rule_set_id: str,
        version: int | None = None,
        *,
        expected_active_rule_set_hash: str | None = None,
        expected_active_absent: bool | None = None,
        expected_active_rule_set_id: str | None = None,
        expected_active_rule_set_version: int | None = None,
    ) -> dict[str, Any]:
        with _RULE_SET_LOCK:
            definition = self.get(rule_set_id, version)
            expected_active = str(expected_active_rule_set_hash or "").strip()
            expected_id = str(expected_active_rule_set_id or "").strip()
            expected_version = expected_active_rule_set_version
            strict_expectation = expected_active_absent is not None
            if strict_expectation and not isinstance(expected_active_absent, bool):
                raise RuleSetError(
                    "当前组合并发状态格式不正确。",
                    code="ACTIVE_RULE_SET_EXPECTATION_INVALID",
                )
            if expected_active_absent is True and (
                expected_active or expected_id or expected_version is not None
            ):
                raise RuleSetError(
                    "期望当前组合不存在时，不能同时提交当前组合 ID、版本或摘要。",
                    code="ACTIVE_RULE_SET_EXPECTATION_INVALID",
                )
            if expected_active_absent is False and (
                not expected_active or not expected_id or expected_version is None
            ):
                raise RuleSetError(
                    "期望当前组合存在时，必须同时提交 ID、版本和摘要。",
                    code="ACTIVE_RULE_SET_EXPECTATION_INVALID",
                )
            if expected_active_absent is False and (
                not re.fullmatch(r"[a-z][a-z0-9_]{1,80}", expected_id)
                or not re.fullmatch(r"[a-f0-9]{64}", expected_active)
            ):
                raise RuleSetError(
                    "当前组合 ID 或摘要格式不正确。",
                    code="ACTIVE_RULE_SET_EXPECTATION_INVALID",
                )
            if expected_version is not None:
                if isinstance(expected_version, bool):
                    raise RuleSetError(
                        "当前组合版本格式不正确。",
                        code="ACTIVE_RULE_SET_EXPECTATION_INVALID",
                    )
                try:
                    expected_version = int(expected_version)
                except (TypeError, ValueError):
                    raise RuleSetError(
                        "当前组合版本格式不正确。",
                        code="ACTIVE_RULE_SET_EXPECTATION_INVALID",
                    ) from None
                if expected_version < 1:
                    raise RuleSetError(
                        "当前组合版本格式不正确。",
                        code="ACTIVE_RULE_SET_EXPECTATION_INVALID",
                    )
            if strict_expectation or expected_active:
                try:
                    current_definition = self.active()
                    current_snapshot = {
                        "id": str(current_definition.get("id") or ""),
                        "version": int(current_definition.get("version") or 0),
                        "rule_set_hash": str(
                            current_definition.get("rule_set_hash") or ""
                        ),
                    }
                except RuleSetError as exc:
                    if exc.code != "NO_ACTIVE_RULE_SET":
                        raise
                    current_snapshot = None
                if expected_active_absent is True:
                    expectation_matches = current_snapshot is None
                elif expected_active_absent is False:
                    expectation_matches = bool(
                        current_snapshot
                        and current_snapshot["id"] == expected_id
                        and current_snapshot["version"] == expected_version
                        and current_snapshot["rule_set_hash"] == expected_active
                    )
                else:
                    # 兼容旧调用：非空摘要继续只比较摘要；空串或字段缺失仍表示
                    # 未使用 CAS。新面板必须使用 expected_active_absent 严格字段。
                    expectation_matches = bool(
                        current_snapshot
                        and current_snapshot["rule_set_hash"] == expected_active
                    )
                if not expectation_matches:
                    raise RuleSetError(
                        "当前启用组合已经变化，请重新读取后再激活。",
                        code="ACTIVE_RULE_SET_CHANGED",
                    )
            if definition["status"] not in {"VALIDATED", "ACTIVE"}:
                raise RuleSetError(
                    "规则方案必须先验证，且验证成功不会自动激活。",
                    code="RULE_SET_NOT_VALIDATED",
                )
            state = self._state()
            now = _now()
            for item in self.all_versions():
                is_target = (
                    item["id"] == rule_set_id
                    and int(item["version"]) == int(definition["version"])
                )
                if is_target:
                    continue
                same_family_usable = (
                    item["id"] == rule_set_id
                    and item["status"] in {"VALIDATED", "ACTIVE"}
                )
                if same_family_usable:
                    self._entry(state, item["id"], int(item["version"])).update(
                        {"status": "SUPERSEDED", "superseded_at": now}
                    )
                elif item.get("is_current") or item["status"] == "ACTIVE":
                    self._entry(state, item["id"], int(item["version"])).update(
                        {"status": "VALIDATED", "deactivated_at": now}
                    )
            entry = self._entry(state, rule_set_id, int(definition["version"]))
            entry.update({"status": "ACTIVE", "activated_at": now})
            state["active"] = {"id": rule_set_id, "version": int(definition["version"])}
            self._write_state(state)
            append_audit_event(
                self.root,
                "RULE_SET_ACTIVATED",
                subject_id=rule_set_id,
                version=int(definition["version"]),
            )
            return self.get(rule_set_id, int(definition["version"]))

    def delete(
        self, rule_set_id: str, *, expected_rule_set_hash: str | None = None
    ) -> dict[str, Any]:
        with _RULE_SET_LOCK:
            state = self._state()
            family = state.get("sets", {}).get(rule_set_id, {})
            if self._family_deleted(family):
                return {
                    "deleted": True,
                    "rule_set_id": rule_set_id,
                    "deletion_mode": str(family.get("deletion_mode") or "tombstone"),
                }

            detail = self.detail(rule_set_id)
            self._assert_hash(detail["latest"], expected_rule_set_hash)
            if detail["is_current"]:
                raise RuleSetError(
                    "当前筛选依据不能删除，请先将其他组合设为筛选依据。",
                    code="RULE_SET_ACTIVE",
                )

            versions = self.versions(rule_set_id)
            usage = self._usage_counts()
            pure_draft = self._is_pure_draft_family(rule_set_id, versions, usage)
            deletion_mode = "physical" if pure_draft else "tombstone"
            if pure_draft:
                path = (self.root / versions[0]["source_path"]).resolve()
                allowed_root = self.rule_sets_root.resolve()
                if not path.is_relative_to(allowed_root) or not path.is_file():
                    raise RuleSetError(
                        "规则组合草稿文件不存在。",
                        code="RULE_SET_NOT_FOUND",
                    )
                path.unlink()

            deleted_at = _now()
            family = self._family(state, rule_set_id)
            family.update(
                {
                    "deleted_at": deleted_at,
                    "deletion_mode": deletion_mode,
                    "deleted_latest_version": int(detail["latest_version"]),
                    "deleted_rule_set_hash": str(detail["rule_set_hash"]),
                }
            )
            self._write_state(state)
            append_audit_event(
                self.root,
                "RULE_SET_DELETED",
                subject_id=rule_set_id,
                details={
                    "latest_version": int(detail["latest_version"]),
                    "deletion_mode": deletion_mode,
                },
            )
            return {
                "deleted": True,
                "rule_set_id": rule_set_id,
                "deletion_mode": deletion_mode,
            }

    def resolve(self, definition: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        resolved: dict[str, list[dict[str, Any]]] = {
            "required": [],
            "veto": [],
            "scored": [],
            "secondary": [],
        }
        for section in resolved:
            for ref in definition[section]["rules"]:
                item = self._resolve_ref(ref)
                if item is not None:
                    resolved[section].append(item)
        return resolved
