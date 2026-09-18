"""Short, cached dependency probes for traffic readiness."""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from core.config.model_endpoints import (
    DEFAULT_MODEL_IMPLEMENTATIONS,
    ModelEndpointConfig,
    ModelEndpointType,
    is_placeholder_api_key,
)
from core.models.readiness import (
    ConfigurationReferenceReadiness,
    ModelEndpointDiscovery,
    ModelEndpointReadiness,
    ModelEndpointTarget,
    ReadinessSnapshot,
    ReadinessStatus,
)
from core.utils.logging import get_logger

logger = get_logger()


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


class ModelListUnavailableError(ValueError):
    """A reachable endpoint did not return a usable model list."""

    def __init__(self) -> None:
        super().__init__("Endpoint returned an invalid model list.")


class InsecureModelEndpointError(ValueError):
    """A model probe would send credentials over cleartext transport."""

    def __init__(self) -> None:
        super().__init__("Model endpoints with API keys must use HTTPS.")


@dataclass(frozen=True, slots=True)
class _ModelProbeRequest:
    url: str
    authorization: str | None
    health_only: bool


def _canonical_probe_url(url: str) -> str:
    try:
        canonical = httpx.URL(url)
    except httpx.InvalidURL:
        return url
    if canonical.port == {"http": 80, "https": 443}.get(canonical.scheme):
        canonical = canonical.copy_with(port=None)
    return str(canonical)


class ReadinessService:
    def __init__(
        self,
        checks: dict[str, Callable[[], Awaitable[None]]],
        *,
        discover_model_endpoints: Callable[[], Awaitable[ModelEndpointDiscovery]] | None = None,
        summary_model_kinds: tuple[ModelEndpointType, ...] = (),
        publish: Callable[[ReadinessSnapshot], None] | None = None,
        timeout: float = 2.0,
        cache_ttl: float = 2.0,
    ) -> None:
        self._checks = checks
        self._discover_model_endpoints = discover_model_endpoints
        self._summary_model_kinds = summary_model_kinds
        self._publish = publish
        self._timeout = timeout
        self._cache_ttl = cache_ttl
        self._expires_at = 0.0
        self._snapshot = ReadinessSnapshot(checks={})
        self._lock = asyncio.Lock()

    async def snapshot(self) -> ReadinessSnapshot:
        async with self._lock:
            if time.monotonic() >= self._expires_at:
                snapshot = await self._refresh()
                self._snapshot = snapshot
                self._expires_at = time.monotonic() + self._cache_ttl
                if self._publish is not None:
                    try:
                        self._publish(snapshot)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Failed to publish readiness metrics")
            return self._snapshot

    async def check(self) -> dict[str, ReadinessStatus]:
        return dict((await self.snapshot()).checks)

    async def _refresh(self) -> ReadinessSnapshot:
        if self._discover_model_endpoints is None:
            return ReadinessSnapshot(checks=await self._run_core_checks())

        checks, discovery_result = await asyncio.gather(self._run_core_checks(), self._discover_and_probe())
        discovery_status, model_endpoints, configuration_references, summary_statuses = discovery_result
        for kind in self._summary_model_kinds:
            checks[kind] = summary_statuses.get(kind, discovery_status)
        checks["model_endpoint_discovery"] = discovery_status
        return ReadinessSnapshot(
            checks=checks,
            model_endpoints=model_endpoints,
            configuration_references=configuration_references,
        )

    async def _run_core_checks(self) -> dict[str, ReadinessStatus]:
        results = await asyncio.gather(*(self._probe(check) for check in self._checks.values()))
        return dict(zip(self._checks, results, strict=True))

    async def _discover_and_probe(
        self,
    ) -> tuple[
        ReadinessStatus,
        tuple[ModelEndpointReadiness, ...],
        tuple[ConfigurationReferenceReadiness, ...],
        dict[ModelEndpointType, ReadinessStatus],
    ]:
        assert self._discover_model_endpoints is not None
        deadline = time.monotonic() + self._timeout
        try:
            discovery = await asyncio.wait_for(self._discover_model_endpoints(), timeout=self._timeout)
        except (TimeoutError, httpx.TimeoutException):
            return "timeout", (), (), {}
        except Exception:
            return "unavailable", (), (), {}

        remaining_timeout = max(0.0, deadline - time.monotonic())
        model_endpoints = list(await self._probe_model_endpoints(discovery.targets, remaining_timeout))
        statuses = {(item.kind, item.provider): item.status for item in model_endpoints}
        summary_statuses: dict[ModelEndpointType, ReadinessStatus] = {}
        for kind in self._summary_model_kinds:
            default = next((target for target in discovery.targets if target.kind == kind and target.is_default), None)
            if default is not None:
                summary_statuses[kind] = statuses[(kind, default.provider)]
                continue

            default_key = (kind, "default")
            if default_key not in statuses:
                model_endpoints.append(ModelEndpointReadiness(provider="default", kind=kind, status="unresolvable"))
                statuses[default_key] = "unresolvable"
            summary_statuses[kind] = statuses[default_key]

        finding_counts = Counter(finding.kind for finding in discovery.configuration_references)
        configuration_references = tuple(
            ConfigurationReferenceReadiness(kind=kind, count=count) for kind, count in sorted(finding_counts.items())
        )
        return (
            "ok",
            tuple(sorted(model_endpoints, key=lambda endpoint: (endpoint.kind, endpoint.provider))),
            configuration_references,
            summary_statuses,
        )

    async def _probe_model_endpoints(
        self, targets: tuple[ModelEndpointTarget, ...], timeout: float
    ) -> tuple[ModelEndpointReadiness, ...]:
        readiness = [
            ModelEndpointReadiness(provider=target.provider, kind=target.kind, status="unresolvable")
            for target in targets
            if target.config is None
        ]
        groups: dict[tuple[str, str | None], tuple[_ModelProbeRequest, list[ModelEndpointTarget]]] = {}
        for target in targets:
            if target.config is None:
                continue
            try:
                request = _model_probe_request(target.config, target.kind)
            except InsecureModelEndpointError:
                readiness.append(
                    ModelEndpointReadiness(provider=target.provider, kind=target.kind, status="unavailable")
                )
                continue
            key = (request.url, request.authorization)
            if key not in groups:
                groups[key] = (request, [])
            groups[key][1].append(target)

        grouped_results = await asyncio.gather(
            *(
                self._probe_model_group(request, grouped_targets, timeout)
                for request, grouped_targets in groups.values()
            )
        )
        for group in grouped_results:
            readiness.extend(group)
        return tuple(readiness)

    async def _probe_model_group(
        self, request: _ModelProbeRequest, targets: list[ModelEndpointTarget], timeout: float
    ) -> tuple[ModelEndpointReadiness, ...]:
        if timeout <= 0:
            return tuple(
                ModelEndpointReadiness(provider=target.provider, kind=target.kind, status="timeout")
                for target in targets
            )
        try:
            model_ids = await asyncio.wait_for(_request_model_probe(request, timeout), timeout=timeout)
        except (TimeoutError, httpx.TimeoutException):
            status: ReadinessStatus = "timeout"
        except Exception:
            status = "unavailable"
        else:
            results = []
            for target in targets:
                assert target.config is not None
                try:
                    _validate_model(target.config, model_ids)
                except ModelNotFoundError:
                    target_status: ReadinessStatus = "unavailable"
                else:
                    target_status = "ok"
                results.append(ModelEndpointReadiness(provider=target.provider, kind=target.kind, status=target_status))
            return tuple(results)

        return tuple(
            ModelEndpointReadiness(provider=target.provider, kind=target.kind, status=status) for target in targets
        )

    async def _probe(self, check: Callable[[], Awaitable[None]]) -> ReadinessStatus:
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


