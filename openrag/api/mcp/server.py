"""Standalone MCP (Model Context Protocol) server.

Reimplementation of PR 273's ``openrag/mcp_server.py`` on the hexagonal
stack. A FastMCP streamable-HTTP app exposes OpenRAG's search / catalog /
indexation operations as MCP tools. All tool logic lives in
``services.orchestrators.mcp_service.MCPService``; this module is the thin
``api`` entrypoint that:

* owns a :class:`~di.container.ServiceContainer` for the server process
  (built and initialised in the ASGI lifespan, mirroring ``api/main.py``);
* resolves the caller from the bearer token in
  :class:`MCPAuthContextMiddleware` and stashes the principal in
  :mod:`api.mcp.auth_context`;
* wires one ``@server.tool`` per operation, each reading the auth context
  and forwarding it explicitly to ``MCPService``.

Per the layer guard, ``api`` reaches ``services`` only through ``di`` — the
``MCPService`` instance is pulled off the container (``container.mcp_service``)
and never imported here.

Run it with ``uv run -m api.mcp.server`` (serves at ``http://host:port<path>``,
defaults ``0.0.0.0:8081/mcp``; configure via the ``OPENRAG_MCP_*`` env vars).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import ray
from api.mcp.auth_context import (
    get_allowed_partitions,
    get_user_id,
    is_admin,
    reset_auth_context,
    set_auth_context,
)
from config import load_config
from core.utils.log_tail import app_log_file
from core.utils.logging import get_logger
from di.container import ServiceContainer
from di.workers import ensure_worker_bootstrap
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = get_logger()

config = load_config()
mcp_config = config.mcp

server = FastMCP(mcp_config.server_name, stateless_http=True, json_response=True)

LOG_FILE = app_log_file(config.paths.log_dir)


# ---------------------------------------------------------------------------
# Process-level container (built in the ASGI lifespan, on the serving loop)
# ---------------------------------------------------------------------------

_container: ServiceContainer | None = None


def _require_container() -> ServiceContainer:
    if _container is None:
        raise RuntimeError("MCP service container is not initialized.")
    return _container


def _service():
    """The :class:`MCPService` for the current process (typed loosely so the
    ``api`` layer does not import from ``services``)."""
    return _require_container().mcp_service


async def _startup() -> None:
    global _container
    if not ray.is_initialized():
        ray.init(dashboard_host="0.0.0.0", ignore_reinit_error=True)
    ensure_worker_bootstrap()
    container = ServiceContainer(config)
    await container.initialize()
    _container = container
    logger.info("MCP server container initialized")


async def _shutdown() -> None:
    global _container
    if _container is not None:
        try:
            await _container.shutdown()
        finally:
            _container = None


# ---------------------------------------------------------------------------
# Auth middleware — resolves the caller and stashes the principal
# ---------------------------------------------------------------------------


class MCPAuthContextMiddleware(BaseHTTPMiddleware):
    """Resolve the bearer-token principal and set the MCP auth context.

    Mirrors the token-mode contract of ``api.middleware.AuthMiddleware``:
    when ``AUTH_TOKEN`` is unset the server runs in dev mode and every call
    acts as admin user 1; otherwise a valid ``Authorization: Bearer`` is
    required. Allowed partitions follow ``current_user_or_admin_partitions_list``
    (``["all"]`` for admins under ``SUPER_ADMIN_MODE``).
    """

    async def _resolve_principal(self, request: Request) -> tuple[int | None, bool, list[str] | None]:
        auth_service = _require_container().auth_service
        auth_mode = os.getenv("AUTH_MODE", "token").strip().lower()
        auth_token = os.getenv("AUTH_TOKEN")

        # Dev bypass (act as admin user 1) is allowed ONLY in token mode with no
        # configured admin token — identical to api.middleware.AuthMiddleware.
        # In OIDC mode AUTH_TOKEN is commonly unset; without this gate the MCP
        # endpoint would silently treat every caller as admin (auth bypass), so
        # a valid bearer (users.token) is always required there.
        if auth_mode == "token" and auth_token is None:
            user = await auth_service.get_user_for_request(1)
        else:
            header = request.headers.get("authorization", "")
            token = header.split(" ", 1)[1] if header.lower().startswith("bearer ") else None
            if not token:
                raise PermissionError("Missing token")
            user = await auth_service.get_user_by_token_for_request(token)
            if not user:
                raise PermissionError("Invalid token")
        if not user:
            raise PermissionError("Unauthenticated")

        super_admin_mode = os.getenv("SUPER_ADMIN_MODE", "false").lower() == "true"
        admin = bool(user.get("is_admin"))
        if admin and super_admin_mode:
            allowed = ["all"]
        else:
            user_partitions = await auth_service.list_user_partitions_for_request(user["id"])
            allowed = [p["partition"] for p in user_partitions]
        return user.get("id"), admin, allowed

    async def dispatch(self, request: Request, call_next):
        try:
            user_id, admin, allowed = await self._resolve_principal(request)
        except PermissionError as exc:
            return JSONResponse(status_code=403, content={"detail": str(exc)})
        except Exception:  # pragma: no cover - defensive
            # Don't leak internal exception detail to the client.
            logger.exception("MCP authentication error")
            return JSONResponse(status_code=500, content={"detail": "Internal authentication error"})

        tokens = set_auth_context(user_id=user_id, is_admin=admin, allowed_partitions=allowed)
        try:
            return await call_next(request)
        finally:
            reset_auth_context(tokens)


# ---------------------------------------------------------------------------
# Tools — search
# ---------------------------------------------------------------------------


@server.tool(description="Semantic search across one or many partitions using OpenRAG search flow")
async def search_documents(query: str, partitions: list[str] | None = None, top_k: int | None = None) -> dict:
    return await _service().search_documents(
        query=query,
        partitions=partitions,
        top_k=top_k,
        allowed_partitions=get_allowed_partitions(),
    )


@server.tool(description="Semantic search restricted to one partition")
async def search_partition(query: str, partition: str, top_k: int | None = None) -> dict:
    return await _service().search_documents(
        query=query,
        partitions=[partition],
        top_k=top_k,
        allowed_partitions=get_allowed_partitions(),
    )


@server.tool(description="Semantic search restricted to one file inside one partition")
async def search_file(query: str, partition: str, file_id: str, top_k: int | None = None) -> dict:
    return await _service().search_documents(
        query=query,
        partitions=[partition],
        top_k=top_k,
        file_id=file_id,
        allowed_partitions=get_allowed_partitions(),
    )


# ---------------------------------------------------------------------------
# Tools — catalog browsing
# ---------------------------------------------------------------------------


@server.tool(
    description=(
        "List all partitions accessible to the current user. "
        "Returns partition names, creation timestamps, and total count."
    )
)
async def list_partitions() -> dict:
    return await _service().list_partitions(allowed_partitions=get_allowed_partitions())


@server.tool(
    description=(
        "List all files indexed in a given partition. "
        "Returns file IDs, original filenames, sizes, creation dates, and other metadata. "
        "Use `limit` to cap the number of results."
    )
)
async def list_files(partition: str, limit: int | None = None) -> dict:
    return await _service().list_files(
        partition=partition,
        allowed_partitions=get_allowed_partitions(),
        limit=limit,
    )


@server.tool(
    description=(
        "Get metadata and chunk count for a specific file inside a partition. "
        "Returns file metadata (filename, size, creation date, …) and the total number of indexed chunks."
    )
)
async def get_file_info(partition: str, file_id: str) -> dict:
    return await _service().get_file_info(
        partition=partition,
        file_id=file_id,
        allowed_partitions=get_allowed_partitions(),
    )


@server.tool(
    description=(
        "Fetch text chunks belonging to a specific file, one page at a time. "
        "Use `offset` (default 0) and `limit` (default 3) to page through the file. "
        "Keep `limit` small (3 or fewer) to avoid exceeding your context window — "
        "chunks can be long. "
        "The response includes `total_chunks` and `has_more` so you know whether to "
        "call again with a higher offset. Always check `has_more` and keep paging "
        "until it is false before drawing conclusions about the full file content."
    )
)
async def get_file_chunks(partition: str, file_id: str, offset: int = 0, limit: int = 3) -> dict:
    return await _service().get_file_chunks(
        partition=partition,
        file_id=file_id,
        allowed_partitions=get_allowed_partitions(),
        offset=offset,
        limit=limit,
    )


@server.tool(
    description=(
        "Fuzzy search across file names (filename, original_filename, file_id) "
        "using sequence-similarity matching. "
        "Results are ranked by similarity score (0–1). "
        "Optionally restrict the search to a single `partition`. "
        "Use `cutoff` (default 0.4) to control minimum similarity and `limit` (default 20) to cap results."
    )
)
async def fuzzy_search_files(query: str, partition: str | None = None, cutoff: float = 0.4, limit: int = 20) -> dict:
    return await _service().fuzzy_search_files(
        query=query,
        allowed_partitions=get_allowed_partitions(),
        partition=partition,
        cutoff=cutoff,
        limit=limit,
    )


@server.tool(
    description=(
        "Fetch a single indexed chunk by its ID. "
        "Chunk IDs appear in search results and in get_file_chunks output. "
        "Returns the full text content and all metadata for that chunk."
    )
)
async def get_chunk_by_id(chunk_id: str) -> dict:
    return await _service().get_chunk_by_id(
        chunk_id=chunk_id,
        allowed_partitions=get_allowed_partitions(),
    )


# ---------------------------------------------------------------------------
# Tools — task introspection
# ---------------------------------------------------------------------------


@server.tool(
    description=(
        "Get the current status and details of an indexation task. "
        "Task states: QUEUED → SERIALIZING → CHUNKING → INSERTING → COMPLETED (or FAILED). "
        "If the task failed, the error message is included in the response."
    )
)
async def get_indexation_task_status(task_id: str) -> dict:
    return await _service().get_task_status(
        task_id=task_id,
        user_id=get_user_id(),
        is_admin=is_admin(),
    )


@server.tool(
    description=(
        "List all indexation tasks belonging to the current user. "
        "Use `task_status` to filter: 'active' (queued/in-progress), 'completed', 'failed', "
        "or any exact state name. Omit to get all tasks."
    )
)
async def list_my_tasks(task_status: str | None = None) -> dict:
    return await _service().list_my_tasks(
        user_id=get_user_id(),
        is_admin=is_admin(),
        task_status=task_status,
    )


@server.tool(
    description=(
        "Fetch chronological log lines for a specific indexation task. "
        "Useful for diagnosing slow or stuck indexations. "
        "Use `max_lines` to cap output (default 100)."
    )
)
async def get_task_logs(task_id: str, max_lines: int = 100) -> dict:
    return await _service().get_task_logs(
        task_id=task_id,
        user_id=get_user_id(),
        is_admin=is_admin(),
        log_file=LOG_FILE,
        max_lines=max_lines,
    )


# ---------------------------------------------------------------------------
# Tools — write operations
# ---------------------------------------------------------------------------


@server.tool(
    description=(
        "Delete a file and all its indexed chunks from a partition. "
        "Requires editor (or owner) access to the partition. "
        "This operation is irreversible."
    )
)
async def delete_file(partition: str, file_id: str) -> dict:
    return await _service().delete_file(
        partition=partition,
        file_id=file_id,
        allowed_partitions=get_allowed_partitions(),
        user_id=get_user_id(),
    )


@server.tool(
    description=(
        "Update metadata fields of an existing indexed file without re-uploading it. "
        "Pass a JSON object with only the fields to change (e.g. author, title). "
        "To move the file to another partition, include a 'partition' key — "
        "you must have editor access to both the source and destination partitions."
    )
)
async def update_file_metadata(partition: str, file_id: str, metadata: dict) -> dict:
    return await _service().update_file_metadata(
        partition=partition,
        file_id=file_id,
        metadata=metadata,
        allowed_partitions=get_allowed_partitions(),
        user_id=get_user_id(),
    )


@server.tool(
    description=(
        "Copy a file from one partition to another. "
        "Requires read access to the source partition and editor access to the destination. "
        "Optionally supply `extra_metadata` to override fields in the copy."
    )
)
async def copy_file(
    source_partition: str,
    source_file_id: str,
    dest_partition: str,
    dest_file_id: str,
    extra_metadata: dict | None = None,
) -> dict:
    return await _service().copy_file(
        source_partition=source_partition,
        source_file_id=source_file_id,
        dest_partition=dest_partition,
        dest_file_id=dest_file_id,
        allowed_partitions=get_allowed_partitions(),
        user_id=get_user_id(),
        extra_metadata=extra_metadata,
    )


@server.tool(
    description=(
        "Download a document from a public HTTP/HTTPS URL and index it into a partition. "
        "Returns a task_id that can be polled with get_indexation_task_status. "
        "The file_id must be unique within the partition. "
        "Optionally supply `extra_metadata` (dict) to attach custom fields."
    )
)
async def index_url(url: str, partition: str, file_id: str, extra_metadata: dict | None = None) -> dict:
    return await _service().index_url(
        url=url,
        partition=partition,
        file_id=file_id,
        allowed_partitions=get_allowed_partitions(),
        user_id=get_user_id(),
        extra_metadata=extra_metadata,
    )


# ---------------------------------------------------------------------------
# App factory + entrypoint
# ---------------------------------------------------------------------------


def create_mcp_http_app():
    """Build the FastMCP streamable-HTTP app with auth + container lifecycle.

    The container is initialised on the serving event loop (asyncpg pools are
    loop-bound) by composing our startup/shutdown around FastMCP's own
    session-manager lifespan.
    """
    server.settings.host = mcp_config.host
    server.settings.port = mcp_config.port
    server.settings.streamable_http_path = mcp_config.path

    app = server.streamable_http_app()
    app.add_middleware(MCPAuthContextMiddleware)

    fastmcp_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _combined_lifespan(app_):
        await _startup()
        try:
            async with fastmcp_lifespan(app_):
                yield
        finally:
            await _shutdown()

    app.router.lifespan_context = _combined_lifespan
    return app


def main():
    import uvicorn

    uvicorn.run(create_mcp_http_app(), host=mcp_config.host, port=mcp_config.port)


if __name__ == "__main__":
    main()
