from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float):
        if pd.isna(value) or value in {float("inf"), float("-inf")}:
            return None
    return value


def main() -> int:
    if len(sys.argv) != 2:
        raise RuntimeError("missing plugin path")
    path = Path(sys.argv[1]).resolve()
    payload = json.loads(sys.stdin.read())
    frame = pd.DataFrame(payload.get("frame") or [])
    params = payload.get("params") or {}
    spec = importlib.util.spec_from_file_location("_local_rule_plugin", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load plugin")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    raw = module.evaluate(frame.copy(deep=True), dict(params))
    if isinstance(raw, bool):
        result = {"boolean_result": raw, "observed": {}}
    elif isinstance(raw, dict):
        result = dict(raw)
    else:
        raise RuntimeError("plugin result must be bool or dict")
    if "boolean_result" not in result:
        raise RuntimeError("plugin result missing boolean_result")
    sys.stdout.write(json.dumps(_json_safe(result), ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
