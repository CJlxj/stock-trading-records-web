from __future__ import annotations

import ast
from functools import reduce
import math
import operator
from typing import Any

import numpy as np
import pandas as pd

from src.indicators import atr as indicator_atr
from src.indicators import ema as indicator_ema
from src.indicators import rsi as indicator_rsi
from src.rules.expression_parser import ParsedExpression, parse_expression


class ExpressionRuntimeError(ValueError):
    pass


class ExpressionDataGap(ExpressionRuntimeError):
    pass


def _series(value: Any) -> pd.Series:
    if isinstance(value, pd.Series):
        return value
    if isinstance(value, (list, tuple, np.ndarray)):
        return pd.Series(value)
    raise ExpressionRuntimeError("指标函数需要一列时序数据。")


def _period(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ExpressionRuntimeError("时序函数周期必须是整数。") from None
    if not 1 <= parsed <= 2_000:
        raise ExpressionRuntimeError("时序函数周期必须位于 1 到 2000 之间。")
    return parsed


def _latest(value: Any) -> Any:
    if isinstance(value, pd.Series):
        if value.empty:
            raise ExpressionDataGap("规则没有可用的最新值。")
        value = value.iloc[-1]
    if isinstance(value, np.generic):
        value = value.item()
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        raise ExpressionDataGap("规则最新值缺失或不是有限数值。")
    try:
        if pd.isna(value):
            raise ExpressionDataGap("规则最新值缺失。")
    except (TypeError, ValueError):
        pass
    return value


def _json_scalar(value: Any) -> Any:
    value = _latest(value)
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, float, np.number)):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ExpressionDataGap("规则观察值不是有限数值。")
        return round(parsed, 8)
    return value


def _logical(values: list[Any], mode: str) -> Any:
    if not values:
        raise ExpressionRuntimeError(f"{mode} 至少需要一个条件。")
    if any(isinstance(value, pd.Series) for value in values):
        normalized = [
            value.astype(bool) if isinstance(value, pd.Series) else bool(value)
            for value in values
        ]
        operation = operator.and_ if mode == "all" else operator.or_
        return reduce(operation, normalized)
    return all(bool(value) for value in values) if mode == "all" else any(
        bool(value) for value in values
    )


def _call(name: str, args: list[Any]) -> Any:
    if name in {"all", "any"}:
        return _logical(args, name)
    if name == "between":
        if len(args) != 3:
            raise ExpressionRuntimeError("between 需要值、下限和上限三个参数。")
        return (args[0] >= args[1]) & (args[0] <= args[2])
    if name == "ma":
        if len(args) != 2:
            raise ExpressionRuntimeError("ma 需要序列和周期两个参数。")
        period = _period(args[1])
        return _series(args[0]).rolling(period, min_periods=period).mean()
    if name == "ema":
        if len(args) != 2:
            raise ExpressionRuntimeError("ema 需要序列和周期两个参数。")
        return indicator_ema(_series(args[0]), _period(args[1]))
    if name == "rsi":
        if len(args) != 2:
            raise ExpressionRuntimeError("rsi 需要序列和周期两个参数。")
        return indicator_rsi(_series(args[0]), _period(args[1]))
    if name == "atr":
        if len(args) != 4:
            raise ExpressionRuntimeError("atr 需要 high、low、close 和周期。")
        frame = pd.DataFrame(
            {"high": _series(args[0]), "low": _series(args[1]), "close": _series(args[2])}
        )
        return indicator_atr(frame, _period(args[3]))
    if name == "obv":
        if len(args) != 2:
            raise ExpressionRuntimeError("obv 需要 close 和 volume。")
        close, volume = _series(args[0]), _series(args[1])
        return (np.sign(close.diff()).fillna(0) * volume).cumsum()
    if name == "prev":
        if len(args) != 2:
            raise ExpressionRuntimeError("prev 需要序列和回看周期。")
        return _series(args[0]).shift(_period(args[1]))
    if name in {"highest", "lowest"}:
        if len(args) != 2:
            raise ExpressionRuntimeError(f"{name} 需要序列和周期。")
        series, period = _series(args[0]), _period(args[1])
        rolling = series.rolling(period, min_periods=period)
        return rolling.max() if name == "highest" else rolling.min()
    if name == "pct_change":
        if len(args) != 2:
            raise ExpressionRuntimeError("pct_change 需要序列和周期。")
        return _series(args[0]).pct_change(_period(args[1]), fill_method=None)
    if name == "abs":
        if len(args) != 1:
            raise ExpressionRuntimeError("abs 只接受一个参数。")
        return abs(args[0])
    raise ExpressionRuntimeError(f"未实现的规则函数：{name}。")


