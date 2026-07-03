"""The rule evaluator is fed config-file expressions, so its sandbox is
security-critical. These tests lock the sandbox shut."""

import pytest

from ark import ruleeval
from ark.ruleeval import RuleError, evaluate, validate


def test_basic_boolean_and_compare():
    ctx = {"kind": "image", "year": 2025}
    assert evaluate('kind == "image" and year >= 2020', ctx) is True
    assert evaluate('kind == "video" or year == 2025', ctx) is True
    assert evaluate('not (kind == "image")', ctx) is False


def test_membership_and_none_tolerance():
    assert evaluate("month in (11, 12, 1)", {"month": 12}) is True
    assert evaluate("month in (11, 12, 1)", {"month": None}) is False
    # ordering against a missing (None) field must not raise, just be False
    assert evaluate("year > 2000", {"year": None}) is False


def test_attribute_and_method_calls():
    class NS:
        city = "Goa"
    ctx = {"place": NS(), "filename": "INVOICE_acme.pdf"}
    assert evaluate('place.city == "Goa"', ctx) is True
    assert evaluate('"invoice" in filename.lower()', ctx) is True
    assert evaluate("len(filename) > 3", ctx) is True


def test_missing_name_is_none_not_error():
    assert evaluate("nonexistent", {}) is None
    assert evaluate('nonexistent == "x"', {}) is False


# ---- security: the sandbox must reject escape attempts --------------------

@pytest.mark.parametrize("expr", [
    "().__class__",
    "().__class__.__bases__",
    "__import__('os')",
    "open('/etc/passwd')",
    "().__class__.__mro__[1].__subclasses__()",
    "globals()",
    "eval('1')",
    "(1).__reduce__()",
    "[].__class__",
    "lambda: 1",
    "filename.__class__",
    "x if True else __import__('os')",
])
def test_sandbox_rejects_escapes(expr):
    with pytest.raises(RuleError):
        validate(expr)
    with pytest.raises(RuleError):
        evaluate(expr, {"filename": "a", "x": 1})


def test_disallowed_function_call_rejected():
    with pytest.raises(RuleError):
        validate("print('hi')")
    with pytest.raises(RuleError):
        validate("exec('x=1')")


def test_syntax_error_is_ruleerror():
    with pytest.raises(RuleError):
        validate("kind == ")
