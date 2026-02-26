"""
MCP service for indexation-related operations:
- Listing partitions and files
- Retrieving file chunks
- Fuzzy search on file names
- Task status queries
"""
from __future__ import annotations

import difflib
from typing import Any

from utils.dependencies import get_task_state_manager, get_vectordb


class IndexationService:
    """Handles indexation / catalog operations for the MCP server."""

    def _enforce_partition_access(
        self,
        partition: str,
        allowed_partitions: list[str] | None,
    ) -> None:
        """Raise PermissionError if the user is not allowed to access *partition*."""
        if allowed_partitions is None:
            raise PermissionError("Authentication context is missing")
        if allowed_partitions == ["all"]:
            return
        if partition not in allowed_partitions:
            raise PermissionError(f"Access denied for partition: {partition}")

    # ------------------------------------------------------------------
    # Partitions
    # ------------------------------------------------------------------

    async def list_partitions(self, allowed_partitions: list[str] | None) -> dict[str, Any]:
        """Return the list of partitions accessible to the current user."""
        if allowed_partitions is None:
            raise PermissionError("Authentication context is missing")

        vectordb = get_vectordb()

        if allowed_partitions == ["all"]:
            partitions = await vectordb.list_partitions.remote()
        else:
            # list_partitions returns all; filter to what the user can see
            all_partitions = await vectordb.list_partitions.remote()
            partitions = [p for p in all_partitions if p["partition"] in allowed_partitions]

        return {"count": len(partitions), "partitions": partitions}

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------

    async def list_files(
        self,
        partition: str,
        allowed_partitions: list[str] | None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Return the list of files in a partition."""
        self._enforce_partition_access(partition, allowed_partitions)

        vectordb = get_vectordb()
        result = await vectordb.list_partition_files.remote(partition=partition, limit=limit)
        files = result.get("files", [])
        return {"partition": partition, "count": len(files), "files": files}

    async def get_file_info(
        self,
        partition: str,
        file_id: str,
        allowed_partitions: list[str] | None,
    ) -> dict[str, Any]:
        """Return metadata and chunk count for a specific file."""
        self._enforce_partition_access(partition, allowed_partitions)

        vectordb = get_vectordb()

        if not await vectordb.file_exists.remote(file_id, partition):
            raise FileNotFoundError(f"File '{file_id}' not found in partition '{partition}'")

        chunks = await vectordb.get_file_chunks.remote(partition=partition, file_id=file_id, include_id=False)
        metadata = chunks[0].metadata if chunks else {}

        return {
            "partition": partition,
            "file_id": file_id,
            "chunk_count": len(chunks),
            "metadata": metadata,
        }

    async def get_file_chunks(
        self,
        partition: str,
        file_id: str,
        allowed_partitions: list[str] | None,
    ) -> dict[str, Any]:
        """Return the full text content of every chunk for a specific file."""
        self._enforce_partition_access(partition, allowed_partitions)

        vectordb = get_vectordb()

        if not await vectordb.file_exists.remote(file_id, partition):
            raise FileNotFoundError(f"File '{file_id}' not found in partition '{partition}'")

        chunks = await vectordb.get_file_chunks.remote(partition=partition, file_id=file_id, include_id=True)

        return {
            "partition": partition,
            "file_id": file_id,
            "chunk_count": len(chunks),
            "chunks": [
                {
                    "chunk_id": chunk.metadata.get("_id"),
                    "content": chunk.page_content,
                    "metadata": {k: v for k, v in chunk.metadata.items() if k != "_id"},
                }
                for chunk in chunks
            ],
        }

    # ------------------------------------------------------------------
    # Fuzzy file name search
    # ------------------------------------------------------------------

    async def fuzzy_search_files(
        self,
        query: str,
        allowed_partitions: list[str] | None,
        partition: str | None = None,
        cutoff: float = 0.4,
        limit: int = 20,
    ) -> dict[str, Any]:
        """
        Fuzzy search across file names (``filename`` / ``original_filename`` /
        ``file_id`` fields in file metadata) using difflib sequence matching.

        Args:
            query: The search string to match against file names.
            allowed_partitions: Partitions the current user may access.
            partition: Optional – restrict the search to a single partition.
            cutoff: Minimum similarity ratio (0-1). Lower = more permissive.
            limit: Maximum number of results to return.

        Returns:
            A dict with ``count`` and ``files`` sorted by descending similarity.
        """
        if allowed_partitions is None:
            raise PermissionError("Authentication context is missing")

        normalized_query = query.strip().lower()
        if not normalized_query:
            raise ValueError("Query cannot be empty")

        vectordb = get_vectordb()

        # Determine which partitions to search
        if partition is not None:
            self._enforce_partition_access(partition, allowed_partitions)
            search_partitions = [partition]
        elif allowed_partitions == ["all"]:
            all_partitions = await vectordb.list_partitions.remote()
            search_partitions = [p["partition"] for p in all_partitions]
        else:
            search_partitions = allowed_partitions

        # Collect all files from the relevant partitions
        candidate_files: list[dict[str, Any]] = []
        for part in search_partitions:
            result = await vectordb.list_partition_files.remote(partition=part, limit=None)
            for f in result.get("files", []):
                candidate_files.append(f)

        if not candidate_files:
            return {"query": query, "count": 0, "files": []}

        # Build (ratio, file_dict) pairs using difflib
        scored: list[tuple[float, dict[str, Any]]] = []
        for f in candidate_files:
            # Build a set of searchable name candidates from the file record
            names: list[str] = []
            for key in ("filename", "original_filename", "file_id"):
                val = f.get(key)
                if val:
                    names.append(str(val).lower())

            if not names:
                continue

            best_ratio = max(
                difflib.SequenceMatcher(None, normalized_query, name).ratio()
                for name in names
            )
            if best_ratio >= cutoff:
                scored.append((best_ratio, f))

        # Sort by descending similarity, then take top `limit`
        scored.sort(key=lambda x: x[0], reverse=True)
        results = [
            {"score": round(ratio, 4), **file_dict}
            for ratio, file_dict in scored[:limit]
        ]

        return {"query": query, "count": len(results), "files": results}

    # ------------------------------------------------------------------
    # Task status
    # ------------------------------------------------------------------

    async def get_task_status(self, task_id: str, user_id: int | None) -> dict[str, Any]:
        """
        Return the status of an indexation task.

        Only the user who created the task (or an admin) may query it.
        ``user_id=None`` is treated as admin (no-auth mode).
        """
        task_state_manager = get_task_state_manager()

        details = await task_state_manager.get_details.remote(task_id)
        if details is None:
            raise KeyError(f"Task '{task_id}' not found")

        # Ownership check (skip when AUTH_TOKEN is not configured, i.e. user_id is None)
        if user_id is not None and details.get("user_id") != user_id:
            raise PermissionError("You do not have permission to access this task")

        state = await task_state_manager.get_state.remote(task_id)

        result: dict[str, Any] = {
            "task_id": task_id,
            "task_state": state,
            "details": details,
        }

        if state == "FAILED":
            error = await task_state_manager.get_error.remote(task_id)
            result["error"] = error

        return result
