from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

import pandas as pd

from src.rules.schema import RuleSchemaError, normalize_rule_definition
from src.rules.storage import file_sha256, relative_project_path


class PluginValidationError(ValueError):
    pass


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def inspect_plugin(project_root: str | Path, path: str | Path) -> dict[str, Any]:
    root = Path(project_root)
    custom_root = (root / "rules" / "custom").resolve()
    plugin_path = Path(path)
    if not plugin_path.is_absolute():
        plugin_path = root / plugin_path
    resolved = plugin_path.resolve()
    if not _inside(resolved, custom_root) or resolved.suffix != ".py":
        raise PluginValidationError("插件路径必须位于 rules/custom/ 且为 Python 文件。")
    if not resolved.is_file():
        raise PluginValidationError("插件文件不存在。")
    try:
        source = resolved.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(resolved))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise PluginValidationError(f"插件源码无法解析：{exc}。") from None

    metadata: dict[str, Any] | None = None
    function_found = False
    for node in tree.body:
        if (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and (
                any(isinstance(target, ast.Name) and target.id == "RULE_META" for target in node.targets)
                if isinstance(node, ast.Assign)
                else isinstance(node.target, ast.Name) and node.target.id == "RULE_META"
            )
        ):
            value = node.value
            try:
                literal = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                raise PluginValidationError("RULE_META 必须是可静态读取的字面量字典。") from None
            if not isinstance(literal, dict):
                raise PluginValidationError("RULE_META 必须是字典。")
            metadata = literal
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "evaluate":
            function_found = True
            if len(node.args.args) < 2:
                raise PluginValidationError("evaluate 必须接收 frame 和 params。")
    if metadata is None:
        raise PluginValidationError("插件缺少 RULE_META。")
    if not function_found:
        raise PluginValidationError("插件缺少 evaluate(frame, params)。")

    raw = {
        **metadata,
        "implementation": {
            "type": "python_plugin",
            "path": relative_project_path(root, resolved),
            "symbol": "evaluate",
        },
        "status": str(metadata.get("status") or "DRAFT"),
    }
    try:
        definition = normalize_rule_definition(raw)
    except RuleSchemaError as exc:
        raise PluginValidationError(str(exc)) from None
    definition.update(
        {
            "source_path": relative_project_path(root, resolved),
            "source_symbol": "evaluate",
            "source_hash": file_sha256(resolved),
            "plugin_notice": (
                "Python 插件在限时子进程中运行，但这不是绝对安全沙箱；"
                "只应加载本人检查过的本地代码。"
            ),
        }
    )
    return definition


def discover_plugins(project_root: str | Path) -> list[dict[str, Any]]:
    root = Path(project_root)
    custom_root = root / "rules" / "custom"
    if not custom_root.exists():
        return []
    discovered: list[dict[str, Any]] = []
    for path in sorted(custom_root.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            discovered.append(inspect_plugin(root, path))
        except PluginValidationError as exc:
            discovered.append(
                {
                    "id": f"invalid.{path.stem}",
                    "version": 1,
                    "name": path.stem,
                    "status": "DRAFT",
                    "implementation": {"type": "python_plugin"},
                    "source_path": relative_project_path(root, path),
                    "source_hash": file_sha256(path),
                    "load_error": str(exc),
                }
            )
    return discovered


def _resource_limits() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
        memory_limit = 512 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    except (ImportError, OSError, ValueError):
        pass


def run_plugin(
    project_root: str | Path,
    definition: dict[str, Any],
    frame: pd.DataFrame,
    params: dict[str, Any],
    *,
    timeout_seconds: float = 3.0,
) -> dict[str, Any]:
    root = Path(project_root)
    relative = definition.get("implementation", {}).get("path")
    plugin_path = (root / str(relative or "")).resolve()
    custom_root = (root / "rules" / "custom").resolve()
    if not _inside(plugin_path, custom_root):
        raise PluginValidationError("插件真实路径越过 rules/custom/ 边界。")
    if file_sha256(plugin_path) != definition.get("source_hash"):
        raise PluginValidationError("插件源码已变化，请重新载入并验证。")

    payload = {
        "frame": frame.replace({float("inf"): None, float("-inf"): None})
        .where(pd.notna(frame), None)
        .to_dict(orient="records"),
        "params": params,
    }
    runner = Path(__file__).with_name("plugin_runner.py")
    with tempfile.TemporaryDirectory(prefix="stock-rule-plugin-") as temp_dir:
        input_path = Path(temp_dir) / "input.json"
        output_path = Path(temp_dir) / "output.json"
        input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with input_path.open("rb") as input_handle, output_path.open("wb") as output_handle:
            try:
                completed = subprocess.run(
                    [sys.executable, "-I", str(runner), str(plugin_path)],
                    stdin=input_handle,
                    stdout=output_handle,
                    stderr=subprocess.PIPE,
                    cwd=temp_dir,
                    env={"PYTHONIOENCODING": "utf-8", "PATH": os.defpath},
                    timeout=timeout_seconds,
                    check=False,
                    preexec_fn=_resource_limits if os.name == "posix" else None,
                )
            except subprocess.TimeoutExpired:
                raise PluginValidationError("插件运行超时。") from None
        stderr = completed.stderr.decode("utf-8", errors="replace")[:2_000]
        if completed.returncode != 0:
            raise PluginValidationError(
                f"插件运行失败：{stderr or f'退出码 {completed.returncode}'}"
            )
        if output_path.stat().st_size > 1024 * 1024:
            raise PluginValidationError("插件输出超过 1 MB 限制。")
        try:
            result = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise PluginValidationError("插件没有返回有效 JSON。") from None
    if not isinstance(result, dict) or "boolean_result" not in result:
        raise PluginValidationError("插件返回值缺少 boolean_result。")
    result["boolean_result"] = bool(result["boolean_result"])
    return result
