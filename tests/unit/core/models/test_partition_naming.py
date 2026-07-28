"""Which partition names are spoken for.

The two rules read alike but are not the same: ``all`` collides with a sentinel
and is closed to everyone, while an internal prefix is a namespace whose owner
is allowed in. Conflating them either hands users an invisible partition or
stops a run from creating its own.
"""

from __future__ import annotations

import pytest
from core.models.evaluation import EVAL_PARTITION_PREFIX
from core.models.partition import (
    INTERNAL_PARTITION_PREFIXES,
    is_internal_partition,
    is_reserved_partition_name,
)


def test_the_eval_namespace_is_registered_as_internal():
    """The registry is what the generic call sites consult; a namespace missing
    from it is one no listing filters and no creation path reserves."""
    assert EVAL_PARTITION_PREFIX in INTERNAL_PARTITION_PREFIXES


@pytest.mark.parametrize("name", ["__eval_deadbeef", "__eval_"])
def test_internal_partitions_are_recognised(name):
    assert is_internal_partition(name)


@pytest.mark.parametrize("name", ["p1", "eval_x", "_eval_x", "all", "__evaluation"])
def test_ordinary_partitions_are_not_internal(name):
    assert not is_internal_partition(name)


@pytest.mark.parametrize("name", ["all", "ALL", "  all  "])
def test_the_all_sentinel_is_reserved_however_it_is_spelled(name):
    assert is_reserved_partition_name(name)


@pytest.mark.parametrize("name", ["all", "ALL", "  all  "])
def test_the_all_sentinel_stays_reserved_for_internal_callers(name):
    """It would expand a listing to every partition — wanted by no caller."""
    assert is_reserved_partition_name(name, allow_internal=True)


@pytest.mark.parametrize("name", ["__eval_mine", "__EVAL_mine", "  __eval_mine  "])
def test_the_internal_prefix_is_reserved_against_ordinary_callers(name):
    """Case and whitespace are normalised, so a name cannot be spelled to sit
    just outside the check and become a partition no listing shows."""
    assert is_reserved_partition_name(name)


def test_a_namespace_owner_may_claim_its_own_prefix():
    assert not is_reserved_partition_name("__eval_deadbeef", allow_internal=True)


@pytest.mark.parametrize("name", ["p1", "eval_x", "my-partition"])
def test_ordinary_names_are_not_reserved(name):
    assert not is_reserved_partition_name(name)
