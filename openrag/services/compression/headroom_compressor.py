"""Headroom-backed compressor.

Headroom (https://github.com/headroomlabs-ai/headroom) routes content to a
per-type compressor: statistical folding for JSON and logs, a small ModernBERT
model for prose. Retrieved chunks are prose, so the model path is the one that
does the work here. It runs on CPU by default.

``headroom.compress`` is synchronous and CPU-bound, so it is called in a worker
thread. Each text is sent as its own user message in one batched call, which
keeps us on the public API instead of reaching into Headroom's router.
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.compression import Compressor, compressor_registry
from core.models.compression import CompressionOptions
from core.utils.logging import get_logger

logger = get_logger()

DEFAULT_MODEL = "gpt-4o"


@compressor_registry.register("headroom")
class HeadroomCompressor(Compressor):
    """Compresses text with Headroom's content-aware pipeline."""

    name = "headroom"

    def __init__(self, *, model: str = DEFAULT_MODEL, **extra: Any) -> None:
        try:
            from headroom import CompressConfig, compress
        except ImportError as exc:
            raise RuntimeError("headroom backend selected but headroom-ai is not installed") from exc

        self._compress_fn = compress
        self._config_cls = CompressConfig
        self._model = model
        self._extra = extra

    async def _compress(self, texts: list[str], *, options: CompressionOptions) -> list[str]:
        indices = [i for i, t in enumerate(texts) if len(t) >= options.min_chars]
        if not indices:
            return texts

        compressed = await asyncio.to_thread(self._run, [texts[i] for i in indices], options)
        if compressed is None:
            return texts

        out = list(texts)
        for i, text in zip(indices, compressed, strict=True):
            out[i] = text
        return out

    def _run(self, texts: list[str], options: CompressionOptions) -> list[str] | None:
        config = self._config_cls(
            compress_user_messages=True,
            protect_recent=0,
            protect_analysis_context=False,
            target_ratio=options.target_ratio,
            **self._extra,
        )
        result = self._compress_fn(
            [{"role": "user", "content": t} for t in texts],
            model=self._model,
            config=config,
        )
        messages = result.messages
        if len(messages) != len(texts):
            logger.error(f"headroom returned {len(messages)} messages for {len(texts)} texts")
            return None
        return [
            m["content"] if isinstance(m.get("content"), str) else original
            for m, original in zip(messages, texts, strict=True)
        ]


__all__ = ["HeadroomCompressor"]
