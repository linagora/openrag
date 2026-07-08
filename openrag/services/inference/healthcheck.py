"""Probe inference endpoints for readiness.

Used at container startup (fail-fast) and by the ``/health_check`` route.
Uses raw ``httpx`` — no OpenAI SDK dependency.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

import httpx
from core.utils.logging import get_logger

logger = get_logger()


class EndpointStatus(str, Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNREACHABLE = "unreachable"


@dataclass
class HealthResult:
    url: str
    status: EndpointStatus
    latency_ms: float = 0.0
    models: list[str] = field(default_factory=list)
    http_status: int | None = None
    error: str | None = None


async def check_endpoint_health(endpoint: str, *, timeout: float = 5.0) -> HealthResult:
    """Probe a vLLM / OpenAI-compatible server via ``GET /v1/models``."""
    url = endpoint.rstrip("/")
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{url}/v1/models")
        latency = (time.monotonic() - start) * 1000
        if resp.status_code == 200:
            models = [m["id"] for m in resp.json().get("data", [])]
            return HealthResult(url=url, status=EndpointStatus.HEALTHY, latency_ms=latency, models=models)
        return HealthResult(url=url, status=EndpointStatus.UNHEALTHY, latency_ms=latency, http_status=resp.status_code)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        latency = (time.monotonic() - start) * 1000
        return HealthResult(url=url, status=EndpointStatus.UNREACHABLE, latency_ms=latency, error=str(exc))
    except Exception as exc:
        latency = (time.monotonic() - start) * 1000
        logger.warning("Unexpected error probing endpoint", url=url, error=str(exc))
        return HealthResult(url=url, status=EndpointStatus.UNREACHABLE, latency_ms=latency, error=str(exc))


async def check_infinity(endpoint: str, *, timeout: float = 5.0) -> HealthResult:
    """Probe an Infinity reranker server via ``GET /health``."""
    url = endpoint.rstrip("/")
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{url}/health")
        latency = (time.monotonic() - start) * 1000
        if resp.status_code == 200:
            return HealthResult(url=url, status=EndpointStatus.HEALTHY, latency_ms=latency)
        return HealthResult(url=url, status=EndpointStatus.UNHEALTHY, latency_ms=latency, http_status=resp.status_code)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        latency = (time.monotonic() - start) * 1000
        return HealthResult(url=url, status=EndpointStatus.UNREACHABLE, latency_ms=latency, error=str(exc))
    except Exception as exc:
        latency = (time.monotonic() - start) * 1000
        logger.warning("Unexpected error probing infinity endpoint", url=url, error=str(exc))
        return HealthResult(url=url, status=EndpointStatus.UNREACHABLE, latency_ms=latency, error=str(exc))


async def check_model_available(endpoint: str, model: str, *, timeout: float = 5.0) -> HealthResult:
    """Probe an OpenAI-compatible endpoint and verify a specific model is served."""
    result = await check_endpoint_health(endpoint, timeout=timeout)
    if result.status != EndpointStatus.HEALTHY:
        return result
    if model not in result.models:
        available = ", ".join(result.models) if result.models else "(none)"
        return HealthResult(
            url=result.url,
            status=EndpointStatus.UNHEALTHY,
            latency_ms=result.latency_ms,
            models=result.models,
            error=f"Model '{model}' not found. Available: {available}",
        )
    return result
