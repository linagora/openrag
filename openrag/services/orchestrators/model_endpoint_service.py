"""ModelEndpointService — CRUD, env-driven seeding, and client-cache invalidation.

Orchestrates ModelEndpointRepository to maintain the named endpoint registry
in the DB and in the in-memory config (Settings.models). On first boot it
seeds one default endpoint per model type from existing env/config values
so the system works without any admin interaction.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from core.config.model_endpoints import ModelEndpointConfig, ModelEndpointRow
from core.utils.exceptions import NotFoundError, ValidationError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.ports.model_endpoint_repo import ModelEndpointRepository

logger = get_logger()

_VALID_TYPES = frozenset({"embedder", "reranker", "llm", "vlm"})


def _slug(model_name: str) -> str:
    """'owner/model-name' → 'model-name'; '' → 'default'."""
    return model_name.split("/")[-1] if model_name else "default"


def _with_api_key(extra: dict[str, Any], api_key: str | None) -> dict[str, Any]:
    """Add ``api_key`` to endpoint extras when configured."""
    if api_key:
        return {**extra, "api_key": api_key}
    return extra


def _with_enable_thinking(extra: dict[str, Any], enable_thinking: bool | None) -> dict[str, Any]:
    """Add chat-template thinking control only when explicitly configured."""
    if enable_thinking is None:
        return extra
    return {**extra, "enable_thinking": enable_thinking}


class ModelEndpointService:
    """CRUD and lifecycle management for named model endpoints."""

    def __init__(
        self,
        *,
        model_endpoint_repo: ModelEndpointRepository,
        config: Settings,
        partition_service: Any = None,
        client_caches: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._repo = model_endpoint_repo
        self._config = config
        self._partition_service = partition_service
        self._client_caches: dict[str, dict[str, Any]] = client_caches or {}

    # ------------------------------------------------------------------
    # Startup lifecycle
    # ------------------------------------------------------------------

    async def seed_defaults(self) -> None:
        """Insert one default endpoint per type if the DB is empty for that type.

        Seeds are derived from existing Settings / env-var values so that
        existing deployments continue working after the Phase 14 upgrade
        without any admin intervention.
        """
        seeds = self._build_default_seeds()
        now = datetime.now(UTC)
        for model_type, data in seeds.items():
            existing = await self._repo.list_all(model_type=model_type)
            if existing:
                continue
            endpoint: str = data["endpoint"]
            model_name: str = data["model_name"]
            if not endpoint:
                logger.info(f"No {model_type} endpoint configured — skipping seed.")
                continue
            row = ModelEndpointRow(
                name=_slug(model_name or ""),
                model_type=model_type,
                endpoint=endpoint,
                model_name=model_name or None,
                batch_size=data.get("batch_size", 32),
                timeout=data.get("timeout", 30.0),
                extra=data.get("extra", {}),
                is_default=True,
                created_at=now,
                updated_at=now,
            )
            await self._repo.create(row)
            logger.info(f"Seeded default {model_type} endpoint '{row.name}'.")

    def _build_default_seeds(self) -> dict[str, dict[str, Any]]:
        """Build seed data from env overrides + existing Settings fallbacks.

        The ``*_ENDPOINT`` / ``*_MODEL`` env vars below are seed-specific names
        the config loader does NOT map onto ``Settings``, so they are read here
        directly. The api-key env vars (``API_KEY``, ``EMBEDDER_API_KEY``, ...)
        ARE mapped by the loader (loader.py), so ``s.<type>.api_key`` already
        reflects any env override — reading them via ``os.getenv`` again would
        be redundant double-handling (and non-deterministic when a local .env is
        loaded into the process via ``load_dotenv``).
        """
        s = self._config
        return {
            "embedder": {
                "endpoint": os.getenv("EMBEDDER_ENDPOINT", s.embedder.base_url),
                "model_name": os.getenv("EMBEDDING_MODEL", s.embedder.model_name),
                "batch_size": s.embedder.batch_size,
                "timeout": s.embedder.timeout,
                "extra": _with_api_key({"implementation": "vllm"}, s.embedder.api_key),
            },
            "llm": {
                "endpoint": os.getenv("LLM_ENDPOINT", s.llm.base_url),
                "model_name": os.getenv("LLM_MODEL", s.llm.model),
                "timeout": s.llm.timeout,
                "extra": _with_enable_thinking(
                    _with_api_key({"implementation": "vllm"}, s.llm.api_key),
                    s.llm.enable_thinking,
                ),
            },
            "vlm": {
                "endpoint": os.getenv("VLM_ENDPOINT", s.vlm.base_url),
                "model_name": os.getenv("VLM_MODEL", s.vlm.model),
                "timeout": s.vlm.timeout,
                "extra": _with_enable_thinking(
                    _with_api_key({"implementation": "vllm"}, s.vlm.api_key),
                    s.vlm.enable_thinking,
                ),
            },
            "reranker": {
                # Catalog the reranker endpoint whenever it is configured, like
                # the embedder — registration is about availability, not whether
                # reranking is on. Activation is the retrieval preset's
                # enable_reranker kill-switch, which inherits reranker.enabled,
                # so a disabled reranker is seeded but unused by default and
                # remains available for per-partition opt-in.
                "endpoint": os.getenv("RERANKER_ENDPOINT", s.reranker.base_url),
                "model_name": os.getenv("RERANKER_MODEL", s.reranker.model_name),
                "timeout": s.reranker.timeout,
                "extra": _with_api_key({"implementation": s.reranker.provider}, s.reranker.api_key),
            },
        }

    async def load_all(self) -> None:
        """Fetch all endpoints from DB, rebuild config.models dicts atomically.

        Adds a virtual 'default' alias pointing to the is_default=True row for
        each model type so component factories can resolve 'default' without
        knowing the actual endpoint name.
        """
        rows = await self._repo.list_all()
        buckets: dict[str, dict[str, ModelEndpointConfig]] = {t: {} for t in _VALID_TYPES}
        default_cfgs: dict[str, ModelEndpointConfig] = {}

        for row in rows:
            bucket = buckets.get(row.model_type)
            if bucket is None:
                continue
            cfg = ModelEndpointConfig(
                endpoint=row.endpoint,
                model_name=row.model_name,
                batch_size=row.batch_size,
                timeout=row.timeout,
                extra=row.extra,
            )
            bucket[row.name] = cfg
            if row.is_default:
                default_cfgs[row.model_type] = cfg

        for model_type, default_cfg in default_cfgs.items():
            buckets[model_type]["default"] = default_cfg

        models = self._config.models
        for attr in ("embedder", "reranker", "llm", "vlm"):
            target: dict = getattr(models, attr)
            target.clear()
            target.update(buckets[attr])

        logger.info(
            "Loaded model endpoints.",
            n_embedder=len(buckets["embedder"]),
            n_llm=len(buckets["llm"]),
            n_reranker=len(buckets["reranker"]),
            n_vlm=len(buckets["vlm"]),
        )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create_model_endpoint(self, row: ModelEndpointRow) -> ModelEndpointRow:
        """Register a new endpoint; raises 409 if (name, model_type) already exists."""
        if row.model_type not in _VALID_TYPES:
            raise ValidationError(f"Invalid model_type '{row.model_type}'. Must be one of: {sorted(_VALID_TYPES)}")
        existing = await self._repo.get(row.name, row.model_type)
        if existing is not None:
            raise ValidationError(
                f"Endpoint '{row.name}' of type '{row.model_type}' already exists.",
                status_code=409,
                code="ENDPOINT_EXISTS",
            )
        result = await self._repo.create(row)
        await self.load_all()
        return result

    async def get_model_endpoint(self, name: str, model_type: str) -> ModelEndpointRow:
        """Fetch one endpoint row; raises 404 if not found."""
        row = await self._repo.get(name, model_type)
        if row is None:
            raise NotFoundError(f"Endpoint '{name}' of type '{model_type}' not found.")
        return row

    async def list_model_endpoints(self, model_type: str | None = None) -> list[ModelEndpointRow]:
        return await self._repo.list_all(model_type=model_type)

    async def update_model_endpoint(self, name: str, model_type: str, **fields: object) -> ModelEndpointRow:
        """Update endpoint fields and/or rename it.

        Pass ``new_name=`` to rename. After any change the in-memory config is
        reloaded and the stale cached client instance is evicted so the next
        request builds a fresh client against the updated config.
        """
        existing = await self._repo.get(name, model_type)
        if existing is None:
            raise NotFoundError(f"Endpoint '{name}' of type '{model_type}' not found.")

        new_name: str | None = fields.pop("new_name", None)  # type: ignore[assignment]

        if fields:
            updated = await self._repo.update(name, model_type, **fields)
        else:
            updated = existing

        effective_name = name
        if new_name and new_name != name:
            await self._repo.rename(name, model_type, new_name)
            effective_name = new_name
            self._invalidate_client_cache(model_type, name)

        self._invalidate_client_cache(model_type, effective_name)
        if existing.is_default:
            # The 'default' alias resolves to this endpoint, so a client built under
            # the 'default' cache key is now stale too. Evict it alongside the named
            # entry (set_default already does this; update must match).
            self._invalidate_client_cache(model_type, "default")
        await self.load_all()
        return await self._repo.get(effective_name, model_type) or (updated or existing)

    async def delete_model_endpoint(self, name: str, model_type: str) -> None:
        """Delete an endpoint.

        Raises 404 if not found, 422 if it is the last endpoint of its type
        (would leave components with no fallback).
        """
        existing = await self._repo.get(name, model_type)
        if existing is None:
            raise NotFoundError(f"Endpoint '{name}' of type '{model_type}' not found.")

        all_of_type = await self._repo.list_all(model_type=model_type)
        if len(all_of_type) <= 1:
            raise ValidationError(f"Cannot delete the last '{model_type}' endpoint. Register a replacement first.")

        await self._repo.delete(name, model_type)
        self._invalidate_client_cache(model_type, name)
        if existing.is_default:
            # load_all only adds the 'default' alias for an is_default row, so
            # deleting the default would drop it entirely and make factory("default")
            # raise. Promote a surviving endpoint (deterministic: first by name) so
            # 'default' keeps resolving, and evict its now-stale cached client.
            survivor = next((e for e in sorted(all_of_type, key=lambda e: e.name) if e.name != name), None)
            if survivor is not None:
                await self._repo.set_default(model_type, survivor.name)
            self._invalidate_client_cache(model_type, "default")
        await self.load_all()

    async def set_default(self, model_type: str, name: str) -> None:
        """Promote ``name`` to the default endpoint for ``model_type``."""
        existing = await self._repo.get(name, model_type)
        if existing is None:
            raise NotFoundError(f"Endpoint '{name}' of type '{model_type}' not found.")
        await self._repo.set_default(model_type, name)
        self._invalidate_client_cache(model_type, "default")
        await self.load_all()

    # ------------------------------------------------------------------
    # Endpoint validation
    # ------------------------------------------------------------------

    async def validate_endpoint(
        self,
        url: str,
        model_name: str | None = None,
        *,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """Probe ``{url}/models`` to verify the endpoint is reachable and serving.

        Returns a dict with ``reachable``, ``model_found``, ``models_served``,
        and ``detail`` keys — suitable as a ``ValidateEndpointResponse`` payload.
        """
        import httpx  # local import to avoid hard dep in tests that mock it

        result: dict[str, Any] = {
            "reachable": False,
            "model_found": None,
            "models_served": None,
            "detail": None,
        }
        models_url = url.rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            async with httpx.AsyncClient(timeout=5.0, headers=headers) as client:
                resp = await client.get(models_url)
            result["reachable"] = True
            if resp.status_code == 200:
                data = resp.json()
                served = [m["id"] for m in data.get("data", []) if "id" in m]
                result["models_served"] = served
                if model_name is not None:
                    result["model_found"] = model_name in served
            else:
                result["detail"] = f"HTTP {resp.status_code}"
        except httpx.ConnectError as exc:
            result["detail"] = f"Connection error: {exc}"
        except httpx.TimeoutException:
            result["detail"] = "Connection timed out"
        except Exception as exc:  # noqa: BLE001
            result["detail"] = str(exc)
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _invalidate_client_cache(self, model_type: str, name: str) -> None:
        """Evict ``name`` from the component-factory cache for ``model_type``."""
        cache = self._client_caches.get(model_type)
        if cache is not None:
            cache.pop(name, None)


__all__ = ["ModelEndpointService"]
