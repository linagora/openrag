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


# Provenance marker written into an endpoint's ``extra`` when the seeder creates
# it from env. It is what lets boot-time sync find *its own* row again after the
# configured model — and therefore the slug the row was named after — changes.
# Kept in ``extra`` rather than a new column so this needs no Alembic migration;
# ``extra`` already carries control keys (``implementation``), and factories.py
# filters both out before splatting the rest into a client constructor.
ENV_MANAGED_KEY = "managed_by"
ENV_MANAGED_VALUE = "env"


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

    # When True, the env-derived endpoint for each type (the one whose name
    # matches _slug(model_name)) is kept in sync with Settings/env on every
    # boot instead of only on first seed — lets operators manage it purely
    # via env vars + a pod rollout. Endpoints created by hand (any other
    # name) are never touched. Default False preserves the "DB is the
    # editable source of truth after first boot" behavior.
    sync_on_boot: bool = False


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


__all__ = ["ModelEndpointConfig", "ModelsConfig", "ModelEndpointRow", "ModelEndpointType"]
