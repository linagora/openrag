"""Headroom-backed compressor.

Headroom (https://github.com/headroomlabs-ai/headroom) routes content to a
per-type compressor: statistical folding for JSON and logs, a small ModernBERT
model for prose. Retrieved chunks are prose, so the model path is the one that
does the work here. It runs on CPU and needs no GPU.

``headroom.compress`` is synchronous and CPU-bound, so it is called in a worker
thread. Each text is sent as its own user message in one batched call, which
keeps us on the public API instead of reaching into Headroom's router.

Two behaviours of the upstream package this adapter has to account for:

* Compression is gated on an in-process model cache, not the on-disk one, so a
  fresh worker silently compresses nothing until the model is loaded.
  ``ensure_background_load`` primes it without blocking construction.
* Prose compression costs roughly 500 ms per 400-word chunk on CPU, so a
  ``top_n`` of 10 adds several seconds. Size ``compression.timeout_s``
  accordingly; the base class passes content through when it is exceeded.

``headroom-ai`` also declares ``litellm``, which collides with the httpx pin of
``infinity-client``; the dependency is dropped via a uv override (see
``pyproject.toml``). Import is deferred to construction regardless, and
``di.compressors.create_compressor`` falls back to noop when it fails.
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

    def __init__(self, *, model: str = DEFAULT_MODEL, warmup: bool = True, **extra: Any) -> None:
        try:
            from headroom import CompressConfig, compress
        except ImportError as exc:
            raise RuntimeError("headroom backend selected but headroom-ai is not installed") from exc

        self._compress_fn = compress
        self._config_cls = CompressConfig
        self._model = model
        self._extra = extra
        if warmup:
            self._warmup()

    def _warmup(self) -> None:
        """Prime the prose model off the calling thread.

        Without this the first requests in every process route to a no-op: the
        readiness gate reads an in-process cache that only a load populates.
        Downloading is someone else's thread, so construction stays I/O-free.
        """
        try:
            from headroom.transforms.kompress_compressor import KompressCompressor

            KompressCompressor().ensure_background_load()
        except Exception as exc:
            logger.warning(f"Headroom model warmup failed; compression stays inactive until it loads: {exc}")

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
