"""ModelEndpointService — CRUD, env-driven seeding, and client-cache invalidation.

Orchestrates ModelEndpointRepository to maintain the named endpoint registry
in the DB and in the in-memory config (Settings.models). On first boot it
seeds one default endpoint per model type from existing env/config values
so the system works without any admin interaction.
"""

from __future__ import annotations

import io
import os
import wave
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from core.config.model_endpoints import (
    ENV_MANAGED_KEY,
    ENV_MANAGED_VALUE,
    STT_LANGUAGE_KEY,
    STT_REQUEST_CONTROL_EXTRA_KEYS,
    ModelEndpointConfig,
    ModelEndpointRow,
)
from core.utils.exceptions import NotFoundError, ValidationError
from core.utils.logging import get_logger
from core.utils.redaction import preserve_existing_secrets

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.ports.model_endpoint_repo import ModelEndpointRepository

logger = get_logger()

_VALID_TYPES = frozenset({"embedder", "reranker", "llm", "vlm", "stt"})
_SAMPLING_TYPES = frozenset({"llm", "vlm"})
# `EMPTY` is the config's stand-in for "no key configured" (see endpoints.py), not
# a credential. Treating it as one would let boot-time sync overwrite a real
# hand-set key with a placeholder the moment sync_on_boot was switched on.
_PLACEHOLDER_API_KEYS = frozenset({"", "EMPTY"})
_STT_VALIDATION_TIMEOUT_SECONDS = 15.0
_STT_TEXT_RESPONSE_FORMATS = frozenset({"text", "srt", "vtt"})
# Which env var, if any, owns a given tunable per model type. `_build_default_seeds`
# always fills these from Settings, so their presence in the seed says nothing about
# whether the *environment* set them — without this table sync would write the config
# default over an admin's value (an llm row tuned to timeout=99 came back as 60).
# Absent from this table (e.g. llm timeout, which has no env var at all) means env
# does not own the field and sync must leave it alone.
_ENV_OWNED_TUNABLES: dict[str, dict[str, str]] = {
    "embedder": {"batch_size": "EMBEDDER_BATCH_SIZE", "timeout": "EMBEDDER_TIMEOUT"},
    "vlm": {"timeout": "VLM_TIMEOUT"},
    "reranker": {"timeout": "RERANKER_TIMEOUT"},
    "stt": {
        "batch_size": "TRANSCRIBER_MAX_CONCURRENT_CHUNKS",
        "timeout": "TRANSCRIBER_TIMEOUT",
    },
    "llm": {},
}


def _build_stt_validation_wav() -> bytes:
    """Build a portable one-second WAV sample for STT endpoint validation."""
    sample_rate = 16_000
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * sample_rate)
    return buffer.getvalue()


# Created once rather than encoding/transcoding a file for each Admin UI validation.
_STT_VALIDATION_WAV = _build_stt_validation_wav()


def _flatten_multipart_field(key: str, value: Any) -> list[tuple[str, str]]:
    """Serialize JSON-like values as OpenAI-style multipart form fields."""
    if isinstance(value, Mapping):
        return [
            item
            for nested_key, nested_value in value.items()
            for item in _flatten_multipart_field(f"{key}[{nested_key}]", nested_value)
        ]
    if isinstance(value, (list, tuple)):
        return [item for nested_value in value for item in _flatten_multipart_field(f"{key}[]", nested_value)]
    if value is True:
        serialized = "true"
    elif value is False:
        serialized = "false"
    elif value is None:
        serialized = ""
    else:
        serialized = str(value)
    return [(key, serialized)] if serialized else []


def _stt_validation_form_data(
    model_name: str,
    extra: dict[str, Any] | None,
    prompt: str | None = None,
) -> dict[str, Any]:
    """Build the sanitized multipart fields used by an STT runtime request."""
    request_options: dict[str, Any] = {"model": model_name}
    if prompt and prompt.strip():
        request_options["prompt"] = prompt.strip()
    if extra:
        language = extra.get(STT_LANGUAGE_KEY)
        if isinstance(language, str) and language.strip():
            request_options[STT_LANGUAGE_KEY] = language.strip()
        request_options.update(
            {key: value for key, value in extra.items() if key not in STT_REQUEST_CONTROL_EXTRA_KEYS}
        )

    serialized: dict[str, Any] = {}
    for key, value in request_options.items():
        for field_name, field_value in _flatten_multipart_field(key, value):
            existing = serialized.get(field_name)
            if existing is None:
                serialized[field_name] = field_value
            elif isinstance(existing, list):
                existing.append(field_value)
            else:
                serialized[field_name] = [existing, field_value]
    return serialized


