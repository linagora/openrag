"""MCPService — application logic for the Model Context Protocol server.

Reimplementation of PR 273's ``components/mcp`` (``SearchToolService`` +
``IndexationService``) on the hexagonal stack. Where the old code talked
straight to the Ray ``vectordb`` / ``indexer`` / ``task_state_manager``
actors, this service composes the Phase-8 orchestrators
(``RetrievalService``, ``PartitionService``, ``IndexingService``,
``JobService``, ``ConversionService``) plus the :class:`VectorStore` port,
so it stays Ray-free (``services → core`` only).

Per-request authorization (the OpenRAG partition ACL) is passed in
explicitly — ``user_id`` / ``is_admin`` / ``allowed_partitions`` are method
arguments, never read from ambient context. The ``api/mcp`` server layer
resolves them from the bearer token and forwards them; this keeps the
service a pure orchestrator that is trivially unit-testable.

``allowed_partitions == ["all"]`` is the admin wildcard (set by the server
when ``is_admin and SUPER_ADMIN_MODE``); ``None`` means the auth context
never ran and every method refuses.
"""

from __future__ import annotations

import asyncio
import difflib
import ipaddress
import json
import mimetypes
import socket
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

import httpx
from core.utils.conts import is_internal_metadata_key, strip_protected_metadata
from core.utils.exceptions import ValidationError
from core.utils.log_tail import collect_task_logs
from core.utils.logging import get_logger
from core.utils.partition_limits import max_partitions_for_user
from core.utils.url_safety import is_blocked_address, is_safe_url

if TYPE_CHECKING:
    from core.vector_stores import VectorStore
    from services.orchestrators.conversion_service import ConversionService
    from services.orchestrators.indexing_service import IndexingService
    from services.orchestrators.job_service import JobService
    from services.orchestrators.partition_service import PartitionService
    from services.orchestrators.retrieval_service import RetrievalService

logger = get_logger()

_ROLE_HIERARCHY: dict[str, int] = {"viewer": 1, "editor": 2, "owner": 3}
_FORBIDDEN_FILE_ID_CHARS: frozenset[str] = frozenset("/")
_ACTIVE_STATES = {"QUEUED", "SERIALIZING", "CHUNKING", "INSERTING"}
_MAX_REDIRECTS = 10
_MAX_CHUNKS_PER_CALL = 200
_MAX_FUZZY_CANDIDATES = 5000


