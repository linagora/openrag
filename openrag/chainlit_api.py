"""Standalone Chainlit ASGI app (Ray Serve mode).

When ``ray.serve`` is enabled, ``api.main`` runs the API as a Ray Serve
deployment and this app in a *separate* uvicorn process on its own port
(``ray.serve.chainlit_port``). Because it is a distinct process, it cannot
borrow the API replicas' asyncpg pool — it needs its own
:class:`ServiceContainer`, initialized in a lifespan exactly like
``api.main`` does.

Without that initialization the OIDC ``AuthMiddleware`` session lookup
(and the ``/static`` download route) reach an un-opened pool and raise
``ConnectionManager.initialize() has not been called`` — a 500 on every
``/chainlit/`` HTML load once a user holds an ``openrag_session`` cookie.
In token mode the ``/chainlit`` bypass never touches the DB, which is why
the crash only surfaced under ``AUTH_MODE=oidc``.
"""

from contextlib import asynccontextmanager

from api.chainlit_assets import mount_chainlit_root_assets
from api.middleware.auth import AuthMiddleware
from api.middleware.request_id import RequestIdMiddleware
from api.middleware.security_headers import SecurityHeadersMiddleware
from api.routers.user.download import router as download_router
from chainlit.utils import mount_chainlit
from core.config import load_config
from core.utils.logging import get_logger
from di.container import ServiceContainer
from di.providers import set_container
from fastapi import FastAPI

logger = get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Own this process's ServiceContainer lifecycle.

    Mirrors ``api.main.lifespan``'s container handling (minus the Ray /
    worker bootstrap, which the API process owns): build the container and
    open its asyncpg pool best-effort, so a Postgres/Milvus hiccup degrades
    to 503 rather than blocking boot. ``container.initialize()`` is safe to
    run here even though the API replicas already ran it — migrations and
    seeds are idempotent, and Serve only starts this process after the API
    deployment is healthy.
    """
    container: ServiceContainer | None
    try:
        container = ServiceContainer(load_config())
        await container.initialize()
    except Exception:
        logger.exception("Chainlit ServiceContainer init failed; serving degraded (503)")
        container = None
    app.state.container = container
    set_container(container)
    try:
        yield
    finally:
        if container is not None:
            try:
                await container.shutdown()
            except Exception:  # pragma: no cover - defensive shutdown guard
                logger.exception("Chainlit ServiceContainer shutdown skipped")
        set_container(None)


def _get_auth_service(request):
    container = getattr(request.app.state, "container", None)
    if container is None:
        # Degraded boot (container init failed). Surface as RuntimeError so
        # AuthMiddleware maps it to a 503 on guarded routes; the /chainlit
        # bypass treats the failed lookup as an invalid session and redirects
        # to /auth/login instead of 500-ing.
        raise RuntimeError("ServiceContainer is not available")
    return container.auth_service


app = FastAPI(lifespan=lifespan)
app.state.container = None
app.add_middleware(
    AuthMiddleware,
    get_auth_service=_get_auth_service,
)
# AuthMiddleware's ``/static`` branch authenticates via the ``?token=`` query
# param, but it reads it from ``request.state.original_token`` — which only
# RequestIdMiddleware populates (it stashes the raw token there before redacting
# the query string for logs). The mounted deployment (``api.main``) registers
# this; without it here, source-file downloads on this standalone origin see no
# token and 403 with "Missing token" (the Ray Serve source-preview failure).
# Registered after AuthMiddleware so it wraps it — i.e. runs first and sets
# ``original_token`` before auth reads it (add_middleware adds outermost-last).
app.add_middleware(RequestIdMiddleware)
# Ray Serve mode serves this Chainlit app on its own port, so it needs the same
# baseline security headers the mounted deployment gets from api.main. Added
# last → outermost, covering every response on this origin.
app.add_middleware(SecurityHeadersMiddleware)

# Ray Serve mode runs the API and Chainlit on separate ports. Source previews
# rewrite their file download links to the browser origin (the Chainlit host),
# so this standalone Chainlit app must also expose the authorized,
# partition-checked source-download route (/static/{extract_id}) or those
# links would 404. In the mounted (/chainlit) deployment the UI shares the
# API's origin, which already serves this route.
app.include_router(download_router)

mount_chainlit(app=app, target="./app_front.py", path="/chainlit")

# Ray Serve mode serves this standalone Chainlit app on its own origin, so it
# must also expose Chainlit's root-absolute pdf.js worker at /assets — the same
# separate-origin reason the /static download route is replicated above.
mount_chainlit_root_assets(app)
