"""FastAPI application entry point (Phase 10A).

Phase 10A move of the FastAPI scaffolding out of ``openrag/main.py`` into
``api/main.py``. Behaviour is intentionally identical to the legacy
module — same middleware stack, same routers, same OpenAPI shape — so
the running app keeps working while the rest of Phase 10 drops new
infrastructure into this directory:

* 10B   ``api/error_handlers.py``     replaces the inline handlers below
* 10C   ``api/middleware/*.py``       replaces inline / legacy middleware
* 10D   ``api/dependencies/auth.py``  replaces ``routers/utils.require_admin``
* 10E   ``api/schemas/**/*.py``       formalises router response shapes
* 10F   ``api/routers/**/*.py``       replaces ``openrag/routers/*``
* 10G   entrypoint switch             ``uvicorn openrag.api.main:app``

Structural changes vs the legacy module:

* ``@app.on_event("startup"/"shutdown")`` -> single ``@asynccontextmanager``
  lifespan (FastAPI deprecated ``on_event`` in 0.105+).
* ``ray.init`` is guarded with ``is_initialized()`` so re-importing the
  module (tests, the legacy ``openrag/main.py`` shim still in place
  until 10G) is a no-op rather than an error.

The legacy ``openrag/main.py`` continues to run the existing entrypoint
(``uvicorn main:app``) until 10G flips Dockerfile / entrypoint.sh /
``__main__`` to ``openrag.api.main:app``.
"""

from __future__ import annotations

import os
import warnings
from contextlib import asynccontextmanager
from enum import Enum
from importlib.metadata import version as get_package_version
from pathlib import Path

import ray
import uvicorn

# Ray must be initialised before importing modules that look up actors
# at import time — services.workers.bootstrap creates the long-lived
# worker pool from its top level. ``ignore_reinit_error`` and the
# ``is_initialized`` guard keep parallel imports (legacy ``main.py`` +
# tests + this module) safe.
if not ray.is_initialized():
    ray.init(dashboard_host="0.0.0.0", ignore_reinit_error=True)

# flake8: noqa: E402  (ray.init must run first)
import services.workers.bootstrap  # noqa: F401  (side-effect actor creation)
from api.error_handlers import register_error_handlers
from api.middleware import (
    AuthMiddleware,
    InstrumentationMiddleware,
    RequestIdMiddleware,
    RequestTimeoutMiddleware,
)
from config import load_config
from di.container import ServiceContainer
from dotenv import dotenv_values
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from routers.actors import router as actors_router
from routers.auth import router as auth_router
from routers.extract import router as extract_router
from routers.indexer import router as indexer_router
from routers.monitoring import router as monitoring_router
from routers.openai import router as openai_router
from routers.partition import router as partition_router
from routers.queue import router as queue_router
from routers.search import router as search_router
from routers.tools import router as tools_router
from routers.users import router as users_router
from routers.utils import require_admin
from routers.workspaces import router as workspaces_router
from utils.logger import get_logger

# pydub 0.25.1 ships invalid-escape regex literals; the warning is upstream.
warnings.filterwarnings("ignore", category=SyntaxWarning, module="pydub")


# ---------------------------------------------------------------------------
# Config + env validation
# ---------------------------------------------------------------------------

logger = get_logger()
config = load_config()
DATA_DIR = Path(config.paths.data_dir)

SHARED_ENV = os.environ.get("SHARED_ENV", None)
env_vars = dotenv_values(SHARED_ENV) if SHARED_ENV else {}
env_vars["PYTHONPATH"] = "/app/openrag"

AUTH_TOKEN: str | None = os.getenv("AUTH_TOKEN")
INDEXERUI_PORT: str | None = os.getenv("INDEXERUI_PORT", "3042")
INDEXERUI_URL: str | None = os.getenv("INDEXERUI_URL", f"http://localhost:{INDEXERUI_PORT}")
CORS_EXTRA_ORIGINS: list[str] = [o.strip() for o in os.getenv("CORS_EXTRA_ORIGINS", "").split(";") if o.strip()]
WITH_CHAINLIT_UI: bool = os.getenv("WITH_CHAINLIT_UI", "true").lower() == "true"
WITH_OPENAI_API: bool = os.getenv("WITH_OPENAI_API", "true").lower() == "true"

