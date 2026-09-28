"""Public liveness, dependency readiness, and version endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()
#: Checks that gate readiness. The default embedder is one: without it uploads
#: fail and retrieval returns nothing or the wrong vectors, so a pod reporting
#: ready would serve broken answers (#1099). Other model kinds (LLM, VLM, STT,
#: per-partition endpoints) stay report-only, so an optional model's outage does
#: not take the API out of service. A check absent from the snapshot (model
#: discovery not configured) does not gate.
_CORE_READINESS_CHECKS = frozenset({"postgres", "milvus", "ray", "embedder"})
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