class _Evaluator:
    def __init__(self, env: dict[str, Any]) -> None:
        self.env = env
        self.traces: list[dict[str, Any]] = []

    def visit(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return self.visit(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in self.env:
                raise ExpressionDataGap(f"规则缺少字段或参数：{node.id}。")
            return self.env[node.id]
        if isinstance(node, ast.Call):
            return _call(node.func.id, [self.visit(argument) for argument in node.args])
        if isinstance(node, ast.BoolOp):
            values = [self.visit(value) for value in node.values]
            return _logical(values, "all" if isinstance(node.op, ast.And) else "any")
        if isinstance(node, ast.UnaryOp):
            value = self.visit(node.operand)
            if isinstance(node.op, ast.Not):
                return ~value.astype(bool) if isinstance(value, pd.Series) else not bool(value)
            if isinstance(node.op, ast.USub):
                return -value
            if isinstance(node.op, ast.UAdd):
                return +value
        if isinstance(node, ast.BinOp):
            left, right = self.visit(node.left), self.visit(node.right)
            if isinstance(node.op, ast.Div):
                denominator = right
                if isinstance(denominator, pd.Series):
                    if (denominator == 0).any():
                        raise ExpressionRuntimeError("规则表达式出现除零。")
                elif denominator == 0:
                    raise ExpressionRuntimeError("规则表达式出现除零。")
                return left / right
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
        if isinstance(node, ast.Compare):
            left_value = self.visit(node.left)
            current = left_value
            results: list[Any] = []
            operator_labels = {
                ast.Eq: "==",
                ast.NotEq: "!=",
                ast.Lt: "<",
                ast.LtE: "<=",
                ast.Gt: ">",
                ast.GtE: ">=",
            }
            for operation, comparator in zip(node.ops, node.comparators):
                right_value = self.visit(comparator)
                if isinstance(operation, ast.Eq):
                    result = current == right_value
                elif isinstance(operation, ast.NotEq):
                    result = current != right_value
                elif isinstance(operation, ast.Lt):
                    result = current < right_value
                elif isinstance(operation, ast.LtE):
                    result = current <= right_value
                elif isinstance(operation, ast.Gt):
                    result = current > right_value
                elif isinstance(operation, ast.GtE):
                    result = current >= right_value
                else:
                    raise ExpressionRuntimeError("不支持的比较运算。")
                self.traces.append(
                    {
                        "expression": ast.unparse(node),
                        "left": _json_scalar(current),
                        "operator": operator_labels[type(operation)],
                        "right": _json_scalar(right_value),
                        "result": bool(_latest(result)),
                    }
                )
                results.append(result)
                current = right_value
            return _logical(results, "all")
        raise ExpressionRuntimeError(f"运行时不支持 {type(node).__name__}。")


def evaluate_expression(
    expression: str | ParsedExpression,
    frame: pd.DataFrame,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parsed = (
        expression
        if isinstance(expression, ParsedExpression)
        else parse_expression(
            expression,
            allowed_names=set(frame.columns) | set((params or {}).keys()),
        )
    )
    environment = {str(column): frame[column] for column in frame.columns}
    environment.update(params or {})
    evaluator = _Evaluator(environment)
    result = evaluator.visit(parsed.tree)
    boolean_result = bool(_latest(result))
    observed = {
        name: _json_scalar(environment[name])
        for name in parsed.names
        if name in environment
    }
    return {
        "boolean_result": boolean_result,
        "normalized_expression": parsed.normalized,
        "observed": observed,
        "comparisons": evaluator.traces,
    }
