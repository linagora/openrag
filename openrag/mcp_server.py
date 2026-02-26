import os

from components.mcp.adapters import RayIndexerSearchGateway
from components.mcp.auth_context import get_allowed_partitions, get_user_id, reset_auth_context, set_auth_context
from components.mcp.indexation_service import IndexationService
from components.mcp.service import SearchToolService
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

server = FastMCP(server_name, stateless_http=True, json_response=True)
search_service = SearchToolService(
    gateway=RayIndexerSearchGateway(),
    default_top_k=default_top_k,
    max_top_k=max_top_k,
    similarity_threshold=similarity_threshold,
)
indexation_service = IndexationService()


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
    return await search_service.search_documents(
        query=query,
        partitions=partitions,
        top_k=top_k,
        allowed_partitions=get_allowed_partitions(),
    )


@server.tool(description="Semantic search restricted to one partition")
async def search_partition(query: str, partition: str, top_k: int | None = None) -> dict:
    return await search_service.search_documents(
        query=query,
        partitions=[partition],
        top_k=top_k,
        allowed_partitions=get_allowed_partitions(),
    )


@server.tool(description="Semantic search restricted to one file inside one partition")
async def search_file(query: str, partition: str, file_id: str, top_k: int | None = None) -> dict:
    return await search_service.search_documents(
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
    return await indexation_service.list_partitions(
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
    return await indexation_service.list_files(
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
    return await indexation_service.get_file_info(
        partition=partition,
        file_id=file_id,
        allowed_partitions=get_allowed_partitions(),
    )


@server.tool(
    description=(
        "Fetch the full text content of every chunk belonging to a specific file. "
        "Each chunk includes its chunk_id, text content, and metadata. "
        "Useful for reading the raw indexed content of a document."
    )
)
async def get_file_chunks(partition: str, file_id: str) -> dict:
    """Return all text chunks for a file in full."""
    return await indexation_service.get_file_chunks(
        partition=partition,
        file_id=file_id,
        allowed_partitions=get_allowed_partitions(),
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
    return await indexation_service.fuzzy_search_files(
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
    return await indexation_service.get_task_status(
        task_id=task_id,
        user_id=get_user_id(),
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
