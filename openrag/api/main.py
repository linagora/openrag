"""FastAPI application entry point.

Phase 10A lifted the FastAPI scaffolding out of the legacy
``openrag/main.py``; 10B-10F filled in the surrounding infrastructure
(``error_handlers``, ``middleware/``, ``routers/``); 10G flipped
``entrypoint.sh`` and ``uvicorn`` over to ``api.main:app`` and deleted
the legacy module + its shims. This file is the only entry point.

Notable shape vs the deleted legacy module:

* ``@app.on_event("startup"/"shutdown")`` -> single ``@asynccontextmanager``
  lifespan (FastAPI deprecated ``on_event`` in 0.105+).
* ``ray.init`` runs inside the lifespan rather than at module load, so
  importing this module does not touch the Ray runtime. The worker
  bootstrap (``services.workers.bootstrap``) is imported from the
  lifespan immediately after ``ray.init`` to create the detached worker
  actors before the container reads from them.
"""

from __future__ import annotations

import asyncio
import os
import warnings
from contextlib import asynccontextmanager
from enum import Enum
from importlib.metadata import version as get_package_version

import ray
import uvicorn
from api.cors_config import sanitize_cors_origins
from api.dependencies.auth import require_admin
from api.error_handlers import register_error_handlers
from api.middleware import (
    AuthMiddleware,
    InstrumentationMiddleware,
    RateLimitMiddleware,
    RequestIdMiddleware,
    RequestTimeoutMiddleware,
    SecurityHeadersMiddleware,
)
from api.routers.admin.cluster import router as actors_router
from api.routers.admin.indexing import router as indexer_router
from api.routers.admin.jobs import router as queue_router
from api.routers.admin.model_endpoints import router as model_endpoints_router
from api.routers.admin.monitoring import router as monitoring_router
from api.routers.admin.partitions import router as partition_router
from api.routers.admin.presets import router as presets_router
from api.routers.admin.tools import router as tools_router
from api.routers.admin.users import router as users_router
from api.routers.admin.workspaces import router as workspaces_router
from api.routers.auth.oidc import router as auth_router
from api.routers.user.chat import prime_max_model_tokens
from api.routers.user.chat import router as openai_router
from api.routers.user.download import router as download_router
from api.routers.user.extract import router as extract_router
from api.routers.user.health import router as health_router
from api.routers.user.search import router as search_router
from core.config import load_config
from core.utils.banner import print_startup_banner
from core.utils.logging import get_logger
from di.container import ServiceContainer
from di.providers import set_container
from di.workers import ensure_worker_bootstrap
from dotenv import dotenv_values
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, RedirectResponse

# pydub 0.25.1 ships invalid-escape regex literals; the warning is upstream.
warnings.filterwarnings("ignore", category=SyntaxWarning, module="pydub")


# ---------------------------------------------------------------------------
# Config + env validation
# ---------------------------------------------------------------------------

logger = get_logger()
settings = load_config()
CONTAINER_STARTUP_TIMEOUT = float(
    os.getenv("OPENRAG_CONTAINER_STARTUP_TIMEOUT", max(60, settings.rdb.command_timeout * 4))
)

SHARED_ENV = os.environ.get("SHARED_ENV", None)
env_vars = dotenv_values(SHARED_ENV) if SHARED_ENV else {}
env_vars["PYTHONPATH"] = "/app/openrag"

AUTH_TOKEN: str | None = os.getenv("AUTH_TOKEN")
INDEXERUI_PORT: str | None = os.getenv("INDEXERUI_PORT", "3042")
INDEXERUI_URL: str | None = os.getenv("INDEXERUI_URL", f"http://localhost:{INDEXERUI_PORT}")
CORS_EXTRA_ORIGINS: list[str] = [o.strip() for o in os.getenv("CORS_EXTRA_ORIGINS", "").split(";") if o.strip()]
WITH_CHAINLIT_UI: bool = os.getenv("WITH_CHAINLIT_UI", "true").lower() == "true"
WITH_OPENAI_API: bool = os.getenv("WITH_OPENAI_API", "true").lower() == "true"

# AUTH_MODE / OIDC_* env validation lives on OIDCConfig.from_env() so
# the rules are configuration, not module-load code. ServiceContainer
# invokes the validator inside __init__, which makes misconfiguration
# trip when the lifespan constructs the container rather than at the
# first request.

try:
    app_version = get_package_version("openrag")
except Exception:
    app_version = "unknown"


