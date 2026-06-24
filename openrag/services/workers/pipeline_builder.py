from __future__ import annotations

from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from typing import Any

from core.chunking.chunking_strategy import ChunkingStrategy
from core.config.indexation_pipeline import IndexationPipelineConfig
from core.embeddings.embedder import Embedder
from core.indexing.contextualize import ChunkContextualizer
from core.indexing.parsers.document_parser import DocumentParser
from core.indexing.topic_tags import TopicTagger
from core.models.document import Document, DocumentType
from core.utils.logging import get_logger
from core.vector_stores.vector_store import VectorStore
from core.vlm.vlm import VLM
from services.workers.stages.caption import caption_stage
from services.workers.stages.chunk import chunk_stage
from services.workers.stages.contextualize import contextualize_stage
from services.workers.stages.embed import embed_stage
from services.workers.stages.parse import parse_stage
from services.workers.stages.store import store_stage
from services.workers.stages.topic_tag import topic_tag_stage

logger = get_logger()


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
    topic_tag: float | None = None


@dataclass(slots=True, frozen=True)
class IndexingPipeline:
    """Sequential indexing pipeline assembled from worker stage functions."""

    parser: DocumentParser
    chunker: ChunkingStrategy
    embedder: Embedder
    vector_store: VectorStore
    vlm: VLM | None = None
    # Global gate for captioning images *embedded* in other documents
    # (mirrors ``config.loader.image_captioning``). Standalone image files are
    # always captioned when a VLM is available, regardless of this flag.
    image_captioning: bool = True
    contextualizer: ChunkContextualizer | None = None
    topic_tagger: TopicTagger | None = None
    timeouts: PipelineTimeouts = PipelineTimeouts()
    indexation_config: IndexationPipelineConfig | None = None
    parser_factory: Callable[[str], DocumentParser] | None = None
    chunker_factory: Callable[[Any], ChunkingStrategy] | None = None
    embedder_factory: Callable[[str], Embedder] | None = None
    vlm_factory: Callable[[str], VLM] | None = None
    contextualizer_factory: Callable[[str], ChunkContextualizer] | None = None
    topic_tagger_factory: Callable[[str], TopicTagger] | None = None

    async def run(self, row: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        """Run a single row through parse, optional enrichments, embed, and store."""

        config = self._effective_indexation_config(row)
        parser = self._select_parser(config)
        chunker = self._select_chunker(config)
        embedder = self._select_embedder(row)
        contextualizer = self._select_contextualizer(config)
        topic_tagger = self._select_topic_tagger(config)

        await parse_stage(row, parser, timeout=self.timeouts.parse)
        if self._should_caption(row, config):
            vlm = self._select_vlm(config)
            if vlm is not None:
                await caption_stage(
                    row,
                    vlm,
                    timeout=self.timeouts.caption,
                    per_image_timeout=self.timeouts.caption_per_image,
                )
        await chunk_stage(row, chunker, timeout=self.timeouts.chunk)
        if contextualizer is not None:
            await contextualize_stage(
                row,
                contextualizer,
                timeout=self.timeouts.contextualize,
                per_chunk_timeout=self.timeouts.contextualize_per_chunk,
            )
        if topic_tagger is not None:
            max_tags = config.max_topic_tags if config is not None else 7
            await topic_tag_stage(
                row,
                topic_tagger,
                max_tags=max_tags,
                timeout=self.timeouts.topic_tag,
            )
        await embed_stage(
            row,
            embedder,
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

    def _effective_indexation_config(self, row: MutableMapping[str, Any]) -> IndexationPipelineConfig | None:
        raw_config = row.get("indexation_config", self.indexation_config)
        if raw_config is None:
            return None
        if isinstance(raw_config, IndexationPipelineConfig):
            return raw_config
        if isinstance(raw_config, dict):
            return IndexationPipelineConfig(**raw_config)
        raise TypeError("indexation_config must be an IndexationPipelineConfig or dict")

    def _select_parser(self, config: IndexationPipelineConfig | None) -> DocumentParser:
        if config is not None and self.parser_factory is not None:
            return self.parser_factory(config.parsing_strategy)
        return self.parser

    def _select_chunker(self, config: IndexationPipelineConfig | None) -> ChunkingStrategy:
        if config is not None and self.chunker_factory is not None:
            return self.chunker_factory(config.chunking)
        return self.chunker

    def _select_embedder(self, row: MutableMapping[str, Any]) -> Embedder:
        embedder_name = row.get("embedder_name")
        if embedder_name and self.embedder_factory is not None:
            return self.embedder_factory(str(embedder_name))
        return self.embedder

    def _select_vlm(self, config: IndexationPipelineConfig | None) -> VLM | None:
        """Pick the captioning VLM instance (availability only — policy is in
        ``_should_caption``)."""
        if config is not None and self.vlm_factory is not None:
            return self.vlm_factory(config.vlm or "default")
        return self.vlm

    def _should_caption(self, row: MutableMapping[str, Any], config: IndexationPipelineConfig | None) -> bool:
        """Decide whether to caption this document's images.

        A standalone image file's caption is its only text content, so it is
        always captioned when a VLM is available (legacy ``ImageLoader``
        parity). Images embedded in other documents are gated by the global
        ``image_captioning`` flag and the per-partition setting.
        """
        document = row.get("document")
        if isinstance(document, Document) and document.content_type is DocumentType.IMAGE:
            return True
        per_partition = config.enable_image_captioning if config is not None else True
        return self.image_captioning and per_partition

    def _select_contextualizer(self, config: IndexationPipelineConfig | None) -> ChunkContextualizer | None:
        if config is not None:
            if not config.enable_contextualization:
                return None
            if self.contextualizer_factory is not None:
                name = config.contextualization_llm or "default"
                try:
                    return self.contextualizer_factory(name)
                except KeyError as exc:
                    # Only an unresolvable endpoint name (the factory raises KeyError)
                    # is skipped — contextualization is an enhancement and must not fail
                    # the file over a missing/typo'd LLM. Any other factory error (prompt
                    # load, bad client config, ...) is a real fault and is left to surface.
                    logger.warning(f"Skipping contextualization: cannot resolve LLM '{name}' ({exc})")
                    return None
        return self.contextualizer

    def _select_topic_tagger(self, config: IndexationPipelineConfig | None) -> TopicTagger | None:
        if config is not None:
            if not config.enable_topic_tagging:
                return None
            if self.topic_tagger_factory is not None:
                name = config.topic_tagging_llm or "default"
                try:
                    return self.topic_tagger_factory(name)
                except KeyError as exc:
                    # Only an unresolvable endpoint name (KeyError) is skipped; any other
                    # factory error is a real fault and is left to surface, not masked.
                    logger.warning(f"Skipping topic tagging: cannot resolve LLM '{name}' ({exc})")
                    return None
        return self.topic_tagger


def build_indexing_pipeline(
    *,
    parser: DocumentParser,
    chunker: ChunkingStrategy,
    embedder: Embedder,
    vector_store: VectorStore,
    vlm: VLM | None = None,
    image_captioning: bool = True,
    contextualizer: ChunkContextualizer | None = None,
    topic_tagger: TopicTagger | None = None,
    timeouts: PipelineTimeouts | None = None,
    indexation_config: IndexationPipelineConfig | None = None,
    parser_factory: Callable[[str], DocumentParser] | None = None,
    chunker_factory: Callable[[Any], ChunkingStrategy] | None = None,
    embedder_factory: Callable[[str], Embedder] | None = None,
    vlm_factory: Callable[[str], VLM] | None = None,
    contextualizer_factory: Callable[[str], ChunkContextualizer] | None = None,
    topic_tagger_factory: Callable[[str], TopicTagger] | None = None,
) -> IndexingPipeline:
    """Build the default sequential indexing pipeline."""

    return IndexingPipeline(
        parser=parser,
        chunker=chunker,
        embedder=embedder,
        vector_store=vector_store,
        vlm=vlm,
        image_captioning=image_captioning,
        contextualizer=contextualizer,
        topic_tagger=topic_tagger,
        timeouts=timeouts or PipelineTimeouts(),
        indexation_config=indexation_config,
        parser_factory=parser_factory,
        chunker_factory=chunker_factory,
        embedder_factory=embedder_factory,
        vlm_factory=vlm_factory,
        contextualizer_factory=contextualizer_factory,
        topic_tagger_factory=topic_tagger_factory,
    )


__all__ = ["IndexingPipeline", "PipelineTimeouts", "build_indexing_pipeline"]