def _stt_validation_response_is_compatible(response: Any, extra: dict[str, Any] | None) -> bool:
    """Check that the probe response has the shape consumed at runtime."""
    response_format = extra.get("response_format") if extra else None
    if response_format in _STT_TEXT_RESPONSE_FORMATS:
        return isinstance(response.text, str)
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return False
    return isinstance(payload, Mapping) and isinstance(payload.get("text"), str)


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


def _sampling_params(llm_cfg: Any) -> dict[str, Any]:
    """Extract the shared LLM/VLM sampling params (``LLMParamsConfig``) as a dict."""
    return {
        "temperature": llm_cfg.temperature,
        "max_retries": llm_cfg.max_retries,
        "logprobs": llm_cfg.logprobs,
    }


def _with_sampling_params(extra: dict[str, Any], llm_cfg: Any) -> dict[str, Any]:
    """Add the shared LLM/VLM sampling params (``LLMParamsConfig``) to endpoint extras.

    Without this, a named LLM/VLM endpoint built via the DB-backed registry
    (di/factories.py's ``make_component_factory``, which splats ``extra``
    straight into the client constructor) never receives ``temperature`` /
    ``max_retries`` / ``logprobs`` and silently runs at the provider default.
    Mirrors the fallback config built in
    ``indexer_pool._global_llm_endpoint_config``.
    """
    return {**extra, **_sampling_params(llm_cfg)}


def _is_unmodified_seed(row: ModelEndpointRow, data: dict[str, Any]) -> bool:
    """Is *row* still byte-identical to what the seeder would have written?

    Rows created before the marker existed carry no provenance, so the slug is
    the only handle on them — but a slug match alone cannot tell an old seed from
    an endpoint an admin happened to name after the model. Adopting the latter
    would overwrite their URL and model on the first synced restart.

    Requiring the row to still match env exactly makes adoption safe by
    construction: if it matches, taking ownership changes nothing; if an admin
    has touched it, it is left alone and simply never adopted.
    """
    return row.endpoint == data["endpoint"] and (row.model_name or "") == (data["model_name"] or "")