AUTH_MODE: str = os.getenv("AUTH_MODE", "token").strip().lower()
if AUTH_MODE not in ("token", "oidc"):
    raise RuntimeError(f"Invalid AUTH_MODE={AUTH_MODE!r}. Expected 'token' or 'oidc'.")

OIDC_ENDPOINT: str | None = os.getenv("OIDC_ENDPOINT")
OIDC_CLIENT_ID: str | None = os.getenv("OIDC_CLIENT_ID")
OIDC_CLIENT_SECRET: str | None = os.getenv("OIDC_CLIENT_SECRET")
OIDC_REDIRECT_URI: str | None = os.getenv("OIDC_REDIRECT_URI")
OIDC_CLAIM_SOURCE: str = os.getenv("OIDC_CLAIM_SOURCE", "id_token").strip().lower()
OIDC_CLAIM_MAPPING: str = os.getenv("OIDC_CLAIM_MAPPING", "").strip()
OIDC_SCOPES: str = os.getenv("OIDC_SCOPES", "openid email profile offline_access")
OIDC_TOKEN_ENCRYPTION_KEY: str | None = os.getenv("OIDC_TOKEN_ENCRYPTION_KEY")
OIDC_POST_LOGOUT_REDIRECT_URI: str | None = os.getenv("OIDC_POST_LOGOUT_REDIRECT_URI")

# Whitelist of writable DB fields populated by OIDC claim mapping.
# is_admin / external_user_id / file_quota / token are either identity-
# defining or privilege-escalation vectors and must never be writable here.
_OIDC_CLAIM_MAPPING_ALLOWED_FIELDS = {"display_name", "email"}


