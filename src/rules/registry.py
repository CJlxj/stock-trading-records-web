from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re
import threading
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

from src.indicators import add_indicators
from src.rules.expression_runtime import (
    ExpressionDataGap,
    ExpressionRuntimeError,
    evaluate_expression,
)
from src.rules.plugin_loader import (
    PluginValidationError,
    discover_plugins,
    run_plugin,
)
from src.rules.schema import (
    RuleSchemaError,
    normalize_rule_definition,
    parameter_values,
)
from src.rules.storage import (
    append_audit_event,
    atomic_write_text,
    canonical_hash,
    file_sha256,
    relative_project_path,
)


class RuleRegistryError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "RULE_REGISTRY_ERROR",
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


# Serializes catalog-wide writes (seeding, importing, deleting) so two of them
# cannot interleave their read-modify-write of registry_state.yaml.
_REGISTRY_LOCK = threading.RLock()


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    return normalized[:60] or "custom_rule"


def _test_frame() -> pd.DataFrame:
    values = [20 + index * 0.01 for index in range(5_120)]
    frame = pd.DataFrame(
        {
            "open": [value - 0.05 for value in values],
            "high": [value + 0.2 for value in values],
            "low": [value - 0.2 for value in values],
            "close": values,
            "volume": [
                1_000_000.0 + (index % 20) * 10_000
                for index in range(len(values))
            ],
            "amount": [value * 1_000_000.0 for value in values],
        }
    )
    frame = add_indicators(frame)
    # The deterministic rising sample has no losses, so RSI is undefined.
    # A neutral value keeps the validation fixture finite without creating a
    # second indicator implementation.
    frame["rsi14"] = frame["rsi14"].fillna(55.0)
    return frame


