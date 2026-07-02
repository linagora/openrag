"""Validation for caller-supplied Milvus filter expressions on search routes.

The public search routes accept a raw ``filter`` expression that the vector
store appends to the partition-scoped query as
``(partition in [...]) and (<filter>)``. That wrapping only confines the
caller's expression while its parentheses are *balanced*: Milvus binds ``and``
tighter than ``or``, so an unbalanced ``)`` can close the partition wrapper
early and an ``or`` tautology then matches every row —

    filter = 1==1) or (1==1
    -> (partition in ["mine"]) and (1==1) or (1==1)   # always true

returning other tenants' chunks (``partition`` is a Milvus partition_key —
routing, not a hard boundary). This module rejects such expressions before they
reach the store.

Guarantees enforced (quote-aware, so operators/parens inside string literals
are ignored):

* **Balanced parentheses** — the caller's expression stays confined to the
  single ``and``-joined operand it is wrapped in and cannot rebalance the
  partition wrapper. This is the core isolation guarantee.
* **No ``partition`` reference** — the tenant boundary is set by the route and
  auth, never by the caller (defense-in-depth).
* A bounded length, and a best-effort reject of trivial tautologies
  (``1==1`` / ``true``). The tautology reject is defense-in-depth only —
  tenant isolation is guaranteed by the two rules above, not by it, since an
  always-true predicate is still confined to the caller's partitions by the
  outer ``and``.

``or`` / ``not`` remain allowed: once parentheses are balanced, the top-level
``and`` with the partition clause confines the result set to the caller's
partitions regardless of the operators inside their expression.
"""

from __future__ import annotations

from core.utils.exceptions import ValidationError

#: Upper bound on a caller-supplied filter expression (chars).
MAX_FILTER_LENGTH = 2048

#: Whitespace-stripped, lowercased expressions that match every row.
_TAUTOLOGIES = frozenset({"true", "1==1"})

#: The partition_key column — the tenant boundary, never caller-filterable.
_RESERVED_FIELDS = frozenset({"partition"})


def _reject(reason: str) -> None:
    raise ValidationError(
        f"Invalid filter expression: {reason}",
        status_code=400,
        code="INVALID_FILTER",
    )


def _strip_wrapping_parens(s: str) -> str:
    """Strip parentheses that wrap the *entire* string, one layer at a time.

    ``(1==1)`` -> ``1==1``; ``((true))`` -> ``true``. A leading ``(`` that
    closes before the end (e.g. ``(a)and(b)``) is left untouched. Used so the
    tautology check can't be defeated by simply wrapping the expression in
    parentheses.
    """
    while len(s) >= 2 and s[0] == "(" and s[-1] == ")":
        depth = 0
        wraps = True
        for i, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(s) - 1:
                    wraps = False
                    break
        if not wraps:
            break
        s = s[1:-1]
    return s


def validate_search_filter(expr: str | None) -> None:
    """Reject an unsafe caller-supplied Milvus filter (raises HTTP 400).

    A ``None`` or empty expression is valid (no extra filtering). Raises
    :class:`~core.utils.exceptions.ValidationError` (status 400) otherwise.
    """
    if not expr:
        return
    if len(expr) > MAX_FILTER_LENGTH:
        _reject("expression too long")

    depth = 0
    quote: str | None = None
    escaped = False
    token: list[str] = []
    identifiers: list[str] = []

    for ch in expr:
        if quote is not None:
            # Inside a string literal: ignore parens/operators, only track the
            # closing quote and backslash escapes so a quoted `)` or `partition`
            # cannot trip the checks (or hide a real breakout).
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                _reject("unbalanced parentheses")
        if ch.isalnum() or ch == "_":
            token.append(ch)
        elif token:
            identifiers.append("".join(token))
            token = []

    if quote is not None:
        _reject("unterminated string literal")
    if depth != 0:
        _reject("unbalanced parentheses")
    if token:
        identifiers.append("".join(token))

    if any(ident.lower() in _RESERVED_FIELDS for ident in identifiers):
        _reject("`partition` is not a permitted filter field")

    # Best-effort tautology reject (defense-in-depth only — tenant isolation is
    # guaranteed by the balanced-paren + reserved-field rules above, not by
    # this). Strip whitespace and any wrapping parens so `(1==1)` / `( true )`
    # can't trivially slip past the literal match.
    if _strip_wrapping_parens("".join(expr.split()).lower()) in _TAUTOLOGIES:
        _reject("tautological expression")


__all__ = ["validate_search_filter", "MAX_FILTER_LENGTH"]