def _parse_oidc_claim_mapping(raw: str) -> dict[str, str]:
    """Parse ``OIDC_CLAIM_MAPPING`` (CSV of ``db_field:claim`` pairs).

    Validates each pair against the whitelist and enforces non-empty
    claim names so misconfiguration fails fast at startup rather than
    silently at login time.
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


# ---------------------------------------------------------------------------
# Lifespan — owns the ServiceContainer lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """ServiceContainer lifecycle.

    Replaces the legacy ``@app.on_event("startup"/"shutdown")`` pair
    (deprecated since FastAPI 0.105). Container construction is
    best-effort: a Milvus / PG hiccup at startup MUST NOT block boot
    because ``di.providers.get_container`` already serves 503 while the
    container is absent. The full composition root lands in Phase 11; for
    now this matches the existing behaviour of ``openrag/main.py``.
    """
    container: ServiceContainer | None
    try:
        container = ServiceContainer(config)
    except Exception:  # pragma: no cover - defensive boot guard
        logger.exception("ServiceContainer wiring skipped")
        container = None

    app.state.container = container

    if container is not None:
        try:
            await container.initialize()
        except Exception:  # pragma: no cover - defensive boot guard
            # A half-initialised container (asyncpg pool never opened)
            # would route requests into broken repos and 500. Drop it so
            # di/providers.py serves the intended degraded 503 instead.
            logger.exception("ServiceContainer.initialize failed; serving degraded (503)")
            app.state.container = None
            container = None

    try:
        yield
    finally:
        live = getattr(app.state, "container", None)
        if live is not None:
            try:
                await live.shutdown()
            except Exception:  # pragma: no cover - defensive shutdown guard
                logger.exception("ServiceContainer.shutdown skipped")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(version=app_version, lifespan=lifespan)


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title="Openrag API",
        version=app.version,
        routes=app.routes,
    )
    openapi_schema["components"]["securitySchemes"] = {"BearerAuth": {"type": "http", "scheme": "bearer"}}
    openapi_schema["security"] = [{"BearerAuth": []}]
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi


# Middleware stack — registration is the reverse of execution
# (last ``add_middleware`` call wraps the outermost layer). The
# Phase 10C target order on a request is:
#
#     Instrumentation -> RequestTimeout -> RequestId -> Auth -> route
#
# so Auth is the innermost guard (lifespan's request_id already on
# request.state when an auth failure runs through the error handlers),
# and Instrumentation captures the full request duration including the
# auth check. CORS is added later and ends up outside this stack so
# preflights short-circuit before any instrumentation runs.
app.add_middleware(
    AuthMiddleware,
    get_auth_service=lambda request: request.app.state.container.auth_service,
)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(RequestTimeoutMiddleware)
app.add_middleware(InstrumentationMiddleware)

# Phase 10B centralises the OpenRAGError + generic Exception handlers in
# api/error_handlers.py — the inline decorators that used to live here
# have moved there. Response shape is unchanged.
register_error_handlers(app)


allow_origins = [
    "http://localhost:3042",
    "http://localhost:5173",
    INDEXERUI_URL,
    *CORS_EXTRA_ORIGINS,
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
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
    app. Prevents a bare ``http://localhost:APP_PORT/`` from returning
    404 after an OIDC login that used ``next=/``.
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


# Router mounts. Phase 10F moves these into ``api/routers/{user,admin,auth}/``
# one file at a time; the prefixes / tags stay identical so the OpenAPI
# schema and client SDKs do not break across the move.
app.include_router(indexer_router, prefix="/indexer", tags=[Tags.INDEXER])
app.include_router(extract_router, prefix="/extract", tags=[Tags.EXTRACT])
app.include_router(search_router, prefix="/search", tags=[Tags.SEARCH])
app.include_router(partition_router, prefix="/partition", tags=[Tags.PARTITION])
app.include_router(queue_router, prefix="/queue", tags=[Tags.QUEUE])
app.include_router(actors_router, prefix="/actors", tags=[Tags.ACTORS])
app.include_router(users_router, prefix="/users", tags=[Tags.USERS])
app.include_router(workspaces_router, tags=[Tags.WORKSPACES])
app.include_router(monitoring_router, tags=[Tags.MONITORING])
app.include_router(tools_router, prefix="/v1", tags=[Tags.TOOLS])
# Mount the auth router (OIDC flows). Most routes are bypassed by
# AuthMiddleware; ``/auth/me`` remains protected.
app.include_router(auth_router, tags=["Authentication"])

# Mount openai router if either OpenAI API or Chainlit UI is enabled
# (chainlit uses the openai-compatible endpoints).
if WITH_OPENAI_API or WITH_CHAINLIT_UI:
    app.include_router(openai_router, prefix="/v1", tags=[Tags.OPENAI])

if WITH_CHAINLIT_UI:
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
        # When fronted by a reverse proxy, ``proxy_headers`` alone is not
        # enough: uvicorn only honours X-Forwarded-Proto / -For from peers
        # listed in ``forwarded_allow_ips`` (default: 127.0.0.1). Without
        # this, a proxy outside loopback (the usual docker-compose / k8s
        # case) is ignored, ``request.url.scheme`` stays 'http',
        # ``_is_request_secure`` returns False, and the OIDC
        # ``openrag_session`` + state cookies ship with ``Secure=False``
        # on HTTPS deployments.
        forwarded_allow_ips = os.environ.get("UVICORN_FORWARDED_ALLOW_IPS", "127.0.0.1")
        logger.info("Trusting proxy headers from forwarded_allow_ips=%s", forwarded_allow_ips)
        uvicorn.run(
            "api.main:app",
            host="0.0.0.0",
            port=8080,
            reload=True,
            proxy_headers=True,
            forwarded_allow_ips=forwarded_allow_ips,
        )
