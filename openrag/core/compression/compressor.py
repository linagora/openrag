"""Abstract context compressor interface."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Sequence

from core.models.compression import CompressionOptions, CompressionResult
from core.utils.logging import get_logger

logger = get_logger()


class Compressor(ABC):
    """Shrinks text before it reaches an LLM.

    Subclasses implement :meth:`_compress` and return one string per input.
    :meth:`compress` wraps it with the guarantees callers rely on: order and
    count are preserved, a text is never replaced by a longer one, work is
    bounded by ``options.timeout_s``, and any failure returns the originals
    rather than raising. A compression fault must not fail a RAG query.
    """

    name: str = "compressor"

    async def compress(self, texts: Sequence[str], *, options: CompressionOptions) -> CompressionResult:
        originals = list(texts)
        if not originals:
            return CompressionResult(texts=[], backend=self.name)

        try:
            async with asyncio.timeout(options.timeout_s):
                compressed = await self._compress(originals, options=options)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return self._passthrough(originals, "timeout")
        except Exception as exc:
            logger.warning(f"Compressor '{self.name}' failed, passing content through: {exc}")
            return self._passthrough(originals, str(exc))

        if len(compressed) != len(originals):
            logger.error(f"Compressor '{self.name}' returned {len(compressed)} texts for {len(originals)} inputs")
            return self._passthrough(originals, "cardinality mismatch")

        kept = [c if len(c) < len(o) else o for o, c in zip(originals, compressed, strict=True)]
        return CompressionResult(
            texts=kept,
            backend=self.name,
            chars_before=sum(len(t) for t in originals),
            chars_after=sum(len(t) for t in kept),
        )

    @abstractmethod
    async def _compress(self, texts: list[str], *, options: CompressionOptions) -> list[str]:
        """Compress ``texts``, returning one entry per input in the same order."""

    async def aclose(self) -> None:
        """Release backend resources. Overridden when a backend holds any."""

    def _passthrough(self, texts: list[str], detail: str) -> CompressionResult:
        total = sum(len(t) for t in texts)
        return CompressionResult(
            texts=texts, backend=self.name, chars_before=total, chars_after=total, degraded=True, detail=detail
        )


__all__ = ["Compressor"]
