from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Iterable

from src.rules.technical_fields import TECHNICAL_FIELD_NAMES


MAX_EXPRESSION_LENGTH = 2_000
MAX_AST_NODES = 160
MAX_AST_DEPTH = 18

ALLOWED_FUNCTIONS = {
    "all",
    "any",
    "between",
    "ma",
    "ema",
    "rsi",
    "atr",
    "obv",
    "prev",
    "highest",
    "lowest",
    "pct_change",
    "abs",
}

DEFAULT_NAMES = TECHNICAL_FIELD_NAMES

ALLOWED_NODE_TYPES = (
    ast.Expression,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Call,
    ast.Compare,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.UnaryOp,
    ast.UAdd,
    ast.USub,
    ast.Not,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
)


class ExpressionSyntaxError(ValueError):
    """Raised when a DSL expression is outside the explicit safe subset."""

    def __init__(self, message: str, code: str = "DSL_INVALID") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ParsedExpression:
    source: str
    normalized: str
    tree: ast.Expression
    names: tuple[str, ...]
    functions: tuple[str, ...]


def _depth(node: ast.AST) -> int:
    children = list(ast.iter_child_nodes(node))
    return 1 if not children else 1 + max(_depth(child) for child in children)


def parse_expression(
    expression: str,
    *,
    allowed_names: Iterable[str] | None = None,
) -> ParsedExpression:
    source = str(expression or "").strip()
    if not source:
        raise ExpressionSyntaxError("规则表达式不能为空。", "DSL_EMPTY")
    if len(source) > MAX_EXPRESSION_LENGTH:
        raise ExpressionSyntaxError("规则表达式过长。", "DSL_TOO_LONG")
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ExpressionSyntaxError(
            f"规则表达式语法错误：{exc.msg}。", "DSL_SYNTAX"
        ) from None

    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_AST_NODES:
        raise ExpressionSyntaxError("规则表达式包含过多节点。", "DSL_TOO_COMPLEX")
    if _depth(tree) > MAX_AST_DEPTH:
        raise ExpressionSyntaxError("规则表达式嵌套过深。", "DSL_TOO_DEEP")

    valid_names = set(DEFAULT_NAMES)
    if allowed_names is not None:
        valid_names.update(str(name) for name in allowed_names)
    used_names: set[str] = set()
    used_functions: set[str] = set()

    for node in nodes:
        if not isinstance(node, ALLOWED_NODE_TYPES):
            if isinstance(node, ast.Attribute):
                code = "DSL_ATTRIBUTE_FORBIDDEN"
                message = "规则表达式不允许属性访问。"
            elif isinstance(node, ast.Subscript):
                code = "DSL_SUBSCRIPT_FORBIDDEN"
                message = "规则表达式不允许下标访问；请使用显式时序函数。"
            else:
                code = "DSL_NODE_FORBIDDEN"
                message = f"规则表达式不允许 {type(node).__name__}。"
            raise ExpressionSyntaxError(message, code)

        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                continue
            if not isinstance(node.value, (int, float)):
                raise ExpressionSyntaxError(
                    "规则表达式只允许数字和布尔常量。", "DSL_CONSTANT_FORBIDDEN"
                )

        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in ALLOWED_FUNCTIONS:
                raise ExpressionSyntaxError(
                    "规则表达式调用了未注册函数。", "DSL_FUNCTION_FORBIDDEN"
                )
            if node.keywords:
                raise ExpressionSyntaxError(
                    "规则函数只允许位置参数。", "DSL_KEYWORD_FORBIDDEN"
                )
            used_functions.add(node.func.id)

        if isinstance(node, ast.Name):
            parent_is_call = any(
                isinstance(parent, ast.Call) and parent.func is node
                for parent in nodes
            )
            if parent_is_call:
                continue
            if node.id.startswith("_") or node.id not in valid_names:
                raise ExpressionSyntaxError(
                    f"规则表达式引用了未注册字段或参数：{node.id}。",
                    "DSL_NAME_FORBIDDEN",
                )
            used_names.add(node.id)

    normalized = ast.unparse(tree).strip()
    return ParsedExpression(
        source=source,
        normalized=normalized,
        tree=tree,
        names=tuple(sorted(used_names)),
        functions=tuple(sorted(used_functions)),
    )
