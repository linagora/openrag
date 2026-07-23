"""Named model endpoint registry — multi-endpoint config for embedders, LLMs, rerankers, VLMs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from core.config.base import ConfigMixin
from pydantic import BaseModel, Field

ModelEndpointType = Literal["embedder", "reranker", "llm", "vlm"]


class ModelEndpointConfig(BaseModel):
    """A single registered inference endpoint.

    ``extra`` holds implementation-specific keys:
      ``{"implementation": "vllm"}``    → VLLMEmbedder
      ``{"implementation": "ollama"}``  → OllamaEmbedder
      ``{"implementation": "infinity"}``→ InfinityReranker
      ``{"api_key": "sk-..."}``         → passed to client constructor
    """

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


__all__ = [
    "LLM_CONTEXT_SIZE_KEY",
    "LLM_OUTPUT_TOKENS_KEY",
    "ModelEndpointConfig",
    "ModelsConfig",
    "ModelEndpointRow",
    "ModelEndpointType",
]
