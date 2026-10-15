"""Public liveness, dependency readiness, and version endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()
_CORE_READINESS_CHECKS = frozenset({"postgres", "milvus", "ray"})
#: With READINESS_REQUIRE_EMBEDDER=true the default embedder gates readiness
#: too: without it uploads fail and retrieval returns nothing or the wrong
#: vectors (#1099). Off by default, since every replica shares the embedder:
#: under Kubernetes its outage or restart would take all of them out of the
#: Service at once, admin API and UI included. Only on a verdict about the
#: embedder itself, not on a timeout: model probes share one short deadline with
#: discovery, so a slow round would pull every replica at once. Not when
#: discovery failed either: the embedder then carries discovery's status, which
#: says nothing about it. Other model kinds stay report-only.
_EMBEDDER_GATING_STATUSES = frozenset({"unavailable", "unresolvable"})
_PUBLIC_MODEL_ENDPOINT_CATEGORY = "configured"


@router.get("/health_check", summary="Health check endpoint for API", dependencies=[])
async def health_check(request: Request) -> str:
    return "RAG API is up."


@router.get("/ready", summary="Dependency readiness", dependencies=[])
async def ready(request: Request) -> JSONResponse:
    container = getattr(request.app.state, "container", None)
    if container is None or not container.is_initialized:
        return JSONResponse(
            {
                "status": "not_ready",
                "checks": {"startup": "unavailable"},
                "model_endpoints": [],
                "configuration_references": [],
            },
            status_code=503,
        )
    snapshot = await container.readiness_service.snapshot()
    checks = dict(snapshot.checks)
    required_checks = _CORE_READINESS_CHECKS.intersection(checks)
    is_ready = bool(required_checks) and all(checks[name] == "ok" for name in required_checks)
    if (
        container.readiness_service.requires_embedder
        and checks.get("model_endpoint_discovery") == "ok"
        and checks.get("embedder") in _EMBEDDER_GATING_STATUSES
    ):
        is_ready = False
    return JSONResponse(
        {
            "status": "ready" if is_ready else "not_ready",
            "checks": checks,
            "model_endpoints": [
                {"provider": _PUBLIC_MODEL_ENDPOINT_CATEGORY, "kind": endpoint.kind, "status": endpoint.status}
                for endpoint in snapshot.model_endpoints
            ],
            "configuration_references": [
                {"kind": finding.kind, "count": finding.count, "status": finding.status}
                for finding in snapshot.configuration_references
            ],
        },
        status_code=200 if is_ready else 503,
    )


@router.get("/version", summary="Get openRAG version", dependencies=[])
def get_version(request: Request) -> dict[str, str]:
    return {"version": request.app.version}
