"""A tiny, *safe* expression language for organization rules.

Rules live in a config file, so evaluating them with ``eval`` would be a code-
execution hole. Instead we parse the expression to an AST and walk it with a
strict whitelist of node types. No attribute name may start with ``_`` (blocks
``().__class__`` sandbox escapes), and only a fixed set of string/list helper
methods (plus ``len``) may be called.

Supported: and / or / not, comparisons (== != < <= > >= in not-in is is-not),
arithmetic on scalars, membership over literal tuples/lists/sets, attribute
access (``place.city``, ``taken_at.year``), and calls like
``filename.lower()``, ``name.startswith("IMG")``, ``len(tags)``.
"""

from __future__ import annotations

import ast
from typing import Any

# String/sequence methods safe to call in a rule.
_SAFE_METHODS = {
    "lower", "upper", "strip", "lstrip", "rstrip",
    "startswith", "endswith", "count", "find",
    "replace", "title", "capitalize", "casefold",
}
_SAFE_FUNCS = {
    "len": len,
    "int": int,
    "str": str,
    "float": float,
    "bool": bool,
    "abs": abs,
    "min": min,
    "max": max,
    "any": any,
    "all": all,
}

# Note: multiplicative operators (*, /, %, //) and ** are deliberately EXCLUDED.
# They add nothing to organization rules but enable memory/CPU blowups from a
# config file (e.g. `"x" * 10**9`). Only +/- (which don't amplify size) remain.
_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
    ast.USub, ast.UAdd, ast.BinOp, ast.Add, ast.Sub,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
    ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot, ast.Name,
    ast.Load, ast.Attribute, ast.Constant, ast.Tuple, ast.List, ast.Set,
    ast.Call, ast.IfExp,
)


class RuleError(ValueError):
    """A rule expression is malformed or uses a disallowed construct."""


def validate(expr: str) -> None:
    """Raise RuleError if the expression is not a safe, evaluable rule."""
    _compile(expr)


def evaluate(expr: str, context: dict[str, Any]) -> Any:
    """Evaluate ``expr`` against ``context``; missing names resolve to None."""
    tree = _compile(expr)
    return _eval(tree.body, context)


# ---------------------------------------------------------------------------

def _compile(expr: str) -> ast.Expression:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise RuleError(f"syntax error in rule {expr!r}: {e}") from e
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise RuleError(
                f"disallowed expression element {type(node).__name__} in rule {expr!r}"
            )
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise RuleError(f"attribute {node.attr!r} not allowed (underscore) in {expr!r}")
        if isinstance(node, ast.Call):
            _check_call(node, expr)
    return tree


def _check_call(node: ast.Call, expr: str) -> None:
    if node.keywords:
        raise RuleError(f"keyword arguments not allowed in rule {expr!r}")
    func = node.func
    if isinstance(func, ast.Name):
        if func.id not in _SAFE_FUNCS:
            raise RuleError(f"function {func.id!r} not allowed in rule {expr!r}")
    elif isinstance(func, ast.Attribute):
        if func.attr not in _SAFE_METHODS:
            raise RuleError(f"method {func.attr!r} not allowed in rule {expr!r}")
    else:
        raise RuleError(f"unsupported call target in rule {expr!r}")


def _eval(node: ast.AST, ctx: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        return ctx.get(node.id)  # unknown name -> None (rules degrade gracefully)

    if isinstance(node, ast.BoolOp):
        vals = node.values
        if isinstance(node.op, ast.And):
            result: Any = True
            for v in vals:
                result = _eval(v, ctx)
                if not result:
                    return result
            return result
        else:  # Or
            result = False
            for v in vals:
                result = _eval(v, ctx)
                if result:
                    return result
            return result

    if isinstance(node, ast.UnaryOp):
        val = _eval(node.operand, ctx)
        if isinstance(node.op, ast.Not):
            return not val
        if isinstance(node.op, ast.USub):
            return -val
        return +val

    if isinstance(node, ast.BinOp):
        left, right = _eval(node.left, ctx), _eval(node.right, ctx)
        op = node.op
        if isinstance(op, ast.Add):
            return left + right
        if isinstance(op, ast.Sub):
            return left - right

    if isinstance(node, ast.Compare):
        left = _eval(node.left, ctx)
        for op, comp_node in zip(node.ops, node.comparators):
            right = _eval(comp_node, ctx)
            if not _compare(op, left, right):
                return False
            left = right
        return True

    if isinstance(node, ast.IfExp):
        return _eval(node.body, ctx) if _eval(node.test, ctx) else _eval(node.orelse, ctx)

    if isinstance(node, (ast.Tuple, ast.List)):
        return [_eval(e, ctx) for e in node.elts]
    if isinstance(node, ast.Set):
        return {_eval(e, ctx) for e in node.elts}

    if isinstance(node, ast.Attribute):
        obj = _eval(node.value, ctx)
        return getattr(obj, node.attr, None)

    if isinstance(node, ast.Call):
        return _eval_call(node, ctx)

    raise RuleError(f"cannot evaluate node {type(node).__name__}")


def _eval_call(node: ast.Call, ctx: dict[str, Any]) -> Any:
    args = [_eval(a, ctx) for a in node.args]
    func = node.func
    if isinstance(func, ast.Name):
        return _SAFE_FUNCS[func.id](*args)
    # attribute method call, e.g. filename.lower()
    obj = _eval(func.value, ctx)  # type: ignore[attr-defined]
    if obj is None:
        return None
    method = getattr(obj, func.attr, None)  # type: ignore[attr-defined]
    if method is None:
        return None
    return method(*args)


def _compare(op: ast.cmpop, left: Any, right: Any) -> bool:
    # None-tolerant ordering: ordering comparisons with None are simply False.
    if isinstance(op, ast.Eq):
        return left == right
    if isinstance(op, ast.NotEq):
        return left != right
    if isinstance(op, ast.Is):
        return left is right
    if isinstance(op, ast.IsNot):
        return left is not right
    if isinstance(op, ast.In):
        try:
            return left in right
        except TypeError:
            return False
    if isinstance(op, ast.NotIn):
        try:
            return left not in right
        except TypeError:
            return True
    if left is None or right is None:
        return False
    if isinstance(op, ast.Lt):
        return left < right
    if isinstance(op, ast.LtE):
        return left <= right
    if isinstance(op, ast.Gt):
        return left > right
    if isinstance(op, ast.GtE):
        return left >= right
    return False
