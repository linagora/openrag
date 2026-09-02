"""Context compression configuration."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from .base import ConfigMixin


class CompressionConfig(ConfigMixin):
    """Deployment-wide compression settings.

    ``enabled`` is the global kill switch; whether a given request compresses
    is decided per partition by its retrieval preset. Off by default: on prose
    the gain is modest and it costs latency on the query path.
    """

    enabled: bool = False
    backend: str = "noop"
    target_ratio: float | None = Field(default=None, gt=0.0, le=1.0)
    min_chars: int = Field(default=1000, ge=0)
    timeout_s: float = Field(default=5.0, gt=0.0)
    extra: dict[str, Any] = Field(default_factory=dict)


__all__ = ["CompressionConfig"]
