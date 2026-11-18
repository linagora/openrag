"""The ``scope`` step: the partitions a request searches, narrowed by a workspace or attachments."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from core.chat.scope import Scope, within_workspace
from core.utils.exceptions import WorkspaceNotFoundError

if TYPE_CHECKING:
    from services.orchestrators.workspace_service import WorkspaceService


class ScopeResolver:
    """Resolves ``metadata.workspace`` and ``metadata.attachments`` against the request's partitions."""

    def __init__(self, workspace_service: WorkspaceService) -> None:
        self._workspaces = workspace_service

    async def resolve(self, partitions: list[str] | None, *, workspace: str | None, attachment_ids: list[str]) -> Scope:
        """The scope searched; raises ``WorkspaceNotFoundError`` for a workspace the caller can't search."""
        if workspace and partitions:
            resolved = await self._workspaces.resolve_scope(workspace, partitions)
            if resolved is None:
                raise WorkspaceNotFoundError(f"Workspace '{workspace}' not found.")
            # A workspace belongs to exactly one partition: narrow retrieval to
            # it even for an "openrag-all" / multi-partition request, otherwise
            # file_id-only filtering could match a same-named file in another
            # partition the caller also has access to.
            if attachment_ids:
                # Attachments narrow the workspace, never widen it: only the
                # attached files that belong to it are searched.
                searched = within_workspace(attachment_ids, resolved.file_ids)
                return Scope([resolved.partition], searched, "attachment", searched)
            return Scope([resolved.partition], resolved.file_ids, "workspace")
        if attachment_ids and partitions:
            # No ownership check needed: file_id is ANDed with the server-fixed
            # partition (or, for the "all" wildcard, SUPER_ADMIN_MODE-only).
            searched = await self._existing_file_ids(attachment_ids, partitions)
            return Scope(partitions, searched, "attachment", searched)
        return Scope(partitions)

    async def _existing_file_ids(self, file_ids: list[str], partitions: list[str]) -> list[str]:
        """Order-preserving, deduplicated subset of ``file_ids`` indexed in ``partitions``.

        ``"all"`` (``SUPER_ADMIN_MODE`` wildcard) takes an unscoped lookup instead
        of a per-partition one.
        """
        if "all" in partitions:
            if len(partitions) > 1:
                raise ValueError("`partitions` cannot mix the wildcard with explicit values.")
            found = set(await self._workspaces.get_existing_file_ids_any_partition(file_ids))
        else:
            results = await asyncio.gather(*(self._workspaces.get_existing_file_ids(p, file_ids) for p in partitions))
            found = {fid for r in results for fid in r}
        return [fid for fid in dict.fromkeys(file_ids) if fid in found]
