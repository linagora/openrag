"""Named model endpoint registry — embedders, rerankers, LLMs, VLMs, and STT."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from core.config.base import ConfigMixin
from pydantic import BaseModel, Field

ModelEndpointType = Literal["embedder", "reranker", "llm", "vlm", "stt"]
PLACEHOLDER_API_KEYS = frozenset({"", "EMPTY"})
DEFAULT_MODEL_IMPLEMENTATIONS = {
    "embedder": "vllm",
    "llm": "vllm",
    "vlm": "vllm",
    "reranker": "infinity",
    "stt": "vllm",
}

# Virtual endpoint name. No ``model_endpoints`` row carries it: it is the key
# ``ModelEndpointService.load_all`` files the ``is_default=True`` row under, so
# a partition or preset can reference "whichever endpoint is default" without
# naming it. Anything that resolves a stored reference against the DB has to
# account for it — see ``PgPartitionRepository`` (assignment checks) and
# ``PgModelEndpointRepository`` (usage counts, delete guard).
DEFAULT_ENDPOINT_ALIAS = "default"


def is_placeholder_api_key(value: object) -> bool:
    """Return whether a configured API key represents anonymous access."""
    return value is None or (isinstance(value, str) and value.strip() in PLACEHOLDER_API_KEYS)


class ModelEndpointConfig(BaseModel):
    """A single registered inference endpoint.

    ``name`` is the stable registry identity when this config came from the
    model-endpoint registry. It stays absent for legacy environment-only
    configuration, which has no independently managed registration.

    ``extra`` holds implementation-specific keys:
      ``{"implementation": "vllm"}``    → VLLMEmbedder
      ``{"implementation": "ollama"}``  → OllamaEmbedder
      ``{"implementation": "infinity"}``→ InfinityReranker
      ``{"api_key": "sk-..."}``         → passed to client constructor
      ``{"max_model_len": 8192}``       → embedders: this endpoint's context
          window, mirroring the global ``embedder.max_model_len``. A partition
          may point at an embedder with a different window than the deployment
          default, and the chunker derives its hard safety bound from it (see
          ``core.chunking.factory.create_chunker``). Absent → global value.
    """

    name: str | None = None
    endpoint: str
    model_name: str | None = None
    batch_size: int = Field(default=32, gt=0)
    timeout: float = Field(default=30.0, gt=0)
    extra: dict[str, Any] = Field(default_factory=dict)


def _positive_int(value: Any) -> int | None:
    """Return *value* if it is a positive int, else ``None``.

    Used for the admin-tunable LLM token budgets stored in an endpoint's
    ``extra``. Strict on purpose (mirrors the write-side schema validation): a
    missing key, a non-int (``bool``, ``float``, ``str``), or a non-positive
    number all mean "no override — fall back to the global default", rather than
    silently truncating a float or coercing a string.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


# Well-known ``extra`` keys carrying the LLM's admin-configurable token budgets
# (surfaced as first-class fields in the admin UI). Kept in ``extra`` rather than
# as dedicated columns so they stay LLM-only and need no migration, mirroring the
# existing ``max_model_len`` / ``embed_concurrency`` convention.
LLM_CONTEXT_SIZE_KEY = "max_llm_context_size"
LLM_OUTPUT_TOKENS_KEY = "max_output_tokens"

# Optional language hint for OpenAI-compatible speech-to-text endpoints. It is
# stored in ``extra`` because it applies only to STT requests and needs no
# schema column; when set it takes precedence over the optional Whisper-based
# language detector.
STT_LANGUAGE_KEY = "language"

# Optional post-processing for MOSS diarized output. It is deliberately an
# endpoint control rather than a provider request option: OpenRAG consumes it
# after transcription, so it must never be forwarded to the provider.
MOSS_SPEAKER_AWARE_KEY = "moss_speaker_aware"

# Provenance marker written into an endpoint's ``extra`` when the seeder creates
# it from env. It is what lets boot-time sync find *its own* row again after the
# configured model — and therefore the slug the row was named after — changes.
# Kept in ``extra`` rather than a new column so this needs no Alembic migration.
ENV_MANAGED_KEY = "managed_by"
ENV_MANAGED_VALUE = "env"

# Every ``extra`` key that is control/bookkeeping rather than a constructor
# kwarg. Any site that splats ``extra`` into a client must strip all of these:
# a leaked key reaches the provider as an unknown field, and a strict
# OpenAI-compatible server answers 400 (the leak #712 fixed for ``batch_size``).
# One set in ``core`` so the DI factories *and* the Ray worker factories in
# ``services`` share it — they previously each hardcoded their own list, which
# is exactly how the worker factories ended up still forwarding keys the DI
# factory already stripped.
CONTROL_EXTRA_KEYS = frozenset({"implementation", ENV_MANAGED_KEY, LLM_CONTEXT_SIZE_KEY, LLM_OUTPUT_TOKENS_KEY})

# Connection metadata and OpenRAG-owned fields must not leak into an STT
# provider's multipart request. Shared by runtime transcription and the Admin
# UI validation probe so a saved endpoint is tested with the payload it will
# actually receive.
STT_REQUEST_CONTROL_EXTRA_KEYS = CONTROL_EXTRA_KEYS | frozenset(
    {
        "api_key",
        STT_LANGUAGE_KEY,
        MOSS_SPEAKER_AWARE_KEY,
        "file",
        "model",
        "prompt",
        "stream",
    }
)


