import os

from config import load_config
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from components.mcp.adapters import RayIndexerSearchGateway
from components.mcp.auth_context import get_allowed_partitions, reset_auth_context, set_auth_context
from components.mcp.service import SearchToolService
from routers.utils import current_user_or_admin_partitions_list
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
