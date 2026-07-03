"""The parametric rule engine.

Rules are evaluated top-to-bottom; the first whose ``when`` expression is truthy
wins, and its ``to`` template renders the asset's path under ``organized/``.
Path rendering is deliberately paranoid: every substituted value is stripped of
path separators and ``..``, and the final path is confirmed to stay inside
``organized/`` (a rule template can never write outside the vault).
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
from typing import Any, Optional

from . import ruleeval
from .config import Config, RuleSpec
from .models import Asset

UNKNOWN = "unknown"
# A format spec like ``{n:>900000000}`` would materialize a ~900 MB string inside
# format() *before* sanitization. Cap the numbers a spec may contain.
_MAX_SPEC_NUMBER = 64
_SPEC_DIGITS = re.compile(r"\d+")


@dataclass
class RuleMatch:
    rule_name: Optional[str]
    dest_relpath: str                       # relative to organized/
    tags: list[str] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)
    is_fallback: bool = False
    is_review: bool = False


def validate_rules(cfg: Config) -> list[str]:
    """Return a list of human-readable problems (empty == all valid)."""
    errors: list[str] = []
    for r in cfg.rules:
        try:
            ruleeval.validate(r.when)
        except ruleeval.RuleError as e:
            errors.append(f"rule {r.name!r}: {e}")
        try:
            _fields_in_template(r.to)
        except ValueError as e:
            errors.append(f"rule {r.name!r} template: {e}")
    return errors


def apply_rules(asset: Asset, cfg: Config) -> RuleMatch:
    ctx = asset.rule_context()
    for r in cfg.rules:
        try:
            matched = bool(ruleeval.evaluate(r.when, ctx))
        except ruleeval.RuleError:
            matched = False
        if matched:
            rel, unknown = _render(r.to, ctx)
            return RuleMatch(
                rule_name=r.name, dest_relpath=rel, tags=list(r.tags),
                unknown_fields=unknown, is_review=bool(unknown),
            )

    # Nothing matched -> organized fallback (still structured + searchable).
    rel, unknown = _render(cfg.fallback_to, ctx)
    # If even the fallback can't place it (no date, no kind), route to review.
    if unknown:
        review_rel, _ = _render(cfg.review_to, ctx)
        return RuleMatch(
            rule_name=None, dest_relpath=review_rel, is_fallback=True,
            unknown_fields=unknown, is_review=True,
        )
    return RuleMatch(rule_name=None, dest_relpath=rel, is_fallback=True)


# ---- template rendering ----------------------------------------------------

_FMT = string.Formatter()


def _fields_in_template(template: str) -> list[str]:
    fields = []
    for _lit, field_name, spec, _conv in _FMT.parse(template):
        if field_name is None:
            continue
        if field_name == "":
            raise ValueError("positional {} fields are not allowed; use named fields")
        if spec and _spec_is_abusive(spec):
            raise ValueError(f"format spec {spec!r} exceeds the max width {_MAX_SPEC_NUMBER}")
        fields.append(field_name)
    return fields


def _spec_is_abusive(spec: str) -> bool:
    return any(int(m) > _MAX_SPEC_NUMBER for m in _SPEC_DIGITS.findall(spec))


def _render(template: str, ctx: dict[str, Any]) -> tuple[str, list[str]]:
    """Render a path template. Returns (safe_relpath, unknown_field_names)."""
    out: list[str] = []
    unknown: list[str] = []
    for literal, field_name, spec, _conv in _FMT.parse(template):
        out.append(literal)
        if field_name is None:
            continue
        value = _resolve(field_name, ctx)
        if value is None or value == "":
            unknown.append(field_name)
            out.append(UNKNOWN)
            continue
        out.append(_format_value(value, spec))
    rendered = "".join(out)
    return _sanitize_relpath(rendered), unknown


def _resolve(dotted: str, ctx: dict[str, Any]) -> Any:
    head, _, rest = dotted.partition(".")
    obj: Any = ctx.get(head)
    while rest and obj is not None:
        attr, _, rest = rest.partition(".")
        obj = getattr(obj, attr, None)
    return obj


def _format_value(value: Any, spec: str) -> str:
    # Defense in depth: even if an abusive spec slips past validation, never let
    # format() allocate a giant string here.
    if spec and _spec_is_abusive(spec):
        spec = ""
    if spec:
        try:
            text = format(value, spec)
        except (ValueError, TypeError):
            text = str(value)
    else:
        text = str(value)
    return _sanitize_component(text)


def _sanitize_component(text: str) -> str:
    """A single substituted value must never introduce path structure."""
    text = text.replace("/", "-").replace("\\", "-").replace("\x00", "")
    text = text.strip().strip(".")            # no leading/trailing dots (blocks .., hidden)
    return text or UNKNOWN


def _sanitize_relpath(rel: str) -> str:
    """Collapse a rendered template into a safe relative path.

    Drops empty and dot segments and any ``..``; guarantees a relative result
    that stays under organized/. Literal '/' from the template are the only
    directory separators (component values were already de-slashed)."""
    parts = []
    for seg in rel.replace("\\", "/").split("/"):
        seg = seg.strip()
        if seg in ("", ".", ".."):
            continue
        parts.append(seg)
    return "/".join(parts) if parts else UNKNOWN
