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
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

ray.init(dashboard_host="0.0.0.0")

# Apply noqa: E402 to ignore "module level import not at top of file" cause ray.init has to be called first

# flake8: noqa: E402


from components.auth.middleware import AuthMiddleware
from routers.actors import router as actors_router
from routers.auth import router as auth_router
from routers.extract import router as extract_router
from routers.indexer import router as indexer_router
from routers.monitoring import MonitoringMiddleware
from routers.monitoring import router as monitoring_router
from routers.openai import router as openai_router
from routers.partition import router as partition_router
from routers.queue import router as queue_router
from routers.search import router as search_router
from routers.tools import router as tools_router
from routers.users import router as users_router
from routers.utils import require_admin
from routers.workspaces import router as workspaces_router
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
    WORKSPACES = ("Workspaces",)
    TOOLS = ("Tools",)
    MONITORING = ("Monitoring",)


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

AUTH_MODE: str = os.getenv("AUTH_MODE", "token").strip().lower()
if AUTH_MODE not in ("token", "oidc"):
    raise RuntimeError(f"Invalid AUTH_MODE={AUTH_MODE!r}. Expected 'token' or 'oidc'.")

# OIDC configuration (only required when AUTH_MODE=oidc)
OIDC_ENDPOINT: str | None = os.getenv("OIDC_ENDPOINT")
OIDC_CLIENT_ID: str | None = os.getenv("OIDC_CLIENT_ID")
OIDC_CLIENT_SECRET: str | None = os.getenv("OIDC_CLIENT_SECRET")
OIDC_REDIRECT_URI: str | None = os.getenv("OIDC_REDIRECT_URI")
OIDC_CLAIM_SOURCE: str = os.getenv("OIDC_CLAIM_SOURCE", "id_token").strip().lower()
OIDC_CLAIM_MAPPING: str = os.getenv("OIDC_CLAIM_MAPPING", "").strip()
OIDC_SCOPES: str = os.getenv("OIDC_SCOPES", "openid email profile offline_access")
OIDC_TOKEN_ENCRYPTION_KEY: str | None = os.getenv("OIDC_TOKEN_ENCRYPTION_KEY")
OIDC_POST_LOGOUT_REDIRECT_URI: str = os.getenv("OIDC_POST_LOGOUT_REDIRECT_URI", "/")

# Whitelist of writable DB fields populated by OIDC claim mapping.
# Never allow is_admin / external_user_id / file_quota / token here —
# those are either identity-defining or privilege-escalation vectors.
_OIDC_CLAIM_MAPPING_ALLOWED_FIELDS = {"display_name", "email"}


def _parse_oidc_claim_mapping(raw: str) -> dict[str, str]:
    """Parse the ``OIDC_CLAIM_MAPPING`` env var (CSV of ``db_field:claim`` pairs).

    Validates each pair against the whitelist and enforces non-empty values so
    misconfiguration fails fast at startup rather than silently at login time.
    """
    if not raw:
        return {}
    mapping: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            raise RuntimeError(f"Invalid OIDC_CLAIM_MAPPING entry {pair!r}: expected 'db_field:claim'")
        db_field, claim = pair.split(":", 1)
        db_field = db_field.strip()
        claim = claim.strip()
        if db_field not in _OIDC_CLAIM_MAPPING_ALLOWED_FIELDS:
            raise RuntimeError(
                f"OIDC_CLAIM_MAPPING db_field {db_field!r} is not writable "
                f"(allowed: {sorted(_OIDC_CLAIM_MAPPING_ALLOWED_FIELDS)})"
            )
        if not claim:
            raise RuntimeError(f"OIDC_CLAIM_MAPPING entry for {db_field!r} has empty claim name")
        mapping[db_field] = claim
    return mapping


OIDC_CLAIM_MAPPING_PARSED: dict[str, str] = _parse_oidc_claim_mapping(OIDC_CLAIM_MAPPING)

if AUTH_MODE == "oidc":
    _missing = [
        name
        for name, val in [
            ("OIDC_ENDPOINT", OIDC_ENDPOINT),
            ("OIDC_CLIENT_ID", OIDC_CLIENT_ID),
            ("OIDC_CLIENT_SECRET", OIDC_CLIENT_SECRET),
            ("OIDC_REDIRECT_URI", OIDC_REDIRECT_URI),
            ("OIDC_TOKEN_ENCRYPTION_KEY", OIDC_TOKEN_ENCRYPTION_KEY),
        ]
        if not val
    ]
    if _missing:
        raise RuntimeError("AUTH_MODE=oidc but the following env vars are missing or empty: " + ", ".join(_missing))
    if OIDC_CLAIM_SOURCE not in ("id_token", "userinfo"):
        raise RuntimeError(f"Invalid OIDC_CLAIM_SOURCE={OIDC_CLAIM_SOURCE!r}. Expected 'id_token' or 'userinfo'.")
    logger.info(
        "OIDC authentication mode enabled",
        issuer=OIDC_ENDPOINT,
        claim_source=OIDC_CLAIM_SOURCE,
        claim_mapping_fields=sorted(OIDC_CLAIM_MAPPING_PARSED.keys()),
    )


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


# Register middlewares (order matters - last added runs first)
app.add_middleware(AuthMiddleware, get_vectordb=get_vectordb)
app.add_middleware(TokenRedactingMiddleware)
app.add_middleware(MonitoringMiddleware)


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


@app.get("/", include_in_schema=False)
def root_redirect():
    """Root handler — sends authenticated users to the indexer-ui (if
    configured on a separate host) or the chainlit chat mounted on this
    app. Prevents a bare ``http://localhost:APP_PORT/`` from returning 404
    after an OIDC login that used ``next=/``.
    """
    # INDEXERUI_URL always has a default (localhost:INDEXERUI_PORT); only
    # redirect there when it points to a different host/port than us —
    # otherwise we'd loop.
    if INDEXERUI_URL and f":{os.getenv('APP_PORT', '8080')}" not in INDEXERUI_URL:
        return RedirectResponse(url=INDEXERUI_URL, status_code=302)
    if WITH_CHAINLIT_UI:
        return RedirectResponse(url="/chainlit/", status_code=302)
    return JSONResponse({"status": "ok", "app": "openrag", "version": app.version})


@app.get("/health_check", summary="Health check endpoint for API", dependencies=[])
async def health_check(request: Request):
    # TODO : Error reporting about llm and vlm
    return "RAG API is up."


@app.get("/version", summary="Get openRAG version", dependencies=[])
def get_version():
    return {"version": app.version}


@app.get("/config", summary="Get current configuration", tags=["Configuration"], dependencies=[Depends(require_admin)])
def get_config():
    return config


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
# Mount the workspaces router
app.include_router(workspaces_router, tags=[Tags.WORKSPACES])
# Mount the monitoring router
app.include_router(monitoring_router, tags=[Tags.MONITORING])

app.include_router(tools_router, prefix="/v1", tags=[Tags.TOOLS])

# Mount the auth router (OIDC flows). Routes are mostly bypassed by AuthMiddleware
# except `/auth/me` which remains protected.
app.include_router(auth_router, tags=["Authentication"])

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