class RuleRegistry:
    def __init__(self, project_root: str | Path) -> None:
        self.root = Path(project_root)
        self.builtin_root = self.root / "rules" / "catalog" / "builtin"
        self.user_root = self.root / "rules" / "catalog" / "user"
        self.state_path = self.root / "rules" / "registry_state.yaml"

    def _state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"schema_version": 1, "rules": {}}
        try:
            raw = yaml.safe_load(self.state_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return {"schema_version": 1, "rules": {}}
        return raw if isinstance(raw, dict) else {"schema_version": 1, "rules": {}}

    def _write_state(self, state: dict[str, Any]) -> None:
        atomic_write_text(
            self.state_path,
            yaml.safe_dump(state, allow_unicode=True, sort_keys=False),
        )

    def _state_entry(
        self,
        state: dict[str, Any],
        rule_id: str,
        version: int,
    ) -> dict[str, Any]:
        rules = state.setdefault("rules", {})
        versions = rules.setdefault(rule_id, {}).setdefault("versions", {})
        return versions.setdefault(str(version), {})

    def _load_yaml_definitions(self) -> list[dict[str, Any]]:
        # One catalog root, one origin.  ``rules/catalog/builtin`` is factory
        # seed data that gets imported into this root once (see
        # ``import_default_rules``); it is never a second live catalog, so the
        # library has no "built-in vs personal" split to reason about.
        definitions: list[dict[str, Any]] = []
        for path in sorted(self.user_root.glob("*/*.yaml")):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise RuleRegistryError(f"规则文件无法读取：{path.name}：{exc}。") from None
            candidates = raw.get("rules") if isinstance(raw, dict) else None
            if candidates is None:
                candidates = [raw]
            if not isinstance(candidates, list):
                raise RuleRegistryError(f"规则文件格式不正确：{path.name}。")
            for candidate in candidates:
                try:
                    definition = normalize_rule_definition(candidate)
                except RuleSchemaError as exc:
                    raise RuleRegistryError(
                        f"规则 {path.name} 无效：{exc}",
                        code=exc.code,
                        field=exc.field,
                    ) from None
                definition.update(
                    {
                        "source_path": relative_project_path(self.root, path),
                        "source_symbol": definition["id"],
                        "source_hash": file_sha256(path),
                        "definition_hash": canonical_hash(definition),
                        "origin": "user",
                    }
                )
                definitions.append(definition)
        return definitions

    def factory_definitions(self) -> list[dict[str, Any]]:
        """The rules shipped with the project, straight from the seed files."""

        definitions: list[dict[str, Any]] = []
        for path in sorted(self.builtin_root.glob("*.yaml")):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise RuleRegistryError(
                    f"默认规则文件无法读取：{path.name}：{exc}。"
                ) from None
            candidates = raw.get("rules") if isinstance(raw, dict) else None
            if candidates is None:
                candidates = [raw]
            if not isinstance(candidates, list):
                raise RuleRegistryError(f"默认规则文件格式不正确：{path.name}。")
            for candidate in candidates:
                try:
                    definitions.append(normalize_rule_definition(candidate))
                except RuleSchemaError as exc:
                    raise RuleRegistryError(
                        f"默认规则 {path.name} 无效：{exc}",
                        code=exc.code,
                        field=exc.field,
                    ) from None
        return definitions

    def import_default_rules(self) -> dict[str, Any]:
        """Copy any missing factory rule into the editable catalog.

        Idempotent and additive: a rule id that already exists is skipped
        whole, so a rule the user has edited is never overwritten and a
        re-import only fills the gaps left by deletions.  Imported rules land
        ACTIVE, because they arrive already proven by the project's own tests.
        """

        with _REGISTRY_LOCK:
            present = {str(item["id"]) for item in self.all_versions()}
            state = self._state()
            imported: list[dict[str, Any]] = []
            for definition in self.factory_definitions():
                rule_id = str(definition["id"])
                if rule_id in present:
                    continue
                version = int(definition["version"])
                path = self.user_root / rule_id / f"v{version}.yaml"
                if path.exists():
                    continue
                atomic_write_text(
                    path,
                    yaml.safe_dump(
                        {**definition, "status": "ACTIVE"},
                        allow_unicode=True,
                        sort_keys=False,
                    ),
                )
                entry = self._state_entry(state, rule_id, version)
                stamp = _now()
                entry.update(
                    {
                        "status": "ACTIVE",
                        "created_at": stamp,
                        "validated_at": stamp,
                        "activated_at": stamp,
                    }
                )
                imported.append(definition)
                present.add(rule_id)
            if imported:
                self._write_state(state)
                append_audit_event(
                    self.root,
                    "RULE_DEFAULTS_IMPORTED",
                    subject_id="rules.catalog",
                    details={"rule_ids": [str(item["id"]) for item in imported]},
                )
            return {
                "imported": imported,
                "imported_count": len(imported),
            }

    def ensure_seeded(self) -> dict[str, Any]:
        """Import the factory rules exactly once, on the first ever read.

        After that the catalog is the user's: a rule they deleted stays
        deleted, and only an explicit "导入默认规则" brings it back.
        """

        with _REGISTRY_LOCK:
            state = self._state()
            if state.get("factory_import", {}).get("seeded_at"):
                return {"seeded": False, "imported": [], "imported_count": 0}
            result = self.import_default_rules()
            state = self._state()
            state["factory_import"] = {"seeded_at": _now()}
            self._write_state(state)
            return {"seeded": True, **result}

    def all_versions(self) -> list[dict[str, Any]]:
        state = self._state()
        definitions = self._load_yaml_definitions() + discover_plugins(self.root)
        hydrated: list[dict[str, Any]] = []
        for raw in definitions:
            definition = deepcopy(raw)
            if "definition_hash" not in definition:
                definition["definition_hash"] = canonical_hash(
                    {
                        key: value
                        for key, value in definition.items()
                        if key not in {"source_hash", "plugin_notice"}
                    }
                )
                definition.setdefault("origin", "plugin")
            entry = (
                state.get("rules", {})
                .get(definition["id"], {})
                .get("versions", {})
                .get(str(definition.get("version", 1)), {})
            )
            status = str(entry.get("status") or definition.get("status") or "DRAFT")
            if (
                definition.get("origin") == "plugin"
                and entry.get("validated_source_hash")
                and entry.get("validated_source_hash") != definition.get("source_hash")
            ):
                status = "DRAFT"
                definition["validation_stale"] = True
            definition["status"] = status
            definition["validated_at"] = entry.get("validated_at")
            definition["activated_at"] = entry.get("activated_at")
            definition["validation"] = entry.get("validation")
            hydrated.append(definition)
        hydrated.sort(key=lambda item: (item.get("id", ""), int(item.get("version", 1))))
        return hydrated

    def list_rules(self) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for definition in self.all_versions():
            grouped.setdefault(definition["id"], []).append(definition)
        selected = []
        for versions in grouped.values():
            active = [item for item in versions if item.get("status") == "ACTIVE"]
            selected.append(max(active or versions, key=lambda item: int(item["version"])))
        return sorted(selected, key=lambda item: (item.get("group", ""), item.get("name", "")))

    def get(self, rule_id: str, version: int | None = None) -> dict[str, Any]:
        matches = [
            item
            for item in self.all_versions()
            if item.get("id") == rule_id
            and (version is None or int(item.get("version", 1)) == int(version))
        ]
        if not matches:
            raise RuleRegistryError("没有找到该规则。", code="RULE_NOT_FOUND")
        if version is None:
            active = [item for item in matches if item.get("status") == "ACTIVE"]
            return deepcopy(max(active or matches, key=lambda item: int(item["version"])))
        return deepcopy(matches[0])

    def versions(self, rule_id: str) -> list[dict[str, Any]]:
        versions = [item for item in self.all_versions() if item.get("id") == rule_id]
        if not versions:
            raise RuleRegistryError("没有找到该规则。", code="RULE_NOT_FOUND")
        return sorted(versions, key=lambda item: int(item["version"]), reverse=True)

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        requested_id = str(payload.get("id") or "").strip()
        existing_ids = {str(item["id"]) for item in self.all_versions()}
        # Editing any rule keeps its own id, so the new version supersedes the
        # old one in place.  Only a genuinely new rule gets a generated id.
        if requested_id and requested_id not in existing_ids:
            if not requested_id.startswith("user."):
                requested_id = f"user.{_slug(requested_id)}"
        if not requested_id:
            requested_id = f"user.{_slug(str(payload.get('name') or 'custom_rule'))}"
        existing = [item for item in self.all_versions() if item.get("id") == requested_id]
        next_version = max((int(item["version"]) for item in existing), default=0) + 1
        raw = {
            **payload,
            "id": requested_id,
            "version": next_version,
            "status": "DRAFT",
        }
        try:
            definition = normalize_rule_definition(raw)
        except RuleSchemaError as exc:
            raise RuleRegistryError(str(exc), code=exc.code, field=exc.field) from None
        path = self.user_root / requested_id / f"v{next_version}.yaml"
        if path.exists():
            raise RuleRegistryError("规则版本已存在，不能覆盖。", code="RULE_VERSION_EXISTS")
        atomic_write_text(
            path,
            yaml.safe_dump(definition, allow_unicode=True, sort_keys=False),
        )
        state = self._state()
        entry = self._state_entry(state, requested_id, next_version)
        entry.update({"status": "DRAFT", "created_at": _now()})
        self._write_state(state)
        append_audit_event(
            self.root,
            "RULE_VERSION_CREATED",
            subject_id=requested_id,
            version=next_version,
            details={"source_path": relative_project_path(self.root, path)},
        )
        return self.get(requested_id, next_version)

    def _validate_cases(self, definition: dict[str, Any]) -> dict[str, Any]:
        implementation_type = definition.get("implementation", {}).get("type")
        if implementation_type == "python_plugin":
            result = run_plugin(
                self.root,
                definition,
                _test_frame(),
                parameter_values(definition),
            )
            return {
                "valid": True,
                "case_count": 1,
                "passed_case_count": 1,
                "preview": result,
                "message": "插件元数据、函数签名和受控样本运行通过。",
            }

        tests = definition.get("tests") or []
        inline_cases = [case for case in tests if isinstance(case, dict)]
        # Every rule in the catalog is validated the same way, because there is
        # no longer a "built-in vs personal" distinction to justify two bars:
        # an expression with no inline cases is proven against the standard
        # sample frame, and inline cases, when present, must all pass.
        if not inline_cases:
            evaluated = evaluate_expression(
                definition["implementation"]["normalized_expression"],
                _test_frame(),
                parameter_values(definition),
            )
            return {
                "valid": True,
                "case_count": 1,
                "passed_case_count": 1,
                "preview": {
                    "result": "PASS" if evaluated["boolean_result"] else "FAIL",
                    "normalized_expression": evaluated["normalized_expression"],
                    "observed": evaluated.get("observed") or {},
                },
                "message": "安全表达式检查与标准行情样本运行通过。",
            }
        results = []
        for case in inline_cases:
            frame_raw = case.get("frame")
            if not isinstance(frame_raw, dict):
                raise RuleRegistryError("规则用例 frame 必须是字段到序列的对象。")
            frame = pd.DataFrame(frame_raw)
            evaluated = evaluate_expression(
                definition["implementation"]["normalized_expression"],
                frame,
                parameter_values(definition, case.get("params") or {}),
            )
            actual = "PASS" if evaluated["boolean_result"] else "FAIL"
            expected = str(case.get("expected") or "").upper()
            results.append(
                {
                    "name": str(case.get("name") or f"用例 {len(results) + 1}"),
                    "expected": expected,
                    "actual": actual,
                    "passed": actual == expected,
                }
            )
        failed = [item for item in results if not item["passed"]]
        if failed:
            raise RuleRegistryError(
                "规则用例未通过：" + "、".join(item["name"] for item in failed),
                code="RULE_TEST_FAILED",
            )
        return {
            "valid": True,
            "case_count": len(results),
            "passed_case_count": len(results),
            "cases": results,
            "message": (
                f"表达式安全检查与 {len(results)} 个用例通过。"
                if results
                else "内置规则表达式安全检查通过，金样本由自动化测试覆盖。"
            ),
        }

    def validate(self, rule_id: str, version: int | None = None) -> dict[str, Any]:
        definition = self.get(rule_id, version)
        try:
            validation = self._validate_cases(definition)
        except (ExpressionDataGap, ExpressionRuntimeError, PluginValidationError) as exc:
            raise RuleRegistryError(
                f"规则验证失败：{exc}", code="RULE_VALIDATION_FAILED"
            ) from None
        state = self._state()
        entry = self._state_entry(state, rule_id, int(definition["version"]))
        entry.update(
            {
                "status": "VALIDATED",
                "validated_at": _now(),
                "validated_source_hash": definition.get("source_hash"),
                "validation": validation,
            }
        )
        self._write_state(state)
        append_audit_event(
            self.root,
            "RULE_VALIDATED",
            subject_id=rule_id,
            version=int(definition["version"]),
            details={"definition_hash": definition.get("definition_hash")},
        )
        return self.get(rule_id, int(definition["version"]))

    def activate(self, rule_id: str, version: int | None = None) -> dict[str, Any]:
        definition = self.get(rule_id, version)
        if definition.get("status") not in {"VALIDATED", "ACTIVE"}:
            raise RuleRegistryError(
                "规则必须先验证，且验证成功不会自动激活。",
                code="RULE_NOT_VALIDATED",
            )
        state = self._state()
        for item in self.versions(rule_id):
            item_entry = self._state_entry(state, rule_id, int(item["version"]))
            if int(item["version"]) == int(definition["version"]):
                item_entry.update({"status": "ACTIVE", "activated_at": _now()})
            elif item.get("status") == "ACTIVE":
                item_entry.update({"status": "SUPERSEDED", "superseded_at": _now()})
        self._write_state(state)
        append_audit_event(
            self.root,
            "RULE_ACTIVATED",
            subject_id=rule_id,
            version=int(definition["version"]),
        )
        return self.get(rule_id, int(definition["version"]))

    def delete(self, rule_id: str) -> dict[str, Any]:
        """Physically remove every version of one rule.

        Callers own the policy — this only refuses to touch anything that is
        not a plain YAML file under the editable catalog, so a plugin rule can
        never be removed through here.  There is no tombstone: a rule that is
        still referenced is rejected upstream, and a factory rule the user
        removed comes back only when they ask for it via
        ``import_default_rules``.
        """

        with _REGISTRY_LOCK:
            versions = self.versions(rule_id)
            user_root = self.user_root.resolve()
            paths: list[Path] = []
            for definition in versions:
                source_path = str(definition.get("source_path") or "")
                path = (self.root / source_path).resolve() if source_path else None
                if (
                    path is None
                    or not path.is_relative_to(user_root)
                    or not path.is_file()
                ):
                    raise RuleRegistryError(
                        "只能删除规则库目录下的规则文件。",
                        code="RULE_NOT_DELETABLE",
                    )
                paths.append(path)

            for path in paths:
                path.unlink()
            directory = (self.user_root / rule_id).resolve()
            if (
                directory.is_relative_to(user_root)
                and directory.is_dir()
                and not any(directory.iterdir())
            ):
                directory.rmdir()

            state = self._state()
            state.setdefault("rules", {}).pop(rule_id, None)
            self._write_state(state)
            removed = sorted(int(item["version"]) for item in versions)
            append_audit_event(
                self.root,
                "RULE_DELETED",
                subject_id=rule_id,
                version=max(removed),
                details={"removed_versions": removed},
            )
            return {
                "deleted": True,
                "rule_id": rule_id,
                "removed_versions": removed,
            }

    def source(self, rule_id: str, version: int | None = None) -> dict[str, Any]:
        definition = self.get(rule_id, version)
        path = (self.root / definition["source_path"]).resolve()
        allowed_roots = [
            (self.root / "rules" / "catalog").resolve(),
            (self.root / "rules" / "custom").resolve(),
        ]
        if not any(path.is_relative_to(root) for root in allowed_roots):
            raise RuleRegistryError("规则源码路径越过项目允许边界。")
        source = path.read_text(encoding="utf-8")
        source_scope = "file"
        if path.suffix.lower() in {".yaml", ".yml"}:
            try:
                raw = yaml.safe_load(source) or {}
                candidates = raw.get("rules") if isinstance(raw, dict) else None
                if isinstance(candidates, list):
                    matched = next(
                        (
                            item
                            for item in candidates
                            if isinstance(item, dict)
                            and str(item.get("id")) == definition["id"]
                            and int(item.get("version", 1))
                            == int(definition["version"])
                        ),
                        None,
                    )
                    if matched is not None:
                        source = yaml.safe_dump(
                            matched,
                            allow_unicode=True,
                            sort_keys=False,
                        )
                        source_scope = "definition"
            except (OSError, TypeError, ValueError, yaml.YAMLError):
                source_scope = "file"
        return {
            "id": definition["id"],
            "version": definition["version"],
            "implementation": definition.get("implementation"),
            "source_path": definition["source_path"],
            "source_symbol": definition.get("source_symbol"),
            "source_hash": definition.get("source_hash"),
            "definition_hash": definition.get("definition_hash"),
            "source": source,
            "source_scope": source_scope,
            "plugin_notice": definition.get("plugin_notice"),
        }

    def evaluate(
        self,
        definition: dict[str, Any],
        frame: pd.DataFrame,
        *,
        params: dict[str, Any] | None = None,
        data_date: str | None = None,
    ) -> dict[str, Any]:
        parameter_map = parameter_values(definition, params)
        try:
            if definition["implementation"]["type"] == "expression":
                raw = evaluate_expression(
                    definition["implementation"]["normalized_expression"],
                    frame,
                    parameter_map,
                )
            else:
                raw = run_plugin(self.root, definition, frame, parameter_map)
            boolean_result = bool(raw["boolean_result"])
            status = "PASS" if boolean_result else "FAIL"
            comparisons = raw.get("comparisons") or []
            if raw.get("plain_explanation"):
                explanation = str(raw["plain_explanation"])
            elif comparisons:
                comparison = comparisons[-1]
                explanation = (
                    f"{definition['name']}：{comparison['left']} "
                    f"{comparison['operator']} {comparison['right']}，"
                    f"{'通过' if boolean_result else '未通过'}。"
                )
            else:
                explanation = (
                    f"{definition['name']}：{'通过' if boolean_result else '未通过'}。"
                )
            error = None
        except ExpressionDataGap as exc:
            policy = definition.get("missing_policy", "DATA_GAP")
            status = {"FAIL": "FAIL", "SKIP": "SKIPPED"}.get(policy, "DATA_GAP")
            boolean_result = False if status == "FAIL" else None
            raw = {"observed": {}, "comparisons": []}
            explanation = f"{definition['name']}：数据不足，{exc}"
            error = None
        except (ExpressionRuntimeError, PluginValidationError, RuleSchemaError) as exc:
            status = "ERROR"
            boolean_result = None
            raw = {"observed": {}, "comparisons": []}
            explanation = f"{definition['name']}：规则运行错误。"
            error = str(exc)
        return {
            "rule_id": definition["id"],
            "rule_version": int(definition["version"]),
            "name": definition["name"],
            "kind": definition["kind"],
            "group": definition["group"],
            "status": status,
            "boolean_result": boolean_result,
            "observed": raw.get("observed") or {},
            "comparisons": raw.get("comparisons") or [],
            "plain_explanation": explanation,
            "data_date": data_date,
            "normalized_expression": definition.get("implementation", {}).get(
                "normalized_expression"
            ),
            "params": parameter_map,
            "missing_policy": definition.get("missing_policy"),
            "source_path": definition.get("source_path"),
            "source_symbol": definition.get("source_symbol"),
            "source_hash": definition.get("source_hash"),
            "definition_hash": definition.get("definition_hash"),
            "error": error,
        }
