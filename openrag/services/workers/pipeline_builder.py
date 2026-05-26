from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Any

from core.chunking.chunking_strategy import ChunkingStrategy
from core.embeddings.embedder import Embedder
from core.indexing.contextualize import ChunkContextualizer
from core.indexing.parsers.document_parser import DocumentParser
from core.vector_stores.vector_store import VectorStore
from core.vlm.vlm import VLM
from services.workers.stages.caption import caption_stage
from services.workers.stages.chunk import chunk_stage
from services.workers.stages.contextualize import contextualize_stage
from services.workers.stages.embed import embed_stage
from services.workers.stages.parse import parse_stage
from services.workers.stages.store import store_stage


@dataclass(slots=True, frozen=True)
class PipelineTimeouts:
    """Per-stage timeout configuration for an indexing pipeline row."""

    parse: float | None = None
    caption: float | None = None
    caption_per_image: float = 0.0
    chunk: float | None = None
    contextualize: float | None = None
    contextualize_per_chunk: float = 0.0
    embed: float | None = None
    embed_per_chunk: float = 0.0
    store: float | None = None
    store_per_chunk: float = 0.0


@dataclass(slots=True, frozen=True)
class IndexingPipeline:
    """Sequential indexing pipeline assembled from worker stage functions."""

    parser: DocumentParser
    chunker: ChunkingStrategy
    embedder: Embedder
    vector_store: VectorStore
    vlm: VLM | None = None
    contextualizer: ChunkContextualizer | None = None
    timeouts: PipelineTimeouts = PipelineTimeouts()

    async def run(self, row: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        """Run a single row through parse, optional enrichments, embed, and store."""

        await parse_stage(row, self.parser, timeout=self.timeouts.parse)
        if self.vlm is not None:
            await caption_stage(
                row,
                self.vlm,
                timeout=self.timeouts.caption,
                per_image_timeout=self.timeouts.caption_per_image,
            )
        await chunk_stage(row, self.chunker, timeout=self.timeouts.chunk)
        if self.contextualizer is not None:
            await contextualize_stage(
                row,
                self.contextualizer,
                timeout=self.timeouts.contextualize,
                per_chunk_timeout=self.timeouts.contextualize_per_chunk,
            )
        await embed_stage(
            row,
            self.embedder,
            timeout=self.timeouts.embed,
            per_chunk_timeout=self.timeouts.embed_per_chunk,
        )
        await store_stage(
            row,
            self.vector_store,
            timeout=self.timeouts.store,
            per_chunk_timeout=self.timeouts.store_per_chunk,
        )
        return row


def build_indexing_pipeline(
    *,
    parser: DocumentParser,
    chunker: ChunkingStrategy,
    embedder: Embedder,
    vector_store: VectorStore,
    vlm: VLM | None = None,
    contextualizer: ChunkContextualizer | None = None,
    timeouts: PipelineTimeouts | None = None,
) -> IndexingPipeline:
    """Build the default sequential indexing pipeline."""

    return IndexingPipeline(
        parser=parser,
        chunker=chunker,
        embedder=embedder,
        vector_store=vector_store,
        vlm=vlm,
        contextualizer=contextualizer,
        timeouts=timeouts or PipelineTimeouts(),
    )


__all__ = ["IndexingPipeline", "PipelineTimeouts", "build_indexing_pipeline"]
