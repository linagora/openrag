import os
import re
import warnings
from enum import Enum
from importlib.metadata import version as get_package_version
from pathlib import Path
from urllib.parse import parse_qs

import ray
import uvicorn
from config import load_config
from dotenv import dotenv_values
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

ray.init(dashboard_host="0.0.0.0")

# Apply noqa: E402 to ignore "module level import not at top of file" cause ray.init has to be called first

# flake8: noqa: E402


from routers.actors import router as actors_router
from routers.extract import router as extract_router
from routers.indexer import router as indexer_router
from routers.openai import router as openai_router
from routers.partition import router as partition_router
from routers.queue import router as queue_router
from routers.search import router as search_router
from routers.tools import router as tools_router
from routers.users import router as users_router
from starlette.middleware.base import BaseHTTPMiddleware
from utils.dependencies import get_vectordb
from utils.exceptions import OpenRAGError
from utils.logger import get_logger

# Filter SyntaxWarning from pydub (invalid escape sequences in regex)
# This is a known issue in pydub 0.25.1 that hasn't been fixed upstream
warnings.filterwarnings("ignore", category=SyntaxWarning, module="pydub")


SHARED_ENV = os.environ.get("SHARED_ENV", None)

env_vars = dotenv_values(SHARED_ENV) if SHARED_ENV else {}
env_vars["PYTHONPATH"] = "/app/openrag"


logger = get_logger()
config = load_config()
DATA_DIR = Path(config.paths.data_dir)


class Tags(Enum):
    VDB = "VectorDB operations"
    INDEXER = ("Indexer",)
    SEARCH = ("Semantic Search",)
    OPENAI = ("OpenAI Compatible API",)
    EXTRACT = ("Document extracts",)
    PARTITION = ("Partitions & files",)
    QUEUE = ("Queue management",)
    ACTORS = ("Ray Actors",)
    USERS = ("User management",)
    TOOLS = ("Tools",)


class AppState:
    def __init__(self, config):
        self.config = config
        self.data_dir = Path(config.paths.data_dir)


# Read the token from env (or None if not set)
AUTH_TOKEN: str | None = os.getenv("AUTH_TOKEN")
INDEXERUI_PORT: str | None = os.getenv("INDEXERUI_PORT", "3042")
INDEXERUI_URL: str | None = os.getenv("INDEXERUI_URL", f"http://localhost:{INDEXERUI_PORT}")
WITH_CHAINLIT_UI: bool = os.getenv("WITH_CHAINLIT_UI", "true").lower() == "true"
WITH_OPENAI_API: bool = os.getenv("WITH_OPENAI_API", "true").lower() == "true"


try:
    app_version = get_package_version("openrag")
except Exception:
    app_version = "unknown"

app = FastAPI(version=app_version)


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title="Openrag API",
        version=app.version,
        routes=app.routes,
    )
    # Add global security
    openapi_schema["components"]["securitySchemes"] = {"BearerAuth": {"type": "http", "scheme": "bearer"}}
    openapi_schema["security"] = [{"BearerAuth": []}]
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi


class TokenRedactingMiddleware(BaseHTTPMiddleware):
    """Middleware to redact sensitive tokens from access logs while preserving original for auth."""

    TOKEN_PATTERN = re.compile(r"(token=)[^&\s]+", re.IGNORECASE)

    async def dispatch(self, request: Request, call_next):
        # Store original query string before redacting
        original_query_string = request.scope.get("query_string", b"").decode()

        # Preserve original token in request.state for AuthMiddleware to use
        if "token=" in original_query_string.lower():
            # Extract and store the original token
            params = parse_qs(original_query_string)
            request.state.original_token = params.get("token", [None])[0]

            # Redact token from query string for logging purposes
            redacted_query = self.TOKEN_PATTERN.sub(r"\1[REDACTED]", original_query_string)
            request.scope["query_string"] = redacted_query.encode()

        response = await call_next(request)
        return response


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        vectordb = get_vectordb()
        # Skip if no AUTH_TOKEN configured
        if AUTH_TOKEN is None:
            user = await vectordb.get_user.remote(1)
            user_partitions = await vectordb.list_user_partitions.remote(1)
            request.state.user = user
            request.state.user_partitions = user_partitions
            return await call_next(request)

        # routes to allow access to without token bearer
        if request.url.path in [
            "/docs",
            "/openapi.json",
            "/redoc",
            "/health_check",
            "/version",
        ] or request.url.path.startswith("/chainlit"):  # Allow all chainlit subroutes
            return await call_next(request)

        # Extract token
        token = None

        # For /static routes, allow token via query parameter (this easy file viewing with a link without a bearer)
        # usage http://localhost:8080/static?token=api_key
        if request.url.path.startswith("/static"):
            # Use preserved original token (before redaction) if available
            token = getattr(request.state, "original_token", None)
        else:
            # For all other routes, require Bearer header
            # # Extract Bearer token
            auth = request.headers.get("authorization", "")
            if auth and auth.lower().startswith("bearer "):
                token = auth.split(" ", 1)[1]

        if not token:
            return JSONResponse(status_code=403, content={"detail": "Missing token"})

        # Lookup user in DB
        user = await vectordb.get_user_by_token.remote(token)
        if not user:
            return JSONResponse(status_code=403, content={"detail": "Invalid token"})

        # Load user partitions
        user_partitions = await vectordb.list_user_partitions.remote(user["id"])

        # Attach to request
        request.state.user = user
        request.state.user_partitions = user_partitions
        return await call_next(request)


