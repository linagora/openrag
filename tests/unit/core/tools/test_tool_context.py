"""Unit tests for ``core.tools.context``: the request's scope and the turn's numbered sources."""

from __future__ import annotations

import pytest
from core.chat.scope import Scope
from core.tools import SourceList, ToolContext


def test_a_source_keeps_its_number_when_shown_again():
    sources = SourceList()

    assert sources.number("chunk a", key="a") == 1
    assert sources.number("chunk b", key="b") == 2
    assert sources.number("chunk a, found again", key="a") == 1
    assert sources.entries == ["chunk a", "chunk b"]
    assert len(sources) == 2


def test_a_number_maps_back_to_its_source():
    sources = SourceList()
    sources.number("chunk a", key="a")

    assert sources.get(1) == "chunk a"
    assert sources.get(0) is None
    assert sources.get(2) is None


@pytest.mark.parametrize(
    ("partitions", "partition", "allowed"),
    [
        (["hr", "legal"], "hr", True),
        (["hr"], "finance", False),
        (["all"], "finance", True),
        (None, "hr", False),
    ],
)
def test_a_partition_is_allowed_only_within_the_scope(partitions, partition, allowed):
    ctx = ToolContext(user={"id": 1}, scope=Scope(partitions=partitions))

    assert ctx.allows_partition(partition) is allowed


def test_a_workspace_or_attachments_narrow_the_files_too():
    ctx = ToolContext(user={"id": 1}, scope=Scope(partitions=["hr"], file_ids=["f1"], kind="workspace"))

    assert ctx.allows_file("hr", "f1")
    assert not ctx.allows_file("hr", "f2")
    assert not ctx.allows_file("legal", "f1")


def test_each_context_has_its_own_sources():
    first = ToolContext(user={"id": 1}, scope=Scope(partitions=["hr"]))
    second = ToolContext(user={"id": 1}, scope=Scope(partitions=["hr"]))
    first.sources.number("chunk", key="a")

    assert len(second.sources) == 0
