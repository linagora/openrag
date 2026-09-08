"""Short, cached dependency probes for traffic readiness."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import httpx
from core.config.model_endpoints import DEFAULT_MODEL_IMPLEMENTATIONS, ModelEndpointConfig


class ModelEndpointProbeError(RuntimeError):
    """A model endpoint answered with an unsuccessful HTTP status."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"Model endpoint returned HTTP {status_code}")
        self.status_code = status_code


class ModelNotFoundError(ValueError):
    """A reachable endpoint does not serve the configured model."""

    def __init__(self, model_ids: list[str]) -> None:
        super().__init__("Configured model is unavailable")
        self.model_ids = model_ids


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
        except (TimeoutError, httpx.TimeoutException):
            return "timeout"
        except Exception:
            # This endpoint is public: never return connection strings or keys.
            return "unavailable"


def model_implementation(config: ModelEndpointConfig, model_type: str | None = None) -> str:
    """Resolve the configured provider, matching the DI factory defaults."""
    configured = config.extra.get("implementation")
    if isinstance(configured, str) and configured:
        return configured
    return DEFAULT_MODEL_IMPLEMENTATIONS.get(model_type or "", "vllm")


async def check_model_endpoint(
    config: ModelEndpointConfig,
    *,
    model_type: str | None = None,
    timeout: float = 2.0,
) -> list[str] | None:
    """Check availability without generating tokens or running embeddings."""
    base = config.endpoint.rstrip("/")
    implementation = model_implementation(config, model_type)
    if implementation == "ollama" and not base.endswith("/v1"):
        base += "/v1"
    health_only = implementation in {"infinity", "tei"}
    url = base + ("/health" if health_only else "/models")
    api_key = config.extra.get("api_key")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=False) as client:
        response = await client.get(url)
        if hasattr(response, "raise_for_status"):
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ModelEndpointProbeError(exc.response.status_code) from exc
        elif response.status_code >= 400:
            raise ModelEndpointProbeError(response.status_code)
    if not health_only:
        payload = response.json()
        models = payload.get("data") if isinstance(payload, dict) else None
        model_ids = (
            [item["id"] for item in models if isinstance(item, dict) and isinstance(item.get("id"), str)]
            if isinstance(models, list)
            else []
        )
        if not isinstance(models, list) or (config.model_name and config.model_name not in model_ids):
            if not isinstance(models, list):
                raise ValueError("Configured model is unavailable")
            raise ModelNotFoundError(model_ids)
        return model_ids
    return None