# Register middlewares (order matters - last added runs first)
app.add_middleware(AuthMiddleware)
app.add_middleware(TokenRedactingMiddleware)


# Exception handlers
@app.exception_handler(OpenRAGError)
async def openrag_exception_handler(request: Request, exc: OpenRAGError):
    logger = get_logger()
    logger.error("OpenRAGError occurred", error=str(exc))
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict())


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger = get_logger()
    logger.exception("Unhandled exception", error=str(exc))
    return JSONResponse(
        status_code=500,
        content={"detail": "[UNEXPECTED_ERROR]: An unexpected error occurred", "extra": {}},
    )


# Add CORS middleware
allow_origins = [
    "http://localhost:3042",
    "http://localhost:5173",
    INDEXERUI_URL,
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,  # Adjust as needed for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.state.app_state = AppState(config)
app.mount("/static", StaticFiles(directory=DATA_DIR.resolve(), check_dir=True), name="static")


@app.get("/health_check", summary="Health check endpoint for API", dependencies=[])
async def health_check(request: Request):
    # TODO : Error reporting about llm and vlm
    return "RAG API is up."


@app.get("/version", summary="Get openRAG version", dependencies=[])
def get_version():
    return {"version": app.version}


# Mount the indexer router
app.include_router(indexer_router, prefix="/indexer", tags=[Tags.INDEXER])
# Mount the extract router
app.include_router(extract_router, prefix="/extract", tags=[Tags.EXTRACT])
# Mount the search router
app.include_router(search_router, prefix="/search", tags=[Tags.SEARCH])
# Mount the partition router
app.include_router(partition_router, prefix="/partition", tags=[Tags.PARTITION])
# Mount the queue router
app.include_router(queue_router, prefix="/queue", tags=[Tags.QUEUE])
# Mount the actors router
app.include_router(actors_router, prefix="/actors", tags=[Tags.ACTORS])
# Mount the users router
app.include_router(users_router, prefix="/users", tags=[Tags.USERS])

app.include_router(tools_router, prefix="/v1", tags=[Tags.TOOLS])

# Mount openai router if either OpenAI API or Chainlit UI is enabled (chainlit uses openai api endpoints)
if WITH_OPENAI_API or WITH_CHAINLIT_UI:
    app.include_router(openai_router, prefix="/v1", tags=[Tags.OPENAI])

if WITH_CHAINLIT_UI:
    # Mount the default front
    from chainlit.utils import mount_chainlit

    mount_chainlit(app, "./app_front.py", path="/chainlit")

if __name__ == "__main__":
    if config.ray.serve.enable:
        from ray import serve

        @serve.deployment(num_replicas=config.ray.serve.num_replicas)
        @serve.ingress(app)
        class OpenRagAPI:
            pass

        serve.start(http_options={"host": config.ray.serve.host, "port": config.ray.serve.port})
        if WITH_CHAINLIT_UI:
            from chainlit_api import app as chainlit_app

            serve.run(OpenRagAPI.bind(), route_prefix="/")
            uvicorn.run(chainlit_app, host="0.0.0.0", port=config.ray.serve.chainlit_port)
        else:
            serve.run(OpenRagAPI.bind(), route_prefix="/", blocking=True)

    else:
        uvicorn.run("api:app", host="0.0.0.0", port=8080, reload=True, proxy_headers=True)
