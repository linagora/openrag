"""Tests for the search-filter injection guard (``validate_search_filter``).

Covers the cross-tenant breakout the guard exists to stop (unbalanced parens
rebalancing the ``(partition …) and (…)`` wrapper) plus the reserved-field and
tautology rules, and confirms legitimate documented filters still pass.
"""

import pytest
from core.utils.exceptions import ValidationError
from core.utils.filter_validation import MAX_FILTER_LENGTH, validate_search_filter


@pytest.mark.parametrize(
    "expr",
    [
        # The audit's headline payload: the trailing `)` closes the partition
        # wrapper early so `or (1==1)` matches every tenant's rows.
        "1==1) or (1==1",
        # Other unbalanced-paren breakouts.
        'file_id == "x") or (1==1',
        ")",
        "(page > 5",
        "page > 5)",
        '((file_id == "x")',
        # Referencing the tenant boundary column directly.
        'partition == "other"',
        'partition in ["a", "b"]',
        'page > 5 or PARTITION == "other"',
        # Bare tautologies.
        "1==1",
        "1 == 1",
        "true",
        "TRUE",
        # Unterminated string literal.
        'file_id == "abc',
    ],
)
def test_rejects_unsafe_filters(expr):
    with pytest.raises(ValidationError) as exc:
        validate_search_filter(expr)
    assert exc.value.status_code == 400


def test_length_bound():
    with pytest.raises(ValidationError):
        validate_search_filter("a" * (MAX_FILTER_LENGTH + 1))


@pytest.mark.parametrize(
    "expr",
    [
        None,
        "",
        'file_id == "abc123"',
        "page >= 5 AND page <= 10",
        'file_id in ["id1", "id2", "id3"]',
        'created_at > ISO "2024-01-01T00:00:00+00:00"',
        # Balanced parens with `or` are safe — confined by the outer `and`.
        "(page > 5 or page < 2)",
        'NOT (file_id == "x")',
        # `partition` only as a substring of another field name is fine.
        'partition_group == "g1"',
        # A quoted `)` / `partition` must not trip the checks.
        'filename == "weird)name"',
        'meta["partition"] == "sub"',
    ],
)
def test_allows_legitimate_filters(expr):
    validate_search_filter(expr)  # must not raise
