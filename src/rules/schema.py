from __future__ import annotations

import math
import re
from typing import Any

from src.rules.expression_parser import parse_expression


RULE_ID_PATTERN = re.compile(r"^(?:[a-z][a-z0-9_]*\.)?[a-z][a-z0-9_]{1,80}$")
RULE_KINDS = {"base_gate", "required", "scored", "veto", "diagnostic"}
IMPLEMENTATION_TYPES = {"expression", "python_plugin"}
MISSING_POLICIES = {"DATA_GAP", "FAIL", "SKIP"}
LIFECYCLE_STATES = {"DRAFT", "VALIDATED", "ACTIVE", "SUPERSEDED", "ARCHIVED"}
RESULT_STATES = {"PASS", "FAIL", "DATA_GAP", "ERROR", "SKIPPED"}


class RuleSchemaError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "RULE_SCHEMA_INVALID",
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


def _text(value: Any, field: str, *, maximum: int = 500) -> str:
    parsed = str(value or "").strip()
    if not parsed:
        raise RuleSchemaError(f"{field}不能为空。", field=field)
    if len(parsed) > maximum:
        raise RuleSchemaError(f"{field}过长。", field=field)
    return parsed


def _positive_int(value: Any, field: str, *, maximum: int = 5_000) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise RuleSchemaError(f"{field}必须是整数。", field=field) from None
    if not 1 <= parsed <= maximum:
        raise RuleSchemaError(f"{field}超出允许范围。", field=field)
    return parsed


def _number(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise RuleSchemaError(f"{field}必须是数字。", field=field) from None
    if not math.isfinite(parsed):
        raise RuleSchemaError(f"{field}必须是有限数字。", field=field)
    return parsed


def validate_params_schema(raw: Any) -> dict[str, dict[str, Any]]:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise RuleSchemaError("params_schema 必须是对象。", field="params_schema")
    result: dict[str, dict[str, Any]] = {}
    for raw_name, raw_spec in raw.items():
        name = str(raw_name)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,60}", name):
            raise RuleSchemaError(f"参数名不合法：{name}。", field="params_schema")
        if not isinstance(raw_spec, dict):
            raise RuleSchemaError(f"参数 {name} 的定义必须是对象。", field="params_schema")
        value_type = str(raw_spec.get("type") or "number")
        if value_type not in {"number", "integer"}:
            raise RuleSchemaError(
                f"参数 {name} 只支持 number 或 integer。", field="params_schema"
            )
        default = _number(raw_spec.get("default"), f"参数 {name} 默认值")
        minimum = _number(raw_spec.get("min", default), f"参数 {name} 最小值")
        maximum = _number(raw_spec.get("max", default), f"参数 {name} 最大值")
        if minimum > default or default > maximum:
            raise RuleSchemaError(
                f"参数 {name} 必须满足最小值 <= 默认值 <= 最大值。",
                field="params_schema",
            )
        if value_type == "integer" and not float(default).is_integer():
            raise RuleSchemaError(f"参数 {name} 默认值必须是整数。", field="params_schema")
        result[name] = {
            "type": value_type,
            "default": int(default) if value_type == "integer" else default,
            "min": int(minimum) if value_type == "integer" else minimum,
            "max": int(maximum) if value_type == "integer" else maximum,
        }
    return result


def parameter_values(
    definition: dict[str, Any],
    overrides: dict[str, Any] | None = None,
) -> dict[str, int | float]:
    values: dict[str, int | float] = {}
    provided = overrides or {}
    for name, spec in definition.get("params_schema", {}).items():
        raw = provided.get(name, spec["default"])
        number = _number(raw, f"参数 {name}")
        if not spec["min"] <= number <= spec["max"]:
            raise RuleSchemaError(
                f"参数 {name} 必须位于 {spec['min']} 到 {spec['max']} 之间。",
                field=f"params.{name}",
            )
        if spec["type"] == "integer":
            if not number.is_integer():
                raise RuleSchemaError(f"参数 {name} 必须是整数。", field=f"params.{name}")
            values[name] = int(number)
        else:
            values[name] = number
    unknown = sorted(set(provided) - set(definition.get("params_schema", {})))
    if unknown:
        raise RuleSchemaError(
            "存在未声明参数：" + "、".join(unknown) + "。", field="params"
        )
    return values


