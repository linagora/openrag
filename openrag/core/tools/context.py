"""What a tool call runs with besides its arguments: who asks, what they may search, the turn's sources."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.chat.scope import Scope

if TYPE_CHECKING:
    from core.retrieval.trace import RetrievalTraceBuilder


class SourceList:
    """The sources a turn's tools showed the model, numbered ``[Source N]`` from 1.

    A source is numbered the first time a tool shows it and keeps that number:
    the same chunk found by two searches is one source, so the model's
    citations map back to one entry. The list is in number order, which is what
    the citation filter and ``extra.sources`` read.
    """

    def __init__(self) -> None:
        self._entries: list[Any] = []
        self._numbers: dict[str, int] = {}

    def number(self, source: Any, *, key: str) -> int:
        """The number of *source*, identified by *key* (a chunk id, a URL), adding it if new."""
        number = self._numbers.get(key)
        if number is None:
            self._entries.append(source)
            number = self._numbers[key] = len(self._entries)
        return number

    def get(self, number: int) -> Any | None:
        """The source shown as ``[Source number]``, ``None`` if there is none."""
        return self._entries[number - 1] if 1 <= number <= len(self._entries) else None

    @property
    def entries(self) -> list[Any]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


@dataclass(frozen=True)
class ToolContext:
    """The request a tool call belongs to.

    *scope* is what the request may search, settled before the agent runs; a
    tool never widens it, whatever the model asks for. A ``partitions`` of
    ``["all"]`` is a super admin's every partition; ``None`` is no partition
    at all (a web-only request). *user* is who asks, when the caller knows:
    what they may read is already the scope. *trace*, when the request is
    traced, is the retrieval trace the call's search records into: each call
    gets its own.
    """

    scope: Scope
    user: dict | None = None
    sources: SourceList = field(default_factory=SourceList)
    trace: RetrievalTraceBuilder | None = None

    def allows_partition(self, partition: str) -> bool:
        partitions = self.scope.partitions or []
        return partition in partitions or "all" in partitions

    def allows_file(self, partition: str, file_id: str) -> bool:
        """Whether the request may read *file_id*: its partition is in scope, and so is the file."""
        if not self.allows_partition(partition):
            return False
        return self.scope.file_ids is None or file_id in self.scope.file_ids


__all__ = ["SourceList", "ToolContext"]
