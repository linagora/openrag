import os
from pathlib import Path

from components.app.service import OpenRAGApplicationService
from components.mcp.auth_context import get_allowed_partitions, get_user_id, reset_auth_context, set_auth_context
from config import load_config
from mcp.server.fastmcp import FastMCP
from routers.utils import current_user_or_admin_partitions_list
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from utils.dependencies import get_vectordb

config = load_config()
mcp_config = config.get("mcp", {})

server_name = mcp_config.get("server_name", "OpenRAG MCP")
host = str(mcp_config.get("host", "0.0.0.0"))
port = int(mcp_config.get("port", 8081))
path = str(mcp_config.get("path", "/mcp"))
default_top_k = int(mcp_config.get("default_top_k", 5))
max_top_k = int(mcp_config.get("max_top_k", 50))
similarity_threshold = float(mcp_config.get("similarity_threshold", 0.8))

LOG_FILE = Path(config.paths.log_dir or "logs") / "app.json"

server = FastMCP(server_name, stateless_http=True, json_response=True)
app_service = OpenRAGApplicationService(
    default_top_k=default_top_k,
    max_top_k=max_top_k,
    similarity_threshold=similarity_threshold,
)
# Backward-compatible aliases used by existing tests
search_service = app_service
indexation_service = app_service


AUTH_TOKEN: str | None = os.getenv("AUTH_TOKEN")


class MCPAuthContextMiddleware(BaseHTTPMiddleware):
    async def _resolve_user_context(self, request: Request):
        if getattr(request.state, "user", None) and getattr(request.state, "user_partitions", None) is not None:
            return

        vectordb = get_vectordb()

        if AUTH_TOKEN is None:
            request.state.user = await vectordb.get_user.remote(1)
            request.state.user_partitions = await vectordb.list_user_partitions.remote(1)
            return

        auth = request.headers.get("authorization", "")
        token = None
        if auth and auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1]

        if not token:
            raise PermissionError("Missing token")

        user = await vectordb.get_user_by_token.remote(token)
        if not user:
            raise PermissionError("Invalid token")

        request.state.user = user
        request.state.user_partitions = await vectordb.list_user_partitions.remote(user["id"])

    async def dispatch(self, request: Request, call_next):
        try:
            await self._resolve_user_context(request)
        except PermissionError as exc:
            return JSONResponse(status_code=403, content={"detail": str(exc)})

        user = request.state.user
        user_id = user.get("id")
        allowed_partitions = current_user_or_admin_partitions_list(request)
        tokens = set_auth_context(user_id=user_id, partitions=allowed_partitions)
        try:
            return await call_next(request)
        finally:
            reset_auth_context(tokens)


@server.tool(description="Semantic search across one or many partitions using OpenRAG search flow")
async def search_documents(query: str, partitions: list[str] | None = None, top_k: int | None = None) -> dict:
    return await app_service.search_documents(
        query=query,
        partitions=partitions,
        top_k=top_k,
        allowed_partitions=get_allowed_partitions(),
    )


@server.tool(description="Semantic search restricted to one partition")
async def search_partition(query: str, partition: str, top_k: int | None = None) -> dict:
    return await app_service.search_documents(
        query=query,
        partitions=[partition],
        top_k=top_k,
        allowed_partitions=get_allowed_partitions(),
    )


@server.tool(description="Semantic search restricted to one file inside one partition")
async def search_file(query: str, partition: str, file_id: str, top_k: int | None = None) -> dict:
    return await app_service.search_documents(
        query=query,
        partitions=[partition],
        top_k=top_k,
        file_id=file_id,
        allowed_partitions=get_allowed_partitions(),
    )


@server.tool(
    description=(
        "List all partitions accessible to the current user. "
        "Returns partition names, creation timestamps, and total count."
    )
)
async def list_partitions() -> dict:
    """List the partitions the current user has access to."""
    return await app_service.list_partitions(
        allowed_partitions=get_allowed_partitions(),
    )


