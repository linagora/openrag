"""Public liveness, dependency readiness, and version endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/health_check", summary="Health check endpoint for API", dependencies=[])
async def health_check(request: Request) -> str:
    return "RAG API is up."


@router.get("/ready", summary="Dependency readiness", dependencies=[])
async def ready(request: Request) -> JSONResponse:
    container = getattr(request.app.state, "container", None)
    if container is None or not container.is_initialized:
        return JSONResponse({"status": "not_ready", "checks": {"startup": "unavailable"}}, status_code=503)
    checks = await container.readiness_service.check()
    is_ready = all(value == "ok" for value in checks.values())
    return JSONResponse(
        {"status": "ready" if is_ready else "not_ready", "checks": checks},
        status_code=200 if is_ready else 503,
    )


@router.get("/version", summary="Get openRAG version", dependencies=[])
def get_version(request: Request) -> dict[str, str]:
    return {"version": request.app.version}