class ModelsConfig(ConfigMixin):
    """Named endpoint dictionaries — one per model type.

    Fields are frozen (Pydantic ConfigMixin), but the dict objects they
    hold are mutable. Services perform atomic-ish in-place swaps via
    ``dict.clear() + dict.update()`` rather than reassigning the field.
    """

    embedder: dict[str, ModelEndpointConfig] = Field(default_factory=dict)
    reranker: dict[str, ModelEndpointConfig] = Field(default_factory=dict)
    llm: dict[str, ModelEndpointConfig] = Field(default_factory=dict)
    vlm: dict[str, ModelEndpointConfig] = Field(default_factory=dict)
    stt: dict[str, ModelEndpointConfig] = Field(default_factory=dict)

    # When True, the endpoint the seeder created from env is refreshed from
    # Settings/env on every boot instead of only on first seed — lets operators
    # manage it via env vars + a pod rollout. Endpoints created by hand are
    # never touched. Default False preserves the "DB is the editable source of
    # truth after first boot" behavior.
    sync_on_boot: bool = False

    def llm_extra(self, name: str = "default") -> dict[str, Any]:
        """``extra`` payload of the named LLM endpoint (``{}`` if unregistered).

        ``name`` defaults to the ``"default"`` alias (see ``ModelEndpointService``),
        but any catalogued endpoint name works — in particular a partition's
        ``chat_llm`` preset, so the token preflight can read that endpoint's
        budget instead of always falling back to the global default.
        """
        endpoint = self.llm.get(name)
        return dict(endpoint.extra) if endpoint is not None else {}

    def llm_context_size(self, name: str = "default") -> int | None:
        """Admin-configured context window of the named LLM endpoint, if any."""
        return _positive_int(self.llm_extra(name).get(LLM_CONTEXT_SIZE_KEY))

    def llm_output_tokens(self, name: str = "default") -> int | None:
        """Admin-configured max output tokens of the named LLM endpoint, if any."""
        return _positive_int(self.llm_extra(name).get(LLM_OUTPUT_TOKENS_KEY))


class ModelEndpointRow(BaseModel):
    """DB representation of a model endpoint (returned by the repository)."""

    name: str
    model_type: ModelEndpointType
    endpoint: str
    model_name: str | None = None
    batch_size: int = Field(default=32, gt=0)
    timeout: float = Field(default=30.0, gt=0)
    extra: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False
    created_at: datetime
    updated_at: datetime


# What decides the vectors an embedder produces, and so what an in-place edit
# can move out from under already-indexed files (#762). Mirrored by
# MATERIAL_FIELDS / MATERIAL_EXTRA_KEYS in ui/src/pages/admin/embedder-edit-guard.ts,
# which decides when the UI asks for the acknowledgement the API requires.
# `max_model_len` becomes the embedder's truncation limit: the same model at
# another limit embeds long chunks differently.
MATERIAL_EMBEDDER_EXTRA_KEYS = ("implementation", "max_model_len")


def _shown(value: object) -> str | None:
    """Compare stored and submitted values the way the edit form renders them."""
    return None if value is None or value == "" else str(value)


def embedder_fingerprint(
    endpoint: str | None,
    model_name: str | None,
    extra: Mapping[str, Any] | None,
) -> dict[str, str | None]:
    """The part of an embedder's config that decides its vectors, normalized.

    Two configs with the same fingerprint embed alike; what else they differ in
    (timeout, batch size, API key) cannot change a vector. The edit guard uses it
    to decide whether an edit needs acknowledging, and the indexer to check,
    before it records a file, that the partition's embedder is still the one it
    embedded with (#958), so both judge a change the same way.
    """
    extra = extra or {}
    fingerprint = {
        "endpoint": (endpoint or "").strip().rstrip("/") or None,
        "model_name": _shown(model_name),
    }
    for key in MATERIAL_EMBEDDER_EXTRA_KEYS:
        # An endpoint saved without `implementation` runs the default client,
        # so stamping that default on a later save changes nothing.
        fallback = DEFAULT_MODEL_IMPLEMENTATIONS["embedder"] if key == "implementation" else None
        fingerprint[f"extra.{key}"] = _shown(extra.get(key, fallback))
    return fingerprint


def material_embedder_changes(existing: ModelEndpointRow, fields: Mapping[str, object]) -> list[str]:
    """Fields of an embedder update that would change the vectors it produces."""
    endpoint = fields.get("endpoint")
    model_name = fields.get("model_name", existing.model_name)
    extra = fields.get("extra")
    before = embedder_fingerprint(existing.endpoint, existing.model_name, existing.extra)
    after = embedder_fingerprint(
        endpoint if isinstance(endpoint, str) else existing.endpoint,
        None if model_name is None else str(model_name),
        extra if isinstance(extra, dict) else existing.extra,
    )
    return [key for key in before if before[key] != after[key]]


__all__ = [
    "LLM_CONTEXT_SIZE_KEY",
    "LLM_OUTPUT_TOKENS_KEY",
    "MATERIAL_EMBEDDER_EXTRA_KEYS",
    "MOSS_SPEAKER_AWARE_KEY",
    "PLACEHOLDER_API_KEYS",
    "STT_REQUEST_CONTROL_EXTRA_KEYS",
    "STT_LANGUAGE_KEY",
    "ModelEndpointConfig",
    "ModelsConfig",
    "ModelEndpointRow",
    "ModelEndpointType",
    "embedder_fingerprint",
    "is_placeholder_api_key",
    "material_embedder_changes",
]