class ModelEndpointService:
    """CRUD and lifecycle management for named model endpoints."""

    def __init__(
        self,
        *,
        model_endpoint_repo: ModelEndpointRepository,
        config: Settings,
        partition_service: Any = None,
        preset_service: Any = None,
        prompt_service: Any = None,
        client_caches: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._repo = model_endpoint_repo
        self._config = config
        self._partition_service = partition_service
        self._preset_service = preset_service
        self._prompt_service = prompt_service
        self._client_caches: dict[str, dict[str, Any]] = client_caches or {}

    async def _resolve_stt_validation_prompt(self) -> str | None:
        """Resolve the same managed prompt used by runtime transcription."""
        if self._prompt_service is None:
            return None
        try:
            prompt = await self._prompt_service.resolve_prompt("asr_transcription")
        except Exception as exc:  # noqa: BLE001 - match runtime's provider-native fallback
            logger.bind(error=str(exc)).warning("STT validation prompt resolution failed")
            return None
        return prompt.strip() if prompt and prompt.strip() else None

    # ------------------------------------------------------------------
    # Startup lifecycle
    # ------------------------------------------------------------------

    async def seed_defaults(self) -> None:
        """Insert one default endpoint per type if the DB is empty for that type.

        Seeds are derived from existing Settings / env-var values so that
        existing deployments continue working after the Phase 14 upgrade
        without any admin intervention.

        For ``llm``/``vlm``, a type with existing rows is *not* skipped
        outright: those rows are backfilled with any sampling params missing
        from their ``extra`` first (see ``_backfill_sampling_params``), since
        endpoints created before #720's fix never had them written and would
        otherwise keep silently running at the provider default forever.

        When ``models.sync_on_boot`` is set (env ``MODEL_ENDPOINT_SYNC_ON_BOOT``),
        the endpoint the seeder created is instead refreshed from Settings/env on
        every boot, so operators can manage it via env vars + a pod rollout.

        That row is found by its ``ENV_MANAGED_KEY`` marker rather than by name,
        because the name is derived from the model slug: keying off the slug meant
        that changing the model produced a name that matched nothing, so the sync
        silently did nothing and the old model stayed live. Rows seeded before the
        marker existed are adopted on first sync by matching the slug once.

        For the marked row the sync also rotates ``api_key`` inside ``extra`` —
        env is the source of truth for the credential it owns, and without this a
        rotated key never reached the DB, so requests kept using the stale key and
        failed once the provider revoked it. Every other ``extra`` key an admin set
        is preserved. Endpoints created by hand (no marker) are never touched.
        """
        seeds = self._build_default_seeds()
        now = datetime.now(UTC)
        sync_on_boot = self._config.models.sync_on_boot
        for model_type, data in seeds.items():
            existing = await self._repo.list_all(model_type=model_type)
            if existing and model_type in _SAMPLING_TYPES:
                # #720: rows created before the fix never had the sampling params
                # written, so backfill them before anything else — independent of
                # whether the env still points anywhere or sync_on_boot is set.
                await self._backfill_sampling_params(existing, getattr(self._config, model_type))

            endpoint: str = data["endpoint"]
            model_name: str = data["model_name"]
            if not endpoint:
                logger.info(f"No {model_type} endpoint configured — skipping seed.")
                continue

            name = _slug(model_name or "")
            # The marker survives a model change; the slug does not, so look for
            # the marker first and fall back to the slug only to adopt a row
            # seeded before the marker existed.
            managed_row = next((r for r in existing if r.extra.get(ENV_MANAGED_KEY) == ENV_MANAGED_VALUE), None)
            existing_row = managed_row or await self._repo.get(name, model_type)
            if existing_row is not None:
                if sync_on_boot and (managed_row is not None or _is_unmodified_seed(existing_row, data)):
                    await self._sync_env_managed(existing_row, model_type, data)
                continue

            if existing:
                # Some other endpoint of this type already exists (hand-created
                # via the admin API) — don't create a competing default. Reuses
                # the list fetched above (nothing added/removed a row since).
                continue

            row = ModelEndpointRow(
                name=name,
                model_type=model_type,
                endpoint=endpoint,
                model_name=model_name or None,
                batch_size=data.get("batch_size", 32),
                timeout=data.get("timeout", 30.0),
                extra={**data.get("extra", {}), ENV_MANAGED_KEY: ENV_MANAGED_VALUE},
                is_default=True,
                created_at=now,
                updated_at=now,
            )
            try:
                await self._repo.create(row)
            except ValidationError as exc:
                if exc.status_code != 409:
                    raise
                # Another replica can seed the same empty endpoint type
                # between our read above and this insert. The winner's row is
                # the desired default, so losing that race must not fail boot.
                logger.info(f"Default {model_type} endpoint was seeded concurrently; skipping.")
                continue
            logger.info(f"Seeded default {model_type} endpoint '{row.name}'.")

    async def _sync_env_managed(self, row: ModelEndpointRow, model_type: str, data: dict[str, Any]) -> None:
        """Refresh the env-seeded row from Settings/env.

        Only fields the environment actually owns are written. ``endpoint`` and
        ``model_name`` always are — they are what "point this at the configured
        model" means. ``batch_size``/``timeout`` are written only when their env
        var is set (see ``_ENV_OWNED_TUNABLES``); the seed carries a value for
        them regardless, so trusting the seed would overwrite an admin's tuning
        with the config default.

        ``extra`` is merged, never replaced: the marker and the API key come from
        env, everything else an admin put there survives. The key is only
        overwritten when env supplies a *real* one, so neither an unset
        ``*_API_KEY`` nor its ``EMPTY`` placeholder can clear a working credential.

        The row is deliberately **not renamed** when the model changes. The name
        is a stable identifier that partitions (``chat_llm``) and presets store by
        value, and nothing cascades a rename — so renaming would strand those
        references and the next job could not resolve the endpoint.
        """
        seed_extra: dict = data.get("extra", {}) or {}
        new_extra = {**row.extra, ENV_MANAGED_KEY: ENV_MANAGED_VALUE}
        env_api_key = seed_extra.get("api_key")
        if env_api_key and env_api_key not in _PLACEHOLDER_API_KEYS:
            new_extra["api_key"] = env_api_key

        fields: dict[str, Any] = {
            "endpoint": data["endpoint"],
            "model_name": data["model_name"] or None,
            "extra": new_extra,
        }
        for field, env_var in _ENV_OWNED_TUNABLES.get(model_type, {}).items():
            if os.getenv(env_var) is not None and field in data:
                fields[field] = data[field]

        await self._repo.update(row.name, model_type, **fields)
        logger.info(f"Synced {model_type} endpoint '{row.name}' from env (MODEL_ENDPOINT_SYNC_ON_BOOT=true).")

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
                    _with_sampling_params(_with_api_key({"implementation": "vllm"}, s.llm.api_key), s.llm),
                    s.llm.enable_thinking,
                ),
            },
            "vlm": {
                "endpoint": os.getenv("VLM_ENDPOINT", s.vlm.base_url),
                "model_name": os.getenv("VLM_MODEL", s.vlm.model),
                "timeout": s.vlm.timeout,
                "extra": _with_enable_thinking(
                    _with_sampling_params(_with_api_key({"implementation": "vllm"}, s.vlm.api_key), s.vlm),
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
            "stt": {
                # Keep the existing TRANSCRIBER_* configuration as the source
                # for the first seed, so upgrades preserve a working Whisper,
                # MOSS, or other OpenAI-compatible transcription setup.
                "endpoint": s.loader.transcriber.base_url,
                "model_name": s.loader.transcriber.model_name,
                # ``batch_size`` is the common registry column; for STT it is
                # the per-worker concurrent-transcription limit used by
                # OpenAIAudioClient.
                "batch_size": s.loader.transcriber.max_concurrent_chunks,
                "timeout": s.loader.transcriber.timeout,
                "extra": _with_api_key(
                    {},
                    None if s.loader.transcriber.api_key in _PLACEHOLDER_API_KEYS else s.loader.transcriber.api_key,
                ),
            },
        }

    async def _backfill_sampling_params(self, rows: list[ModelEndpointRow], llm_cfg: Any) -> None:
        """Fill in sampling params missing from pre-existing llm/vlm rows' ``extra``.

        ``seed_defaults()`` only seeds a type when the DB has no rows for it,
        so an endpoint created before #720's fix keeps whatever ``extra`` it
        was given — which never included ``temperature``/``max_retries``/
        ``logprobs`` (see ``_with_sampling_params``). Only keys *absent* from
        ``extra`` are filled in here, so a value an admin explicitly set (or a
        prior backfill already wrote) is never overwritten.
        """
        for row in rows:
            missing = {k: v for k, v in _sampling_params(llm_cfg).items() if k not in row.extra}
            if not missing:
                continue
            await self._repo.update(row.name, row.model_type, extra={**row.extra, **missing})
            logger.info(
                f"Backfilled sampling params on existing {row.model_type} endpoint '{row.name}'.",
                keys=sorted(missing),
            )

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
        for attr in ("embedder", "reranker", "llm", "vlm", "stt"):
            target: dict = getattr(models, attr)
            target.clear()
            target.update(buckets[attr])

        logger.info(
            "Loaded model endpoints.",
            n_embedder=len(buckets["embedder"]),
            n_llm=len(buckets["llm"]),
            n_reranker=len(buckets["reranker"]),
            n_vlm=len(buckets["vlm"]),
            n_stt=len(buckets["stt"]),
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
        if row.is_default:
            # The new endpoint became the default (repo demoted the previous one in
            # the same transaction), so the cached 'default' alias client — built
            # against the old default — is stale and must be evicted.
            self._invalidate_client_cache(row.model_type, "default")
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

        A rename also cascades to every stored reference — ``partitions.embedder``
        / ``partitions.chat_llm`` and endpoint-name fields embedded in
        ``pipeline_presets.config`` — inside the repo's own rename transaction
        (see ``PgModelEndpointRepository.rename``, #770). Those writes are
        invisible until the referencing services reload their in-memory caches,
        which is why a rename also refreshes presets then partitions here —
        the same order ``PresetService.update_preset`` uses, since partition
        resolution reads the presets dict.

        Both reload calls ``await``, so a concurrent request can run between
        them — and the DB rename has *already* committed by that point. Without
        ``_alias_renamed_name``, a request landing in that window could resolve
        a partition/preset that the cascade already repointed at ``new_name``
        against a registry that (until the final ``load_all()`` below) still
        only knows ``name`` — a bare ``KeyError``. The alias makes both ``name``
        and ``new_name`` resolve immediately, built from the row this call just
        wrote — not whatever the in-memory bucket held before it — so a rename
        combined with a field change (e.g. a new ``endpoint``) aliases the
        *updated* config, not a stale pre-update one. That also covers a reload
        call above raising: the registry stays queryable under both names,
        correctly, instead of the update's failure leaving it stuck on a stale
        config until process restart.
        """
        existing = await self._repo.get(name, model_type)
        if existing is None:
            raise NotFoundError(f"Endpoint '{name}' of type '{model_type}' not found.")

        new_name: str | None = fields.pop("new_name", None)  # type: ignore[assignment]
        # 'is_default' is not a plain column edit: flipping it must clear the previous
        # default in the same transaction, so route a truthy value through set_default
        # (atomic clear-then-set) instead of letting the repo write a second
        # is_default=true row. A false/None value is a no-op here — you switch the
        # default by promoting another endpoint, never by leaving the type with none.
        promote_to_default = bool(fields.pop("is_default", None))

        if isinstance(fields.get("extra"), dict):
            fields["extra"] = preserve_existing_secrets(existing.extra, fields["extra"])  # type: ignore[arg-type]

        if fields:
            updated = await self._repo.update(name, model_type, **fields)
        else:
            updated = existing

        effective_name = name
        renamed_from: str | None = None
        if new_name and new_name != name:
            await self._repo.rename(name, model_type, new_name)
            effective_name = new_name
            renamed_from = name
            self._alias_renamed_name(model_type, name, new_name, updated or existing)
            # A cached *client instance* under either name would otherwise survive
            # this alias — the factory checks its cache before consulting the
            # config registry, so a stale pre-rename/pre-update client would keep
            # serving until the eviction at the end of this method, which a
            # reload call below raising would skip entirely. The config alias
            # above is already fresh, so evicting now is safe: anything rebuilt
            # from either name resolves through the up-to-date config, not stale
            # cached state.
            self._invalidate_client_cache(model_type, name)
            self._invalidate_client_cache(model_type, new_name)
            if self._preset_service is not None:
                await self._preset_service.load_all()
            if self._partition_service is not None:
                await self._partition_service.load_partitions()

        if promote_to_default:
            # Clears any prior default and sets this row in one transaction, then
            # re-points the 'default' alias to it — never leaves two defaults.
            await self._repo.set_default(model_type, effective_name)
        # The 'default' alias client is stale when this endpoint becomes the default
        # OR was already the default (its config just changed).
        evict_default = bool(promote_to_default or existing.is_default)

        # Reload the in-memory config FIRST, then evict, so a client rebuilt during
        # the reload window from the old config cannot survive in the cache.
        await self.load_all()
        if renamed_from is not None:
            self._invalidate_client_cache(model_type, renamed_from)
        self._invalidate_client_cache(model_type, effective_name)
        if evict_default:
            self._invalidate_client_cache(model_type, "default")
        return await self._repo.get(effective_name, model_type) or (updated or existing)

    async def delete_model_endpoint(self, name: str, model_type: str) -> None:
        """Delete an endpoint.

        Raises 404 if not found, 422 if it is the last endpoint of its type
        (would leave components with no fallback).
        """
        # The last-endpoint guard and the survivor/default choice are made INSIDE
        # the repo's locked transaction (not from a stale snapshot here), so
        # concurrent deletes of the same type can't both pass the count check or
        # promote an already-deleted survivor — which would leave the type with no
        # endpoint / no default. The repo reports what happened.
        status, promoted = await self._repo.delete_and_promote_default(name, model_type)
        if status == "not_found":
            raise NotFoundError(f"Endpoint '{name}' of type '{model_type}' not found.")
        if status == "last":
            raise ValidationError(f"Cannot delete the last '{model_type}' endpoint. Register a replacement first.")

        # Reload the in-memory config FIRST, then evict: evicting before load_all
        # leaves a window where a concurrent request rebuilds a client from the old
        # config and re-caches it; evicting after the reload drops any such stale
        # client so the next request rebuilds from the fresh config.
        await self.load_all()
        self._invalidate_client_cache(model_type, name)
        if promoted is not None:
            # The deleted endpoint was the default; its 'default' alias client is now stale.
            self._invalidate_client_cache(model_type, "default")

    async def set_default(self, model_type: str, name: str) -> None:
        """Promote ``name`` to the default endpoint for ``model_type``."""
        existing = await self._repo.get(name, model_type)
        if existing is None:
            raise NotFoundError(f"Endpoint '{name}' of type '{model_type}' not found.")
        await self._repo.set_default(model_type, name)
        # Reload before evicting so a client rebuilt during the window can't survive.
        await self.load_all()
        self._invalidate_client_cache(model_type, "default")

    # ------------------------------------------------------------------
    # Endpoint validation
    # ------------------------------------------------------------------

    async def validate_endpoint(
        self,
        url: str,
        model_name: str | None = None,
        *,
        api_key: str | None = None,
        model_type: str | None = None,
        timeout: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Probe an endpoint's model list and, for STT, transcription route.

        Returns a dict with ``reachable``, ``model_found``, ``models_served``,
        ``transcription_supported``, and ``detail`` keys — suitable as a
        ``ValidateEndpointResponse`` payload.
        """
        import httpx  # local import to avoid hard dep in tests that mock it

        result: dict[str, Any] = {
            "reachable": False,
            "model_found": None,
            "models_served": None,
            "transcription_supported": None,
            "detail": None,
        }
        normalized_model_name = model_name.strip() if model_name is not None else None
        if model_type == "stt" and not normalized_model_name:
            result["transcription_supported"] = False
            result["detail"] = "A model name is required to validate audio transcription."
            return result
        try:
            parsed = urlsplit(url)
        except ValueError:
            result["detail"] = "Endpoint URL must be an absolute HTTP(S) URL."
            return result
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            result["detail"] = "Endpoint URL must be an absolute HTTP(S) URL."
            return result
        if parsed.username or parsed.password:
            result["detail"] = "Endpoint URL must not include credentials."
            return result
        base_url = url.rstrip("/")
        models_url = base_url + "/models"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            async with httpx.AsyncClient(timeout=5.0, headers=headers, follow_redirects=False) as client:
                try:
                    resp = await client.get(models_url)
                except httpx.TimeoutException:
                    if model_type != "stt":
                        raise
                    # Model discovery is intentionally short. STT validation
                    # can still be proven by the real audio request below.
                    result["detail"] = "Model list request timed out."
                else:
                    if resp.status_code in {401, 403} and model_type == "stt":
                        result["transcription_supported"] = False
                        result["detail"] = (
                            f"Model list request was rejected with HTTP {resp.status_code}. Check the API key."
                        )
                        return result
                    result["reachable"] = True
                    if resp.status_code == 200:
                        try:
                            data = resp.json()
                            models = data.get("data") if isinstance(data, dict) else None
                            if not isinstance(models, list):
                                raise ValueError("missing list-valued data field")
                        except (TypeError, ValueError):
                            # A reachable endpoint may expose a non-standard or
                            # broken /models response while still implementing the
                            # audio route. Do not let that prevent the STT probe.
                            result["detail"] = "Endpoint returned an invalid model list."
                        else:
                            served = [
                                item["id"]
                                for item in models
                                if isinstance(item, dict) and isinstance(item.get("id"), str)
                            ]
                            result["models_served"] = served
                            if normalized_model_name is not None:
                                result["model_found"] = normalized_model_name in served
                    else:
                        result["detail"] = f"Model list returned HTTP {resp.status_code}."
                if model_type == "stt":
                    # The UI already rejects an unavailable model, so avoid a
                    # needless upload and inference request in that case.
                    if result["model_found"] is False:
                        return result
                    prompt = await self._resolve_stt_validation_prompt()
                    # A well-formed request is required to validate credentials:
                    # some providers reject a missing file/model with 400/422
                    # before they authenticate the request.
                    transcription_response = await client.post(
                        base_url + "/audio/transcriptions",
                        data=_stt_validation_form_data(normalized_model_name, extra, prompt),
                        files={
                            "file": (
                                "openrag-stt-validation.wav",
                                _STT_VALIDATION_WAV,
                                "audio/wav",
                            )
                        },
                        follow_redirects=True,
                        timeout=httpx.Timeout(
                            connect=5.0,
                            read=timeout if timeout is not None else _STT_VALIDATION_TIMEOUT_SECONDS,
                            write=5.0,
                            pool=5.0,
                        ),
                    )
                    result["reachable"] = True
                    transcription_status = transcription_response.status_code
                    if 200 <= transcription_status < 300:
                        if _stt_validation_response_is_compatible(transcription_response, extra):
                            result["transcription_supported"] = True
                        else:
                            result["transcription_supported"] = False
                            result["detail"] = "Transcription endpoint returned an incompatible response."
                    elif transcription_status in {401, 403}:
                        result["transcription_supported"] = False
                        result["detail"] = (
                            f"Transcription capability check was rejected with HTTP {transcription_status}. "
                            "Check the API key."
                        )
                    elif transcription_status in {404, 405}:
                        result["transcription_supported"] = False
                        result["detail"] = "Endpoint does not support OpenAI-compatible audio transcriptions."
                    elif 300 <= transcription_status < 400 or transcription_status >= 500:
                        result["transcription_supported"] = False
                        result["detail"] = f"Transcription capability check returned HTTP {transcription_status}."
                    else:
                        result["transcription_supported"] = False
                        result["detail"] = f"Transcription validation request returned HTTP {transcription_status}."
        except httpx.ConnectError as exc:
            if result["reachable"] and model_type == "stt":
                result["transcription_supported"] = False
                result["detail"] = f"Transcription capability check failed: {exc}"
            else:
                result["detail"] = f"Connection error: {exc}"
        except httpx.TimeoutException:
            if result["reachable"] and model_type == "stt":
                result["transcription_supported"] = False
                result["detail"] = "Transcription capability check timed out"
            else:
                result["detail"] = "Connection timed out"
        except Exception as exc:  # noqa: BLE001
            result["detail"] = str(exc)
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _alias_renamed_name(self, model_type: str, old_name: str, new_name: str, row: ModelEndpointRow) -> None:
        """Make both ``old_name`` and ``new_name`` resolve to *row* before any reload runs.

        Runs synchronously right after the rename ``await`` returns — no
        further ``await`` happens before this executes, so no concurrent
        request can observe the DB already renamed while the registry still
        only answers to ``old_name``.

        Built from ``row`` — the just-written DB state — rather than copying
        whatever the in-memory bucket currently holds under ``old_name``: a
        rename can land in the same call as a field update (e.g. a new
        ``endpoint`` URL), applied to the DB *before* this runs, so the stale
        in-memory entry would alias both names to the pre-update config. If a
        reload below then raises, that staleness would never get corrected
        by the final ``load_all()`` this call never reaches — the registry
        would keep serving the old endpoint under the new (DB-authoritative)
        name until process restart. The next full ``load_all()`` (below, or
        from any later CRUD call) rebuilds the bucket straight from DB and
        drops the ``old_name`` entry on its own.
        """
        bucket: dict[str, Any] | None = getattr(self._config.models, model_type, None)
        if bucket is None:
            return
        cfg = ModelEndpointConfig(
            endpoint=row.endpoint,
            model_name=row.model_name,
            batch_size=row.batch_size,
            timeout=row.timeout,
            extra=row.extra,
        )
        bucket[old_name] = cfg
        bucket[new_name] = cfg

    def _invalidate_client_cache(self, model_type: str, name: str) -> None:
        """Evict ``name`` from the component-factory cache for ``model_type``."""
        cache = self._client_caches.get(model_type)
        if cache is not None:
            cache.pop(name, None)


__all__ = ["ModelEndpointService"]
