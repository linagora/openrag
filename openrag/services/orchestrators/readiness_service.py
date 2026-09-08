"""Short, cached dependency probes for traffic readiness."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import httpx
from core.config.model_endpoints import ModelEndpointConfig


class ReadinessService:
    def __init__(
        self, checks: dict[str, Callable[[], Awaitable[None]]], *, timeout: float = 2.0, cache_ttl: float = 2.0
    ) -> None:
        self._checks = checks
        self._timeout = timeout
        self._cache_ttl = cache_ttl
        self._expires_at = 0.0
        self._result: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def check(self) -> dict[str, str]:
        async with self._lock:
            if time.monotonic() >= self._expires_at:
                results = await asyncio.gather(*(self._probe(check) for check in self._checks.values()))
                self._result = dict(zip(self._checks, results, strict=True))
                self._expires_at = time.monotonic() + self._cache_ttl
            return dict(self._result)

    async def _probe(self, check: Callable[[], Awaitable[None]]) -> str:
        try:
            await asyncio.wait_for(check(), timeout=self._timeout)
            return "ok"
        except TimeoutError:
            return "timeout"
        except Exception:
            # This endpoint is public: never return connection strings or keys.
            return "unavailable"


async def check_model_endpoint(config: ModelEndpointConfig) -> None:
    """Check availability without generating tokens or running embeddings."""
    base = config.endpoint.rstrip("/")
    implementation = config.extra.get("implementation", "vllm")
    if implementation == "ollama" and not base.endswith("/v1"):
        base += "/v1"
    health_only = implementation in {"infinity", "tei"}
    url = base + ("/health" if health_only else "/models")
    api_key = config.extra.get("api_key")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=2.0, headers=headers) as client:
        response = await client.get(url)
        response.raise_for_status()
    if not health_only:
        models = response.json()["data"]
        if not isinstance(models, list) or (config.model_name and config.model_name not in [m["id"] for m in models]):
            raise ValueError("Configured model is unavailable")
