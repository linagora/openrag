"""``apply_parse_memory_limit`` returns the ceiling it really applied (#997)."""

from __future__ import annotations

import resource

import pytest
from core.utils import process_limits
from core.utils.logging import get_logger

logger = get_logger()

_MIB = 1024 * 1024


@pytest.fixture
def applied(monkeypatch) -> list[tuple[int, int]]:
    """Record ``setrlimit`` instead of capping the test process."""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(resource, "getrlimit", lambda _which: (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    monkeypatch.setattr(resource, "setrlimit", lambda _which, limits: calls.append(limits))
    monkeypatch.setattr(process_limits, "child_vmdata_mb", lambda: 100)
    return calls


@pytest.fixture
def logged() -> list[str]:
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="INFO", format="{message}")
    yield messages
    logger.remove(sink_id)


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


@pytest.mark.parametrize("hard", [512 * _MIB, resource.RLIM_INFINITY])
def test_an_inherited_limit_is_never_raised(applied, monkeypatch, hard):
    """A child that inherited a lower limit keeps it: raising the soft limit up
    to the hard one needs no privilege, so asking for more would loosen it."""
    monkeypatch.setattr(resource, "getrlimit", lambda _which: (512 * _MIB, hard))

    assert _apply(1024) == 512
    assert applied == [(512 * _MIB, hard)]


def test_a_lowered_ceiling_says_why(applied, monkeypatch, logged):
    monkeypatch.setattr(resource, "getrlimit", lambda _which: (512 * _MIB, resource.RLIM_INFINITY))

    _apply(1024)

    assert "set to 512 MiB (lowered from 1024 MiB by the limit this child inherited)" in logged[-1]


def test_a_refused_ceiling_does_not_claim_an_inherited_limit_is_gone(applied, monkeypatch, logged):
    monkeypatch.setattr(resource, "getrlimit", lambda _which: (90 * _MIB, resource.RLIM_INFINITY))

    assert _apply(1024) == 0
    assert applied == []
    assert "it keeps the limit it inherited" in logged[-1]
    assert "without a ceiling" not in logged[-1]


def test_a_failed_setrlimit_reports_nothing_applied(monkeypatch):
    def refuse(_which, _limits):
        raise ValueError("not allowed")

    monkeypatch.setattr(resource, "setrlimit", refuse)
    monkeypatch.setattr(process_limits, "child_vmdata_mb", lambda: 100)

    assert _apply(1024) == 0