def _strip_protected_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Drop server-managed keys from caller-supplied metadata (defense in depth).

    Thin wrapper over the shared guard in ``core.utils.conts`` — the key set now
    lives there so the REST and upload paths enforce the same one (#713).
    """
    md, removed = strip_protected_metadata(metadata)
    if removed:
        logger.warning("Dropped protected metadata keys from MCP write", keys=removed)
    return md


class MCPService:
    """Search + catalog + indexation operations exposed as MCP tools."""

    def __init__(
        self,
        *,
        retrieval_service: RetrievalService,
        partition_service: PartitionService,
        indexing_service: IndexingService,
        job_service: JobService,
        conversion_service: ConversionService,
        vector_store: VectorStore,
        collection: str,
        default_top_k: int = 5,
        max_top_k: int = 50,
        similarity_threshold: float = 0.8,
        download_timeout: float = 30.0,
        max_download_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        self._retrieval = retrieval_service
        self._partitions = partition_service
        self._indexing = indexing_service
        self._jobs = job_service
        self._conversion = conversion_service
        self._vector_store = vector_store
        self._collection = collection
        self.default_top_k = default_top_k
        self.max_top_k = max_top_k
        self.similarity_threshold = similarity_threshold
        self.download_timeout = download_timeout
        self.max_download_bytes = max_download_bytes

    # ------------------------------------------------------------------
    # Authorization helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_file_id(file_id: str) -> None:
        if not file_id or not file_id.strip():
            raise ValueError("file_id cannot be empty")
        bad = _FORBIDDEN_FILE_ID_CHARS & set(file_id)
        if bad:
            raise ValueError(f"file_id contains forbidden characters: {', '.join(sorted(bad))}")

    @staticmethod
    def _enforce_partition_scope(
        requested_partitions: list[str],
        allowed_partitions: list[str] | None,
    ) -> list[str]:
        """Resolve the search scope against the caller's allowed partitions."""
        if allowed_partitions is None:
            raise PermissionError("Authentication context is missing")
        if allowed_partitions == ["all"]:
            return ["all"] if requested_partitions == ["all"] else requested_partitions
        if requested_partitions == ["all"] and not allowed_partitions:
            raise PermissionError("No accessible partitions")
        if requested_partitions == ["all"]:
            return allowed_partitions
        denied = [p for p in requested_partitions if p not in allowed_partitions]
        if denied:
            raise PermissionError(f"Access denied for partition(s): {', '.join(sorted(set(denied)))}")
        return requested_partitions

    @staticmethod
    def _enforce_partition_access(partition: str, allowed_partitions: list[str] | None) -> None:
        if allowed_partitions is None:
            raise PermissionError("Authentication context is missing")
        if allowed_partitions == ["all"]:
            return
        if partition not in allowed_partitions:
            raise PermissionError(f"Access denied for partition: {partition}")

    async def _enforce_editor_access(
        self,
        partition: str,
        allowed_partitions: list[str] | None,
        user_id: int | None,
    ) -> None:
        """Require editor (or owner) role on *partition*."""
        self._enforce_partition_access(partition, allowed_partitions)
        if allowed_partitions == ["all"]:
            return  # admin wildcard
        if user_id is None:
            return  # no-auth mode — treated as admin
        members = await self._partitions.list_members(partition)
        membership = next((m for m in members if m.get("user_id") == user_id), None)
        if membership is None or _ROLE_HIERARCHY.get(membership.get("role", ""), 0) < _ROLE_HIERARCHY["editor"]:
            raise PermissionError(f"Editor role required for partition: {partition}")

    async def _ensure_partition_exists(
        self,
        partition: str,
        allowed_partitions: list[str] | None,
        user_id: int | None,
        *,
        is_admin: bool = False,
    ) -> list[str] | None:
        """Auto-create *partition* (owned by the caller) when it is missing.

        Returns the possibly-extended allowed list. The original list is
        never mutated — it may be shared across concurrent requests.
        """
        if allowed_partitions is None:
            return None  # _enforce_*_access will raise
        if allowed_partitions == ["all"]:
            return allowed_partitions
        if partition in allowed_partitions:
            return allowed_partitions
        if await self._partitions.partition_exists(partition):
            return allowed_partitions  # exists but no membership → access check raises
        cap_user = {"id": user_id, "is_admin": is_admin} if user_id is not None else {"id": 1, "is_admin": True}
        try:
            await self._partitions.create_partition(partition, user_id, max_owned=max_partitions_for_user(cap_user))
        except ValidationError as exc:
            if exc.code != "PARTITION_EXISTS":
                raise
        return [*allowed_partitions, partition]

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search_documents(
        self,
        *,
        query: str,
        partitions: list[str] | None,
        top_k: int | None,
        allowed_partitions: list[str] | None,
        file_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("Query cannot be empty")
        if file_id is not None:
            self._validate_file_id(file_id)

        requested = [p.strip() for p in (partitions or ["all"]) if p and p.strip()] or ["all"]
        scoped = self._enforce_partition_scope(requested, allowed_partitions)

        effective_top_k = top_k if top_k is not None else self.default_top_k
        if effective_top_k <= 0:
            raise ValueError("top_k must be greater than 0")
        effective_top_k = min(effective_top_k, self.max_top_k)

        # Inline the file_id as a literal Milvus expression rather than using
        # the templated ``filter_params`` form: the shared VectorStoreSearcher
        # forwards the raw ``filter`` string to Milvus but drops
        # ``filter_params``, so a ``{var}`` placeholder would never be bound.
        # ``json.dumps`` yields a correctly quoted/escaped string literal.
        search_filter = f"file_id == {json.dumps(file_id)}" if file_id is not None else None

        chunks = await self._retrieval.search(
            text=normalized_query,
            partitions=scoped,
            top_k=effective_top_k,
            similarity_threshold=self.similarity_threshold,
            filter=search_filter,
        )
        documents = [self._shape_chunk(c) for c in chunks]
        return {
            "query": normalized_query,
            "partitions": scoped,
            "top_k": effective_top_k,
            "count": len(documents),
            "documents": documents,
        }

    @staticmethod
    def _shape_chunk(chunk: Any) -> dict[str, Any]:
        """Domain ``Chunk`` → MCP search-result dict (legacy metadata shape)."""
        meta = chunk.to_langchain().metadata
        return {
            "chunk_id": meta.get("_id") or chunk.id,
            "content": chunk.text,
            "metadata": _public_chunk_metadata(meta, exclude=("_id",)),
        }

    # ------------------------------------------------------------------
    # Partitions & files
    # ------------------------------------------------------------------

    async def list_partitions(self, *, allowed_partitions: list[str] | None) -> dict[str, Any]:
        if allowed_partitions is None:
            raise PermissionError("Authentication context is missing")
        partitions = await self._partitions.list_partitions()
        if allowed_partitions != ["all"]:
            partitions = [p for p in partitions if p["partition"] in allowed_partitions]
        return {"count": len(partitions), "partitions": partitions}

    async def list_files(
        self,
        *,
        partition: str,
        allowed_partitions: list[str] | None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        self._enforce_partition_access(partition, allowed_partitions)
        files = await self._partitions.list_files(partition, limit=limit)
        return {"partition": partition, "count": len(files), "files": files}

    async def get_file_info(
        self,
        *,
        partition: str,
        file_id: str,
        allowed_partitions: list[str] | None,
    ) -> dict[str, Any]:
        self._enforce_partition_access(partition, allowed_partitions)
        if not await self._partitions.file_exists(file_id, partition):
            raise FileNotFoundError(f"File '{file_id}' not found in partition '{partition}'")
        # Read straight from the vector store (same source as get_file_chunks) so
        # chunk_count is exact — PartitionService.get_file_chunks caps at 2000,
        # which would silently undercount large files and disagree with the
        # total_chunks reported by get_file_chunks.
        rows = await self._vector_store.query_chunks_by_filter(
            self._collection,
            {"partition": partition, "file_id": file_id},
            output_fields=["*"],
        )
        metadata = _public_chunk_metadata(rows[0], exclude=("_id", "text", "vector")) if rows else {}
        return {
            "partition": partition,
            "file_id": file_id,
            "chunk_count": len(rows),
            "metadata": metadata,
        }

    async def get_file_chunks(
        self,
        *,
        partition: str,
        file_id: str,
        allowed_partitions: list[str] | None,
        offset: int = 0,
        limit: int = 3,
    ) -> dict[str, Any]:
        self._enforce_partition_access(partition, allowed_partitions)
        if offset < 0:
            raise ValueError("offset must be >= 0")
        if limit == 0 or limit < -1:
            raise ValueError("limit must be -1 (all) or a positive integer")
        # Cap the page size so a single call can never return an unbounded
        # number of (potentially large) chunks. -1 means "as many as allowed".
        page_size = _MAX_CHUNKS_PER_CALL if limit == -1 else min(limit, _MAX_CHUNKS_PER_CALL)
        if not await self._partitions.file_exists(file_id, partition):
            raise FileNotFoundError(f"File '{file_id}' not found in partition '{partition}'")
        rows = await self._vector_store.query_chunks_by_filter(
            self._collection,
            {"partition": partition, "file_id": file_id},
            output_fields=["*"],
        )
        # Milvus _id is INT64; guard against missing/non-int values so a
        # malformed row can't raise a TypeError mid-sort.
        rows.sort(key=lambda r: r["_id"] if isinstance(r.get("_id"), int) else 0)
        total = len(rows)
        page = rows[offset : offset + page_size]
        return {
            "partition": partition,
            "file_id": file_id,
            "total_chunks": total,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(page) < total,
            "chunks": [
                {
                    "chunk_id": row.get("_id"),
                    "content": row.get("text"),
                    "metadata": _public_chunk_metadata(row, exclude=("text", "_id", "vector")),
                }
                for row in page
            ],
        }

    async def fuzzy_search_files(
        self,
        *,
        query: str,
        allowed_partitions: list[str] | None,
        partition: str | None = None,
        cutoff: float = 0.4,
        limit: int = 20,
    ) -> dict[str, Any]:
        if allowed_partitions is None:
            raise PermissionError("Authentication context is missing")
        normalized_query = query.strip().lower()
        if not normalized_query:
            raise ValueError("Query cannot be empty")

        if partition is not None:
            self._enforce_partition_access(partition, allowed_partitions)
            search_partitions = [partition]
        elif allowed_partitions == ["all"]:
            search_partitions = [p["partition"] for p in await self._partitions.list_partitions()]
        else:
            search_partitions = allowed_partitions

        # Bound the candidate set so an admin (or a user with many partitions)
        # can't trigger an unbounded scan + difflib pass over the whole catalog.
        candidate_files: list[dict[str, Any]] = []
        for part in search_partitions:
            if len(candidate_files) >= _MAX_FUZZY_CANDIDATES:
                logger.warning("fuzzy_search_files candidate cap reached", cap=_MAX_FUZZY_CANDIDATES)
                break
            candidate_files.extend(await self._partitions.list_files(part))
        candidate_files = candidate_files[:_MAX_FUZZY_CANDIDATES]

        scored: list[tuple[float, dict[str, Any]]] = []
        for f in candidate_files:
            names = [str(f[k]).lower() for k in ("filename", "original_filename", "file_id") if f.get(k)]
            if not names:
                continue
            best = max(difflib.SequenceMatcher(None, normalized_query, name).ratio() for name in names)
            if best >= cutoff:
                scored.append((best, f))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = [{"score": round(ratio, 4), **f} for ratio, f in scored[:limit]]
        return {"query": query, "count": len(results), "files": results}

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    async def get_task_status(
        self,
        *,
        task_id: str,
        user_id: int | None,
        is_admin: bool,
    ) -> dict[str, Any]:
        details = await self._jobs.get_task_details(task_id)
        if details is None:
            raise KeyError(f"Task '{task_id}' not found")
        if not is_admin and user_id is not None and details.get("user_id") != user_id:
            raise PermissionError("You do not have permission to access this task")

        state = await self._indexing.get_task_state(task_id)
        result: dict[str, Any] = {"task_id": task_id, "task_state": state, "details": details}
        if state == "FAILED":
            if is_admin:
                result["error"] = await self._indexing.get_task_error(task_id)
            else:
                result["error"] = "Task failed. Contact an administrator for details."
        return result

    async def list_my_tasks(
        self,
        *,
        user_id: int | None,
        is_admin: bool,
        task_status: str | None = None,
    ) -> dict[str, Any]:
        tasks = await self._jobs.list_tasks(is_admin=is_admin, user_id=user_id, task_status=task_status)
        for task in tasks:
            if task.get("state") == "FAILED":
                if is_admin:
                    task["error"] = await self._indexing.get_task_error(task["task_id"])
                else:
                    task["error"] = "Task failed. Contact an administrator for details."
        return {"count": len(tasks), "tasks": tasks}

    async def get_task_logs(
        self,
        *,
        task_id: str,
        user_id: int | None,
        is_admin: bool,
        log_file: str | Path,
        max_lines: int = 100,
    ) -> dict[str, Any]:
        details = await self._jobs.get_task_details(task_id)
        if details is None:
            raise KeyError(f"Task '{task_id}' not found")
        if not is_admin and user_id is not None and details.get("user_id") != user_id:
            raise PermissionError("You do not have permission to access this task")

        log_path = Path(log_file)
        if not log_path.exists():
            raise FileNotFoundError(f"Log file not found: {log_path}")
        logs = collect_task_logs(log_path, task_id, max_lines)
        return {"task_id": task_id, "count": len(logs), "logs": logs}

    # ------------------------------------------------------------------
    # Chunk lookup
    # ------------------------------------------------------------------

    async def get_chunk_by_id(
        self,
        *,
        chunk_id: str,
        allowed_partitions: list[str] | None,
    ) -> dict[str, Any]:
        if allowed_partitions is None:
            raise PermissionError("Authentication context is missing")
        chunk = await self._conversion.get_chunk(chunk_id)
        if chunk is None:
            raise KeyError(f"Chunk '{chunk_id}' not found")
        chunk_partition = chunk["metadata"].get("partition")
        if allowed_partitions != ["all"] and chunk_partition not in allowed_partitions:
            raise PermissionError(f"Access denied for partition: {chunk_partition}")
        return {
            "chunk_id": chunk_id,
            "page_content": chunk["page_content"],
            "metadata": chunk["metadata"],
        }

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def delete_file(
        self,
        *,
        partition: str,
        file_id: str,
        allowed_partitions: list[str] | None,
        user_id: int | None,
    ) -> dict[str, Any]:
        self._validate_file_id(file_id)
        await self._enforce_editor_access(partition, allowed_partitions, user_id)
        if not await self._partitions.file_exists(file_id, partition):
            raise FileNotFoundError(f"File '{file_id}' not found in partition '{partition}'")
        await self._indexing.delete_file(file_id, partition)
        return {
            "partition": partition,
            "file_id": file_id,
            "message": f"File '{file_id}' deleted from partition '{partition}'.",
        }

    async def update_file_metadata(
        self,
        *,
        partition: str,
        file_id: str,
        metadata: dict[str, Any],
        allowed_partitions: list[str] | None,
        user_id: int | None,
    ) -> dict[str, Any]:
        self._validate_file_id(file_id)
        await self._enforce_editor_access(partition, allowed_partitions, user_id)
        # ``partition`` in the payload is an authorized move control (not a
        # protected key) — require editor on the destination too.
        if metadata and "partition" in metadata:
            await self._enforce_editor_access(metadata["partition"], allowed_partitions, user_id)
        if not await self._partitions.file_exists(file_id, partition):
            raise FileNotFoundError(f"File '{file_id}' not found in partition '{partition}'")
        await self._indexing.update_metadata(
            file_id,
            _strip_protected_metadata(metadata),
            partition,
            {"id": user_id} if user_id is not None else None,
        )
        return {
            "partition": partition,
            "file_id": file_id,
            "message": f"Metadata for file '{file_id}' successfully updated.",
        }

    async def copy_file(
        self,
        *,
        source_partition: str,
        source_file_id: str,
        dest_partition: str,
        dest_file_id: str,
        allowed_partitions: list[str] | None,
        user_id: int | None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._validate_file_id(source_file_id)
        self._validate_file_id(dest_file_id)
        self._enforce_partition_access(source_partition, allowed_partitions)
        await self._enforce_editor_access(dest_partition, allowed_partitions, user_id)

        if not await self._partitions.file_exists(source_file_id, source_partition):
            raise FileNotFoundError(f"File '{source_file_id}' not found in partition '{source_partition}'")
        if await self._partitions.file_exists(dest_file_id, dest_partition):
            raise FileExistsError(f"File '{dest_file_id}' already exists in partition '{dest_partition}'")

        await self._indexing.copy_file(
            source_file_id=source_file_id,
            source_partition=source_partition,
            target_file_id=dest_file_id,
            target_partition=dest_partition,
            metadata=_strip_protected_metadata(extra_metadata),
            user={"id": user_id} if user_id is not None else None,
        )
        return {
            "source_partition": source_partition,
            "source_file_id": source_file_id,
            "dest_partition": dest_partition,
            "dest_file_id": dest_file_id,
            "message": "File copied successfully.",
        }

    async def index_url(
        self,
        *,
        url: str,
        partition: str,
        file_id: str,
        allowed_partitions: list[str] | None,
        user_id: int | None,
        is_admin: bool = False,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Validate cheap/safe inputs BEFORE any side effect (partition
        # auto-create), so a blocked URL or bad id never leaves an empty
        # partition behind.
        self._validate_file_id(file_id)
        # SSRF guard: reject non-http(s) and any host resolving to a
        # loopback/private/link-local/reserved range (cloud metadata, internal
        # services). Redirect hops are re-validated inside _safe_download.
        if not is_safe_url(url):
            raise ValueError(f"URL is not allowed for server-side fetch: {url!r}")

        allowed_partitions = await self._ensure_partition_exists(
            partition,
            allowed_partitions,
            user_id,
            is_admin=is_admin,
        )
        await self._enforce_editor_access(partition, allowed_partitions, user_id)

        if await self._partitions.file_exists(file_id, partition):
            raise FileExistsError(f"File '{file_id}' already exists in partition '{partition}'")

        filename = Path(urlparse(url).path.rstrip("/")).name or file_id
        suffix = Path(filename).suffix or ""
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = Path(tmp.name)
        try:
            await self._safe_download(url, tmp_path)
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(f"Failed to download '{url}': {exc}") from exc

        metadata = _strip_protected_metadata(extra_metadata)
        metadata["source_url"] = url
        guessed_mime, _ = mimetypes.guess_type(filename)
        if guessed_mime and "mimetype" not in metadata:
            metadata["mimetype"] = guessed_mime

        task_id = await self._indexing.add_file(
            file_path=str(tmp_path),
            file_id=file_id,
            partition=partition,
            metadata=metadata,
            sanitized_filename=filename,
            original_filename=filename,
            user={"id": user_id, "is_admin": is_admin} if user_id is not None else None,
        )
        return {
            "partition": partition,
            "file_id": file_id,
            "task_id": task_id,
            "message": f"Indexation started. Poll get_indexation_task_status with task_id='{task_id}'.",
        }

    @staticmethod
    async def _assert_host_resolves_safely(host: str) -> None:
        """Reject *host* if it resolves to any private/loopback/reserved address.

        ``is_safe_url`` only inspects the literal host, so a public hostname
        pointing at an internal IP (DNS-based SSRF, incl. cloud metadata at
        169.254.169.254) would otherwise pass. Resolution runs on the event
        loop's async resolver so the loop is never blocked. This narrows but
        cannot fully close TOCTOU DNS rebinding — the literal check and the
        per-hop re-validation in ``_safe_download`` remain the other layers.
        """
        if not host:
            raise ValueError("URL has no host")
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError(f"could not resolve host {host!r}: {exc}") from exc
        for info in infos:
            addr = ipaddress.ip_address(info[4][0])
            if is_blocked_address(addr):
                raise ValueError(f"host {host!r} resolves to a disallowed address: {addr}")

    async def _safe_download(self, url: str, dest: Path) -> None:
        """Stream *url* to *dest* with SSRF, redirect, size and time guards.

        Redirects are followed manually (``follow_redirects=False``) so every
        hop is re-validated by :func:`is_safe_url` — a public host can otherwise
        redirect to a private address after the initial check. The body is
        streamed with a hard byte ceiling so a hostile endpoint cannot exhaust
        disk, and the whole fetch is bounded by ``download_timeout``.
        """
        current_url = url
        async with httpx.AsyncClient(timeout=self.download_timeout, follow_redirects=False) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                if not is_safe_url(current_url):
                    raise ValueError(f"redirect to a disallowed URL: {current_url!r}")
                await self._assert_host_resolves_safely(urlparse(current_url).hostname or "")
                async with client.stream("GET", current_url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location", "")
                        if not location:
                            raise ValueError("redirect without a Location header")
                        current_url = urljoin(current_url, location)
                        continue
                    response.raise_for_status()
                    written = 0
                    with dest.open("wb") as fh:
                        async for chunk in response.aiter_bytes():
                            written += len(chunk)
                            if written > self.max_download_bytes:
                                raise ValueError(f"download exceeds {self.max_download_bytes} bytes")
                            fh.write(chunk)
                    return
        raise ValueError("too many redirects")


__all__ = ["MCPService"]


def _public_chunk_metadata(row: dict[str, Any], *, exclude: tuple[str, ...]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in exclude and not is_internal_metadata_key(key)}