def normalize_rule_definition(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RuleSchemaError("规则定义必须是对象。")
    rule_id = _text(raw.get("id"), "id", maximum=96)
    if not RULE_ID_PATTERN.fullmatch(rule_id):
        raise RuleSchemaError(
            "规则 ID 只能使用小写字母、数字、下划线和可选命名空间。",
            field="id",
        )
    version = _positive_int(raw.get("version", 1), "version", maximum=1_000_000)
    name = _text(raw.get("name") or raw.get("label"), "name", maximum=80)
    description = _text(raw.get("description"), "description", maximum=500)
    kind = str(raw.get("kind") or "scored")
    if kind not in RULE_KINDS:
        raise RuleSchemaError("规则 kind 不受支持。", field="kind")
    group = _text(raw.get("group") or "custom", "group", maximum=80)
    family = _text(raw.get("family") or "custom", "family", maximum=80)
    timeframe = str(raw.get("timeframe") or "day")
    if timeframe != "day":
        raise RuleSchemaError("当前正式筛选只支持 day 周期。", field="timeframe")
    lookback = _positive_int(raw.get("lookback", 1), "lookback")
    inputs_raw = raw.get("inputs") or []
    if not isinstance(inputs_raw, list) or not inputs_raw:
        raise RuleSchemaError("规则至少需要一个输入字段。", field="inputs")
    inputs = []
    for value in inputs_raw:
        item = str(value)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,60}", item):
            raise RuleSchemaError(f"输入字段不合法：{item}。", field="inputs")
        if item not in inputs:
            inputs.append(item)
    params_schema = validate_params_schema(raw.get("params_schema"))
    implementation_raw = raw.get("implementation")
    if not isinstance(implementation_raw, dict):
        raise RuleSchemaError("implementation 必须是对象。", field="implementation")
    implementation_type = str(implementation_raw.get("type") or "")
    if implementation_type not in IMPLEMENTATION_TYPES:
        raise RuleSchemaError("规则实现方式不受支持。", field="implementation.type")
    implementation: dict[str, Any] = {"type": implementation_type}
    if implementation_type == "expression":
        expression = _text(
            implementation_raw.get("expression"),
            "implementation.expression",
            maximum=2_000,
        )
        parsed = parse_expression(
            expression,
            allowed_names=set(inputs) | set(params_schema),
        )
        undeclared_names = sorted(
            set(parsed.names) - set(inputs) - set(params_schema)
        )
        if undeclared_names:
            raise RuleSchemaError(
                "表达式使用了未声明输入：" + "、".join(undeclared_names) + "。",
                field="inputs",
            )
        implementation.update(
            {
                "expression": expression,
                "normalized_expression": parsed.normalized,
            }
        )
    else:
        implementation["path"] = _text(
            implementation_raw.get("path"), "implementation.path", maximum=300
        )
        implementation["symbol"] = _text(
            implementation_raw.get("symbol") or "evaluate",
            "implementation.symbol",
            maximum=100,
        )
    missing_policy = str(raw.get("missing_policy") or "DATA_GAP").upper()
    if missing_policy not in MISSING_POLICIES:
        raise RuleSchemaError("missing_policy 不受支持。", field="missing_policy")
    plain_template = _text(
        raw.get("plain_template") or name, "plain_template", maximum=300
    )
    tests = raw.get("tests") or []
    if not isinstance(tests, list):
        raise RuleSchemaError("tests 必须是数组。", field="tests")
    status = str(raw.get("status") or "DRAFT").upper()
    if status not in LIFECYCLE_STATES:
        raise RuleSchemaError("规则状态不受支持。", field="status")
    return {
        "schema_version": 1,
        "id": rule_id,
        "version": version,
        "name": name,
        "label": name,
        "description": description,
        "kind": kind,
        "group": group,
        "group_label": str(raw.get("group_label") or group),
        "family": family,
        "level": str(raw.get("level") or "自定义"),
        "timeframe": timeframe,
        "lookback": lookback,
        "inputs": inputs,
        "params_schema": params_schema,
        "implementation": implementation,
        "missing_policy": missing_policy,
        "plain_template": plain_template,
        "tests": tests,
        "status": status,
        "visual_definition": raw.get("visual_definition"),
    }
