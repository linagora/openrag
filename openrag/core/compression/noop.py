"""Passthrough compressor — the default backend."""

from __future__ import annotations

from core.models.compression import CompressionOptions

from .compressor import Compressor
from .registry import compressor_registry


@compressor_registry.register("noop")
class NoopCompressor(Compressor):
    """Returns every text unchanged."""

    name = "noop"

    async def _compress(self, texts: list[str], *, options: CompressionOptions) -> list[str]:
        return texts


__all__ = ["NoopCompressor"]
