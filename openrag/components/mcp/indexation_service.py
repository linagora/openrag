"""
MCP service for indexation-related operations:
- Listing partitions and files
- Retrieving file chunks
- Fuzzy search on file names
- Task status queries
- File write operations (delete, update metadata, copy)
- Chunk retrieval by ID
- Task listing and log retrieval
- URL-based file indexation
"""

from __future__ import annotations

import difflib
import json
import mimetypes
import tempfile
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from utils.dependencies import get_indexer, get_task_state_manager, get_vectordb


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
        offset: int = 0,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Return a page of text chunks for a specific file.

        Args:
            offset: Zero-based index of the first chunk to return.
            limit: Maximum number of chunks to return per call.
        """
        self._enforce_partition_access(partition, allowed_partitions)

        vectordb = get_vectordb()

        if not await vectordb.file_exists.remote(file_id, partition):
            raise FileNotFoundError(f"File '{file_id}' not found in partition '{partition}'")

        all_chunks = await vectordb.get_file_chunks.remote(partition=partition, file_id=file_id, include_id=True)
        total = len(all_chunks)
        page = all_chunks[offset : offset + limit]

        return {
            "partition": partition,
            "file_id": file_id,
            "total_chunks": total,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(page) < total,
            "chunks": [
                {
                    "chunk_id": chunk.metadata.get("_id"),
                    "content": chunk.page_content,
                    "metadata": {k: v for k, v in chunk.metadata.items() if k != "_id"},
                }
                for chunk in page
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

            best_ratio = max(difflib.SequenceMatcher(None, normalized_query, name).ratio() for name in names)
            if best_ratio >= cutoff:
                scored.append((best_ratio, f))

        # Sort by descending similarity, then take top `limit`
        scored.sort(key=lambda x: x[0], reverse=True)
        results = [{"score": round(ratio, 4), **file_dict} for ratio, file_dict in scored[:limit]]

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

    # ------------------------------------------------------------------
    # Task listing
    # ------------------------------------------------------------------

    async def list_my_tasks(
        self,
        user_id: int | None,
        task_status: str | None = None,
    ) -> dict[str, Any]:
        """
        Return all indexation tasks belonging to the current user.

        Args:
            user_id: The authenticated user's ID.  ``None`` means no-auth mode
                (returns all tasks, behaving like an admin).
            task_status: Optional filter.  Recognised values:
                - ``"active"``    – QUEUED / SERIALIZING / CHUNKING / INSERTING
                - ``"completed"`` – COMPLETED tasks only
                - ``"failed"``    – FAILED tasks only
                - Any exact state name (case-insensitive)
                - ``None``        – all tasks

        Returns:
            ``{ count, tasks: [{ task_id, state, details, error? }] }``
        """
        task_state_manager = get_task_state_manager()

        active_states = {"QUEUED", "SERIALIZING", "CHUNKING", "INSERTING"}

        if user_id is None:
            all_info: dict[str, dict] = await task_state_manager.get_all_info.remote()
        else:
            all_info = await task_state_manager.get_all_user_info.remote(user_id)

        if task_status is None:
            filtered = list(all_info.items())
        elif task_status.lower() == "active":
            filtered = [(tid, info) for tid, info in all_info.items() if info["state"] in active_states]
        else:
            filtered = [(tid, info) for tid, info in all_info.items() if info["state"].lower() == task_status.lower()]

        tasks = []
        for task_id, info in filtered:
            item: dict[str, Any] = {
                "task_id": task_id,
                "state": info["state"],
                "details": info.get("details"),
            }
            if info["state"] == "FAILED" and info.get("error"):
                item["error"] = info["error"]
            tasks.append(item)

        return {"count": len(tasks), "tasks": tasks}

    # ------------------------------------------------------------------
    # Task logs
    # ------------------------------------------------------------------

    async def get_task_logs(
        self,
        task_id: str,
        user_id: int | None,
        log_file: str | Path,
        max_lines: int = 100,
    ) -> dict[str, Any]:
        """
        Return chronological log lines for an indexation task.

        Ownership is enforced the same way as ``get_task_status``: only the
        task owner (or no-auth mode) may read the logs.

        Args:
            task_id: The task identifier.
            user_id: Authenticated user's ID; ``None`` skips ownership check.
            log_file: Path to the JSON-per-line application log file.
            max_lines: Maximum number of log lines to return (default 100).

        Returns:
            ``{ task_id, count, logs: ["<timestamp> - LEVEL - message - extra", …] }``
        """
        task_state_manager = get_task_state_manager()

        # Ownership check (mirrors get_task_status)
        details = await task_state_manager.get_details.remote(task_id)
        if details is None:
            raise KeyError(f"Task '{task_id}' not found")
        if user_id is not None and details.get("user_id") != user_id:
            raise PermissionError("You do not have permission to access this task")

        log_path = Path(log_file)
        if not log_path.exists():
            raise FileNotFoundError(f"Log file not found: {log_path}")

        logs: list[str] = []
        with open(log_path, errors="replace") as fh:
            for line in reversed(list(fh)):
                try:
                    record = json.loads(line).get("record", {})
                    if record.get("extra", {}).get("task_id") == task_id:
                        logs.append(
                            f"{record['time']['repr']} - {record['level']['name']} - "
                            f"{record['message']} - {record['extra']}"
                        )
                        if len(logs) >= max_lines:
                            break
                except (json.JSONDecodeError, KeyError):
                    continue

        logs = logs[::-1]  # restore chronological order
        return {"task_id": task_id, "count": len(logs), "logs": logs}

    # ------------------------------------------------------------------
    # Chunk retrieval by ID
    # ------------------------------------------------------------------

    async def get_chunk_by_id(
        self,
        chunk_id: str,
        allowed_partitions: list[str] | None,
    ) -> dict[str, Any]:
        """
        Fetch a single indexed chunk by its vector-DB ID.

        The chunk's partition must be in ``allowed_partitions``
        (or ``allowed_partitions == ["all"]`` for admins).

        Args:
            chunk_id: The unique chunk/extract identifier.
            allowed_partitions: Partitions the current user may access.

        Returns:
            ``{ chunk_id, page_content, metadata }``
        """
        if allowed_partitions is None:
            raise PermissionError("Authentication context is missing")

        vectordb = get_vectordb()
        chunk = await vectordb.get_chunk_by_id.remote(chunk_id)
        if chunk is None:
            raise KeyError(f"Chunk '{chunk_id}' not found")

        chunk_partition = chunk.metadata.get("partition")
        if allowed_partitions != ["all"] and chunk_partition not in allowed_partitions:
            raise PermissionError(f"Access denied for partition: {chunk_partition}")

        return {
            "chunk_id": chunk_id,
            "page_content": chunk.page_content,
            "metadata": chunk.metadata,
        }

    # ------------------------------------------------------------------
    # File write operations
    # ------------------------------------------------------------------

    async def delete_file(
        self,
        partition: str,
        file_id: str,
        allowed_partitions: list[str] | None,
    ) -> dict[str, Any]:
        """
        Delete a file and all its chunks from a partition.

        Requires editor access to the partition.

        Returns:
            ``{ partition, file_id, message }``
        """
        self._enforce_partition_access(partition, allowed_partitions)

        vectordb = get_vectordb()
        indexer = get_indexer()

        if not await vectordb.file_exists.remote(file_id, partition):
            raise FileNotFoundError(f"File '{file_id}' not found in partition '{partition}'")

        await indexer.delete_file.remote(file_id, partition)
        return {
            "partition": partition,
            "file_id": file_id,
            "message": f"File '{file_id}' deleted from partition '{partition}'.",
        }

    async def update_file_metadata(
        self,
        partition: str,
        file_id: str,
        metadata: dict[str, Any],
        allowed_partitions: list[str] | None,
    ) -> dict[str, Any]:
        """
        Update metadata fields for an existing file without re-uploading it.

        If ``metadata`` contains a ``"partition"`` key the file will be moved to
        that partition — the caller must also have editor access to the destination.

        Returns:
            ``{ partition, file_id, message }``
        """
        self._enforce_partition_access(partition, allowed_partitions)

        # If moving to another partition, check access to the destination too
        if "partition" in metadata:
            self._enforce_partition_access(metadata["partition"], allowed_partitions)

        vectordb = get_vectordb()
        if not await vectordb.file_exists.remote(file_id, partition):
            raise FileNotFoundError(f"File '{file_id}' not found in partition '{partition}'")

        indexer = get_indexer()
        metadata["file_id"] = file_id
        await indexer.update_file_metadata.remote(file_id, metadata, partition)
        return {
            "partition": partition,
            "file_id": file_id,
            "message": f"Metadata for file '{file_id}' successfully updated.",
        }

    async def copy_file(
        self,
        source_partition: str,
        source_file_id: str,
        dest_partition: str,
        dest_file_id: str,
        allowed_partitions: list[str] | None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Copy a file from one partition to another.

        The caller needs read access to the source partition and write access
        to the destination partition.

        Returns:
            ``{ source_partition, source_file_id, dest_partition, dest_file_id, message }``
        """
        self._enforce_partition_access(source_partition, allowed_partitions)
        self._enforce_partition_access(dest_partition, allowed_partitions)

        vectordb = get_vectordb()
        if not await vectordb.file_exists.remote(source_file_id, source_partition):
            raise FileNotFoundError(f"File '{source_file_id}' not found in partition '{source_partition}'")

        metadata: dict[str, Any] = extra_metadata.copy() if extra_metadata else {}
        metadata["file_id"] = dest_file_id
        metadata["partition"] = dest_partition

        indexer = get_indexer()
        await indexer.copy_file.remote(
            file_id=source_file_id,
            metadata=metadata,
            partition=source_partition,
        )
        return {
            "source_partition": source_partition,
            "source_file_id": source_file_id,
            "dest_partition": dest_partition,
            "dest_file_id": dest_file_id,
            "message": "File copied successfully.",
        }

    # ------------------------------------------------------------------
    # URL-based indexation
    # ------------------------------------------------------------------

    async def index_url(
        self,
        url: str,
        partition: str,
        file_id: str,
        allowed_partitions: list[str] | None,
        extra_metadata: dict[str, Any] | None = None,
        task_state_manager_ref=None,
    ) -> dict[str, Any]:
        """
        Fetch a document from a URL and index it into a partition.

        The file is downloaded to a temporary path, then handed to the Indexer
        actor exactly as the REST ``POST /indexer/partition/{partition}/file/{file_id}``
        endpoint does.

        Args:
            url: Public HTTP/HTTPS URL of the document to fetch.
            partition: Target partition.
            file_id: Desired file identifier (must not already exist).
            allowed_partitions: Partitions the current user may access.
            extra_metadata: Optional dict of additional metadata fields.
            task_state_manager_ref: Injected for testing; uses the real actor when None.

        Returns:
            ``{ partition, file_id, task_id, message }``
        """
        self._enforce_partition_access(partition, allowed_partitions)

        # Validate URL scheme
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Only http/https URLs are supported, got: {parsed.scheme!r}")

        vectordb = get_vectordb()
        if await vectordb.file_exists.remote(file_id, partition):
            raise FileExistsError(f"File '{file_id}' already exists in partition '{partition}'")

        # Derive filename from URL path; fall back to file_id
        url_path = parsed.path.rstrip("/")
        filename = Path(url_path).name or file_id

        # Download to a temp file
        suffix = Path(filename).suffix or ""
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = Path(tmp.name)

        try:
            urllib.request.urlretrieve(url, tmp_path)  # noqa: S310
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(f"Failed to download '{url}': {exc}") from exc

        # Detect mimetype
        guessed_mime, _ = mimetypes.guess_type(filename)

        metadata: dict[str, Any] = extra_metadata.copy() if extra_metadata else {}
        metadata.update(
            {
                "source": str(tmp_path),
                "filename": filename,
                "original_filename": filename,
                "file_id": file_id,
                "source_url": url,
            }
        )
        if guessed_mime and "mimetype" not in metadata:
            metadata["mimetype"] = guessed_mime

        indexer = get_indexer()
        tsm = task_state_manager_ref if task_state_manager_ref is not None else get_task_state_manager()

        task = indexer.add_file.remote(path=tmp_path, metadata=metadata, partition=partition)
        task_id: str = task.task_id().hex()
        await tsm.set_state.remote(task_id, "QUEUED")
        await tsm.set_object_ref.remote(task_id, {"ref": task})

        return {
            "partition": partition,
            "file_id": file_id,
            "task_id": task_id,
            "message": f"Indexation started. Poll get_indexation_task_status with task_id='{task_id}'.",
        }