@server.tool(
    description=(
        "List all files indexed in a given partition. "
        "Returns file IDs, original filenames, sizes, creation dates, and other metadata. "
        "Use `limit` to cap the number of results."
    )
)
async def list_files(partition: str, limit: int | None = None) -> dict:
    """List indexed files in a partition."""
    return await app_service.list_files(
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
    """Return metadata and chunk count for a file."""
    return await app_service.get_file_info(
        partition=partition,
        file_id=file_id,
        allowed_partitions=get_allowed_partitions(),
    )


@server.tool(
    description=(
        "Fetch text chunks belonging to a specific file, one page at a time. "
        "Use `offset` (default 0) and `limit` (default 10) to page through the file. "
        "The response includes `total_chunks` and `has_more` so you know whether to "
        "call again with a higher offset. Always check `has_more` and keep paging "
        "until it is false before drawing conclusions about the full file content."
    )
)
async def get_file_chunks(
    partition: str,
    file_id: str,
    offset: int = 0,
    limit: int = 10,
) -> dict:
    """Return a page of text chunks for a file."""
    return await app_service.get_file_chunks(
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
async def fuzzy_search_files(
    query: str,
    partition: str | None = None,
    cutoff: float = 0.4,
    limit: int = 20,
) -> dict:
    """Fuzzy search on file names across accessible partitions."""
    return await app_service.fuzzy_search_files(
        query=query,
        allowed_partitions=get_allowed_partitions(),
        partition=partition,
        cutoff=cutoff,
        limit=limit,
    )


@server.tool(
    description=(
        "Get the current status and details of an indexation task. "
        "Task states: QUEUED → SERIALIZING → CHUNKING → INSERTING → COMPLETED (or FAILED). "
        "If the task failed, the error message is included in the response."
    )
)
async def get_indexation_task_status(task_id: str) -> dict:
    """Return the status of an indexation task by its task_id."""
    return await app_service.get_task_status(
        task_id=task_id,
        user_id=get_user_id(),
    )


@server.tool(
    description=(
        "List all indexation tasks belonging to the current user. "
        "Use `task_status` to filter: 'active' (queued/in-progress), 'completed', 'failed', "
        "or any exact state name. Omit to get all tasks."
    )
)
async def list_my_tasks(task_status: str | None = None) -> dict:
    """Return the current user's indexation tasks, optionally filtered by state."""
    return await app_service.list_my_tasks(
        user_id=get_user_id(),
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
    """Return structured log lines for a task."""
    return await app_service.get_task_logs(
        task_id=task_id,
        user_id=get_user_id(),
        log_file=LOG_FILE,
        max_lines=max_lines,
    )


@server.tool(
    description=(
        "Fetch a single indexed chunk by its ID. "
        "Chunk IDs appear in search results and in get_file_chunks output. "
        "Returns the full text content and all metadata for that chunk."
    )
)
async def get_chunk_by_id(chunk_id: str) -> dict:
    """Return the content and metadata of a specific chunk."""
    return await app_service.get_chunk_by_id(
        chunk_id=chunk_id,
        allowed_partitions=get_allowed_partitions(),
    )


@server.tool(
    description=(
        "Delete a file and all its indexed chunks from a partition. "
        "Requires editor (or owner) access to the partition. "
        "This operation is irreversible."
    )
)
async def delete_file(partition: str, file_id: str) -> dict:
    """Delete a file from a partition."""
    return await app_service.delete_file(
        partition=partition,
        file_id=file_id,
        allowed_partitions=get_allowed_partitions(),
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
    """Update metadata for a file in-place."""
    return await app_service.update_file_metadata(
        partition=partition,
        file_id=file_id,
        metadata=metadata,
        allowed_partitions=get_allowed_partitions(),
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
    """Copy a file between partitions."""
    return await app_service.copy_file(
        source_partition=source_partition,
        source_file_id=source_file_id,
        dest_partition=dest_partition,
        dest_file_id=dest_file_id,
        allowed_partitions=get_allowed_partitions(),
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
async def index_url(
    url: str,
    partition: str,
    file_id: str,
    extra_metadata: dict | None = None,
) -> dict:
    """Fetch a URL and index the document into a partition."""
    return await app_service.index_url(
        url=url,
        partition=partition,
        file_id=file_id,
        allowed_partitions=get_allowed_partitions(),
        extra_metadata=extra_metadata,
    )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_mcp_http_app():
    server.settings.streamable_http_path = "/"
    app = server.streamable_http_app()
    app.add_middleware(MCPAuthContextMiddleware)
    return app


def configure_http_settings():
    server.settings.host = host
    server.settings.port = port
    server.settings.streamable_http_path = path


def main():
    configure_http_settings()
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