def _model_probe_request(config: ModelEndpointConfig, model_type: str | None = None) -> _ModelProbeRequest:
    base = config.endpoint.rstrip("/")
    implementation = model_implementation(config, model_type)
    if implementation == "ollama" and not base.endswith("/v1"):
        base += "/v1"
    health_only = implementation in {"infinity", "tei"}
    configured_api_key = config.extra.get("api_key")
    api_key = None if is_placeholder_api_key(configured_api_key) else configured_api_key
    try:
        parsed_url = httpx.URL(base)
    except httpx.InvalidURL as exc:
        if api_key:
            raise InsecureModelEndpointError from exc
    else:
        has_credentials = bool(api_key or parsed_url.username or parsed_url.password)
        if has_credentials and parsed_url.scheme != "https":
            raise InsecureModelEndpointError
    authorization = f"Bearer {api_key}" if api_key else None
    return _ModelProbeRequest(
        url=_canonical_probe_url(base + ("/health" if health_only else "/models")),
        authorization=authorization,
        health_only=health_only,
    )


async def _request_model_probe(request: _ModelProbeRequest, timeout: float) -> list[str] | None:
    headers = {"Authorization": request.authorization} if request.authorization else {}
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=False) as client:
        response = await client.get(request.url)
        if hasattr(response, "raise_for_status"):
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ModelEndpointProbeError(exc.response.status_code) from exc
        elif response.status_code >= 400:
            raise ModelEndpointProbeError(response.status_code)

    if request.health_only:
        return None
    try:
        payload = response.json()
    except ValueError as exc:
        raise ModelListUnavailableError from exc
    models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise ModelListUnavailableError
    if not all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in models):
        raise ModelListUnavailableError
    return [item["id"] for item in models]


def _validate_model(config: ModelEndpointConfig, model_ids: list[str] | None) -> None:
    if model_ids is not None and config.model_name and config.model_name not in model_ids:
        raise ModelNotFoundError(model_ids)


async def check_model_endpoint(
    config: ModelEndpointConfig,
    *,
    model_type: str | None = None,
    timeout: float = 2.0,
) -> list[str] | None:
    """Check availability without generating tokens or running embeddings."""
    model_ids = await _request_model_probe(_model_probe_request(config, model_type), timeout)
    _validate_model(config, model_ids)
    return model_ids