class Tags(Enum):
    """OpenAPI tag labels used by mounted routers."""

    VDB = "VectorDB operations"
    INDEXER = "Indexer"
    SEARCH = "Semantic Search"
    OPENAI = "OpenAI Compatible API"
    EXTRACT = "Document extracts"
    PARTITION = "Partitions & files"
    MODEL_ENDPOINTS = "Model Endpoints"
    PRESETS = "Presets"
    QUEUE = "Queue management"
    ACTORS = "Ray Actors"
    USERS = "User management"
    WORKSPACES = "Workspaces"
    TOOLS = "Tools"
    MONITORING = "Monitoring"


# ---------------------------------------------------------------------------
# Lifespan — owns the ServiceContainer lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Boot sequence.

    Replaces the legacy ``@app.on_event("startup"/"shutdown")`` pair
    (deprecated since FastAPI 0.105). Order:

    1. ``ray.init`` (guarded by ``ray.is_initialized``) so test imports
       can pre-initialise Ray with a custom ``runtime_env``.
    2. ``services.workers.bootstrap`` — imported here (not at module
       load) so the detached worker actors are created with Ray live.
    3. ``ServiceContainer`` — best-effort, mirroring the legacy boot
       behaviour: a Milvus / PG hiccup MUST NOT block boot because
       ``di.providers.get_container`` already serves 503 while the
       container is absent.
    4. ``prime_max_model_tokens`` — caches the vLLM ``max_model_len`` so
       per-request validation in the chat router stays synchronous.
    5. ``set_container`` — registers the resolved container as the
       process-level singleton for callers that resolve it without a
       request; cleared on shutdown.
    """
    if not ray.is_initialized():
        logger.info("Startup: initializing Ray")
        _ray_address = os.environ.get("RAY_ADDRESS")
        if _ray_address:
            # Connect to an external Ray cluster (e.g. a dedicated ray-head
            # container). No local dashboard is started — the head node owns it.
            ray.init(address=_ray_address, ignore_reinit_error=True)
        else:
            # Embedded mode: start a local Ray cluster inside this process.
            # Bind the Ray dashboard to localhost by default; the dashboard /
            # Jobs API is unauthenticated (CVE-2023-48022 "ShadowRay") so it
            # must never listen on a routable interface. Operators that front it
            # with an auth proxy can override via RAY_DASHBOARD_HOST.
            ray.init(
                dashboard_host=os.environ.get("RAY_DASHBOARD_HOST", "127.0.0.1"),
                ignore_reinit_error=True,
            )
    logger.info("Startup: Ray is initialized")

    # ``ensure_worker_bootstrap`` imports ``services.workers.bootstrap``
    # for its side effect: creating the long-lived detached worker
    # actors (TaskStateManager, MarkerPool, semaphores).
    # The indirection through :mod:`di.workers` keeps API code free of
    # direct ``services.workers`` imports.
    logger.info("Startup: initializing worker bootstrap")
    ensure_worker_bootstrap(settings)
    logger.info("Startup: worker bootstrap initialized")

    container: ServiceContainer | None
    try:
        logger.info("Startup: wiring ServiceContainer")
        container = ServiceContainer(settings)
        logger.info("Startup: ServiceContainer wired")
    except Exception:  # pragma: no cover - defensive boot guard
        logger.exception("ServiceContainer wiring skipped")
        container = None

    app.state.container = container

    if container is not None:
        try:
            logger.info("Startup: initializing ServiceContainer", timeout=CONTAINER_STARTUP_TIMEOUT)
            await asyncio.wait_for(container.initialize(), timeout=CONTAINER_STARTUP_TIMEOUT)
            logger.info("Startup: ServiceContainer initialized")
        except TimeoutError:  # pragma: no cover - defensive boot guard
            logger.exception("ServiceContainer.initialize timed out; serving degraded (503)")
            app.state.container = None
            container = None
        except Exception:  # pragma: no cover - defensive boot guard
            # A half-initialised container (asyncpg pool never opened)
            # would route requests into broken repos and 500. Drop it so
            # di/providers.py serves the intended degraded 503 instead.
            logger.exception("ServiceContainer.initialize failed; serving degraded (503)")
            app.state.container = None
            container = None

    try:
        logger.info("Startup: priming max model token cache")
        await prime_max_model_tokens(settings)
        logger.info("Startup: max model token cache primed")
    except Exception:  # pragma: no cover - defensive cache guard
        logger.exception("max_model_tokens cache priming failed; falling back to config")

    # Mirror the resolved boot state into the process-level singleton so
    # callers without a request (Chainlit, background tasks) resolve the
    # same container the HTTP routes get via request.app.state. None on a
    # degraded boot keeps di.providers serving the intended 503.
    logger.info("Startup: registering process container", available=getattr(app.state, "container", None) is not None)
    set_container(getattr(app.state, "container", None))
    logger.info("Startup: complete")
    print_startup_banner(app_version)

    try:
        yield
    finally:
        live = getattr(app.state, "container", None)
        if live is not None:
            try:
                await live.shutdown()
            except Exception:  # pragma: no cover - defensive shutdown guard
                logger.exception("ServiceContainer.shutdown skipped")
        set_container(None)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(version=app_version, lifespan=lifespan)


def custom_openapi():
    """Build the OpenAPI schema with global bearer authentication metadata."""
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
# RateLimitMiddleware is registered BEFORE AuthMiddleware so it executes AFTER
# it (registration is reverse of execution), letting it key limits on the
# authenticated user id that AuthMiddleware just put on request.state.
app.add_middleware(RateLimitMiddleware)
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

# Credentials + a wildcard origin is unsafe (Starlette reflects the Origin with
# credentials), so drop any "*" from a misconfigured CORS_EXTRA_ORIGINS=*.
allow_origins = sanitize_cors_origins(allow_origins, allow_credentials=True)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Registered last so it wraps CORS and every other layer: this stamps the
# baseline security headers on all responses, including CORS preflights that
# CORSMiddleware short-circuits before they reach the inner stack.
app.add_middleware(SecurityHeadersMiddleware)


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


@app.get("/config", summary="Get current configuration", tags=["Configuration"], dependencies=[Depends(require_admin)])
def get_config():
    """Return the loaded application settings for admins, plus runtime auth flags
    the admin UI needs. ``super_admin_mode`` governs whether an admin bypasses
    partition-membership checks; the UI permission layer mirrors the backend rule
    instead of assuming every admin has partition access.
    """
    from api.dependencies.auth import SUPER_ADMIN_MODE
    from fastapi.encoders import jsonable_encoder

    return {**jsonable_encoder(settings), "super_admin_mode": SUPER_ADMIN_MODE}


# Router mounts. Phase 10F finished moving these into
# ``api/routers/{user,admin,auth}/``; the legacy ``openrag/routers/*``
# modules are now strangler-fig shims that re-export from the new
# locations. Prefixes / tags stay identical so the OpenAPI schema and
# client SDKs do not break across the move.
app.include_router(health_router, tags=[Tags.MONITORING])
app.include_router(indexer_router, prefix="/indexer", tags=[Tags.INDEXER])
app.include_router(extract_router, prefix="/extract", tags=[Tags.EXTRACT])
# Authorized, partition-checked source-file download (served under /static).
app.include_router(download_router, tags=[Tags.EXTRACT])
app.include_router(search_router, prefix="/search", tags=[Tags.SEARCH])
app.include_router(partition_router, prefix="/partition", tags=[Tags.PARTITION])
app.include_router(model_endpoints_router, prefix="/model-endpoints", tags=[Tags.MODEL_ENDPOINTS])
app.include_router(presets_router, prefix="/presets", tags=[Tags.PRESETS])
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
    from api.chainlit_assets import mount_chainlit_root_assets
    from chainlit.utils import mount_chainlit

    mount_chainlit(app, "./app_front.py", path="/chainlit")

    # Also serve Chainlit's bundled pdf.js worker at the origin root so source
    # PDF previews load (see mount_chainlit_root_assets for the full rationale).
    mount_chainlit_root_assets(app)


if __name__ == "__main__":
    if settings.ray.serve.enable:
        from ray import serve

        # @serve.ingress cloudpickles `app` to ship it to replica processes.
        # loguru's file sink isn't picklable (an open file handle, and with
        # enqueue=True a multiprocessing.SimpleQueue that errors with "SimpleQueue
        # objects should only be shared between processes through inheritance"),
        # and the app graph (lifespan, exception handlers) captures the
        # module-global logger by value. Strip the sinks before binding so the
        # captured logger is handler-less (picklable), then restore them for this
        # driver process below. Replica processes re-add their own sinks via the
        # module-level get_logger() that runs on import — matching loguru's
        # multiprocessing model, where handlers are per-process and never travel
        # through a pickle.
        logger.remove()

        @serve.deployment(num_replicas=settings.ray.serve.num_replicas)
        @serve.ingress(app)
        class OpenRagAPI:
            """Ray Serve deployment wrapper for the FastAPI app."""

        get_logger()  # restore this driver's logging now that `app` is serialized

        serve.start(http_options={"host": settings.ray.serve.host, "port": settings.ray.serve.port})
        if WITH_CHAINLIT_UI:
            from chainlit_api import app as chainlit_app

            serve.run(OpenRagAPI.bind(), route_prefix="/")
            uvicorn.run(chainlit_app, host="0.0.0.0", port=settings.ray.serve.chainlit_port)
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
