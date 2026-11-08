"""Guarded expressions for compute steps.

An alert can be imported from someone else, so an expression stored in one is
data that will be executed. Before anything runs, the expression is parsed and
every node checked against an allow-list: names, constants, arithmetic,
comparisons, boolean logic, a conditional, and calls to a few named functions.
Nothing that can reach an attribute, a subscript or a builtin gets through.

Evaluation is vectorised over a DataFrame: names are columns, ``and``/``or``/
``not`` become element-wise ``&``/``|``/``~`` so they work on whole columns.
"""
from __future__ import annotations

import ast
import operator

import numpy as np
import pandas as pd

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod, ast.Pow: operator.pow}
_CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
        ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}


def _isnull(x):
    return pd.isna(x)


def _fn_min(a, b):
    return np.minimum(a, b)


def _fn_max(a, b):
    return np.maximum(a, b)


def _fn_round(a, n=0):
    return np.round(a, int(n))


def _notnull(x):
    r = pd.isna(x)
    return ~r if isinstance(r, (pd.Series, np.ndarray)) else (not r)


FUNCTIONS = {"abs": np.abs, "min": _fn_min, "max": _fn_max, "round": _fn_round,
             "isnull": _isnull, "notnull": _notnull}


class ExpressionError(ValueError):
    pass


def _check(node: ast.AST, names: set[str]) -> None:
    if isinstance(node, ast.Expression):
        return _check(node.body, names)
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float, str, bool)) and node.value is not None:
            raise ExpressionError(f"constant {node.value!r} is not allowed")
        return
    if isinstance(node, ast.Name):
        names.add(node.id)
        return
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        _check(node.left, names); _check(node.right, names)
        return
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
        return _check(node.operand, names)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        for v in node.values:
            _check(v, names)
        return
    if isinstance(node, ast.Compare) and all(type(o) in _CMP for o in node.ops):
        _check(node.left, names)
        for c in node.comparators:
            _check(c, names)
        return
    if isinstance(node, ast.IfExp):
        _check(node.test, names); _check(node.body, names); _check(node.orelse, names)
        return
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
            raise ExpressionError("only these functions can be called: " + ", ".join(sorted(FUNCTIONS)))
        if node.keywords:
            raise ExpressionError("keyword arguments are not allowed")
        for a in node.args:
            _check(a, names)
        return
    raise ExpressionError(f"'{ast.unparse(node) if hasattr(ast, 'unparse') else node}' "
                          f"uses something expressions cannot ({type(node).__name__})")


def parse(expr: str) -> tuple[ast.Expression, set[str]]:
    """Parse and vet an expression; returns the tree and the column names it uses."""
    text = (expr or "").strip()
    if not text:
        raise ExpressionError("the expression is empty")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"not a valid expression: {exc.msg}") from None
    names: set[str] = set()
    _check(tree, names)
    return tree, names - set(FUNCTIONS) - {"True", "False", "None"}


def evaluate(expr: str, df: pd.DataFrame):
    """The expression's value over ``df`` — a Series, or a scalar for a constant."""
    tree, names = parse(expr)
    missing = sorted(n for n in names if n not in df.columns)
    if missing:
        raise ExpressionError(f"unknown column(s): {', '.join(missing)} "
                              f"(available: {', '.join(map(str, df.columns)) or 'none'})")
    return _eval(tree.body, df)


def _eval(node, df):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id == "True":
            return True
        if node.id == "False":
            return False
        return df[node.id]
    if isinstance(node, ast.BinOp):
        return _BIN[type(node.op)](_eval(node.left, df), _eval(node.right, df))
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, df)
        if isinstance(node.op, ast.USub):
            return -v
        if isinstance(node.op, ast.UAdd):
            return +v
        return ~v if isinstance(v, pd.Series) else (not v)
    if isinstance(node, ast.BoolOp):
        vals = [_eval(v, df) for v in node.values]
        out = vals[0]
        for v in vals[1:]:
            if isinstance(node.op, ast.And):
                out = (out & v) if isinstance(out, pd.Series) or isinstance(v, pd.Series) else (out and v)
            else:
                out = (out | v) if isinstance(out, pd.Series) or isinstance(v, pd.Series) else (out or v)
        return out
    if isinstance(node, ast.Compare):
        left = _eval(node.left, df)
        result = None
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, df)
            part = _CMP[type(op)](left, right)
            result = part if result is None else (result & part)
            left = right
        return result
    if isinstance(node, ast.IfExp):
        test = _eval(node.test, df)
        body, orelse = _eval(node.body, df), _eval(node.orelse, df)
        if isinstance(test, pd.Series):
            return pd.Series(np.where(test.fillna(False).astype(bool), body, orelse), index=df.index)
        return body if test else orelse
    if isinstance(node, ast.Call):
        return FUNCTIONS[node.func.id](*[_eval(a, df) for a in node.args])
    raise ExpressionError(f"cannot evaluate {type(node).__name__}")  # pragma: no cover
