"""Compression domain models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CompressionOptions(BaseModel):
    """Per-call compression knobs."""

    model_config = ConfigDict(frozen=True)

    target_ratio: float | None = Field(default=None, gt=0.0, le=1.0)
    min_chars: int = Field(default=1000, ge=0)
    timeout_s: float = Field(default=15.0, gt=0.0)


class CompressionResult(BaseModel):
    """Outcome of one compression call.

    ``texts`` always has the same length and order as the input: callers map
    results back to their sources by position.
    """

    model_config = ConfigDict(frozen=True)

    texts: list[str]
    backend: str
    chars_before: int = 0
    chars_after: int = 0
    degraded: bool = False
    detail: str | None = None

    @property
    def ratio(self) -> float:
        """Fraction of characters removed, 0.0 when nothing was compressed."""
        if self.chars_before <= 0:
            return 0.0
        return max(0, self.chars_before - self.chars_after) / self.chars_before


__all__ = ["CompressionOptions", "CompressionResult"]
