"""The file fence recorders (#1430), through the ``prometheus_client`` backend of a plain API process."""

from __future__ import annotations

import prometheus_client
import pytest
from core.observability import file_fence_metrics as module


def _value(name: str, **labels: str) -> float:
    return prometheus_client.REGISTRY.get_sample_value(name, labels) or 0.0


def test_a_conflict_is_counted_with_its_labels() -> None:
    before = _value("openrag_file_fence_conflicts_total", file_operation="delete", holder_kind="metadata")

    module.record_file_fence_conflict(file_operation="delete", holder_kind="metadata")

    assert _value("openrag_file_fence_conflicts_total", file_operation="delete", holder_kind="metadata") == before + 1


def test_a_refused_commit_is_counted() -> None:
    before = _value("openrag_file_fence_commits_refused_total")

    module.record_file_fence_commit_refused()

    assert _value("openrag_file_fence_commits_refused_total") == before + 1


def test_a_renewal_failure_is_counted_by_holder_kind() -> None:
    before = _value("openrag_file_fence_renewal_failures_total", holder_kind="index")

    module.record_file_fence_renewal_failure(holder_kind="index")

    assert _value("openrag_file_fence_renewal_failures_total", holder_kind="index") == before + 1


def test_a_recorder_that_cannot_record_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> None:
        raise RuntimeError("registry gone")

    monkeypatch.setattr(module, "_instruments", broken)

    module.record_file_fence_conflict(file_operation="index", holder_kind="index")
    module.record_file_fence_commit_refused()
    module.record_file_fence_renewal_failure(holder_kind="delete")
