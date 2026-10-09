"""``apply_parse_memory_limit`` returns the ceiling it really applied (#997)."""

from __future__ import annotations

import resource

import pytest
from core.utils import process_limits

_MIB = 1024 * 1024


@pytest.fixture
def applied(monkeypatch) -> list[tuple[int, int]]:
    """Record ``setrlimit`` instead of capping the test process."""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(resource, "getrlimit", lambda _which: (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    monkeypatch.setattr(resource, "setrlimit", lambda _which, limits: calls.append(limits))
    monkeypatch.setattr(process_limits, "child_vmdata_mb", lambda: 100)
    return calls


def _apply(memory_limit_mb: int) -> int:
    return process_limits.apply_parse_memory_limit(
        memory_limit_mb, process="Test child", setting="TEST_LIMIT_MB", min_headroom_mb=128
    )


def test_off_applies_nothing(applied):
    assert _apply(0) == 0
    assert applied == []


def test_a_ceiling_above_the_baseline_is_applied_as_asked(applied):
    assert _apply(1024) == 1024
    assert applied[0][0] == 1024 * _MIB


def test_a_ceiling_at_the_baseline_is_refused(applied):
    assert _apply(100) == 0
    assert applied == []


def test_an_existing_hard_limit_clamps_the_ceiling(applied, monkeypatch):
    monkeypatch.setattr(resource, "getrlimit", lambda _which: (resource.RLIM_INFINITY, 512 * _MIB))

    assert _apply(1024) == 512
    assert applied == [(512 * _MIB, 512 * _MIB)]


def test_a_failed_setrlimit_reports_nothing_applied(monkeypatch):
    def refuse(_which, _limits):
        raise ValueError("not allowed")

    monkeypatch.setattr(resource, "setrlimit", refuse)
    monkeypatch.setattr(process_limits, "child_vmdata_mb", lambda: 100)

    assert _apply(1024) == 0
