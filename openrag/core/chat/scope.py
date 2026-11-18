"""What a chat request searches: its partitions, narrowed by a workspace or attachments."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from core.retrieval.trace import TRACE_FILE_SCOPE_KIND_KEY

ScopeKind = Literal["workspace", "attachment"]


@dataclass(frozen=True)
class Scope:
    """The partitions a request searches, and the files it is limited to.

    ``file_ids`` is ``None`` when the whole partitions are searched. An empty
    list is a scope too (a workspace with no files, attachments that aren't
    indexed): it searches nothing, never the whole partition.
    ``attachment_ids`` are the attachments actually searched, the ones
    reported back in ``extra.attachments``.
    """

    partitions: list[str] | None
    file_ids: list[str] | None = None
    kind: ScopeKind | None = None
    attachment_ids: list[str] = field(default_factory=list)

    def filter_params(self, *, traced: bool) -> dict | None:
        """The retrieval filter, with the scope's kind for the trace when one is recorded."""
        if self.file_ids is None:
            return None
        params: dict = {"file_id": self.file_ids}
        if traced:
            params[TRACE_FILE_SCOPE_KIND_KEY] = self.kind
        return params


def within_workspace(attachment_ids: list[str], workspace_file_ids: list[str]) -> list[str]:
    """Order-preserving, deduplicated subset of ``attachment_ids`` allowed by the workspace."""
    allowed = set(workspace_file_ids)
    return list(dict.fromkeys(fid for fid in attachment_ids if fid in allowed))
