from __future__ import annotations

import time
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
        """Run a single row through parse, optional enrichments, embed, and store.

        Each stage is timed and a single structured line is logged per file
        (``ms_parse``, ``ms_chunk``, ``ms_embed``, …), so a slow backend or
        stage is easy to spot — e.g. comparing docling vs marker parse cost.
        Timings are emitted even when a stage fails, so a failure shows how far
        indexing got before erroring.

        CAVEAT — these are **wall-clock**, not CPU time. Files in a batch index
        concurrently and stages share resources (the parser pools, the
        ``asyncio.to_thread`` chunk executor + the GIL, async LLM calls), so a
        stage's value includes time spent **queued/contending** for a slot, not
        just its own work. Under concurrency a value can balloon far past the
        real cost (e.g. the same file chunking in 0.3 s in one run and 67 s in a
        busy batch). They are only a clean per-stage cost at **low concurrency**
        — index one file at a time for accurate numbers; for a batch, read the
        first-finishing (least-contended) file.
        """
        config = self._effective_indexation_config(row)
        parser = self._select_parser(config)
        chunker = self._select_chunker(config)
        embedder = self._select_embedder(row)
        contextualizer, contextualization_llm = self._select_contextualizer(config)
        topic_tagger, topic_tagging_llm = self._select_topic_tagger(config)

        timings: dict[str, float] = {}

        async def _timed(name: str, coro: Any) -> None:
            start = time.perf_counter()
            try:
                await coro
            finally:
                timings[name] = (time.perf_counter() - start) * 1000.0

        try:
            await _timed("parse", parse_stage(row, parser, timeout=self.timeouts.parse))
            # The caption decision needs the parsed document (standalone images
            # always caption), so the VLM is resolved after parse.
            vlm, vlm_name = self._select_vlm(config) if self._should_caption(row, config) else (None, None)
            logger.bind(
                task_id=row.get("task_id"),
                filename=row.get("filename", ""),
                vlm=vlm_name if vlm is not None else None,
                contextualization_llm=contextualization_llm,
                topic_tagging_llm=topic_tagging_llm,
                embedder=str(row.get("embedder_name") or "default"),
            ).debug("model endpoints resolved for indexing (None = stage disabled)")
            if vlm is not None:
                await _timed(
                    "caption",
                    caption_stage(
                        row,
                        vlm,
                        timeout=self.timeouts.caption,
                        per_image_timeout=self.timeouts.caption_per_image,
                    ),
                )
            await _timed("chunk", chunk_stage(row, chunker, timeout=self.timeouts.chunk))
            if contextualizer is not None:
                await _timed(
                    "contextualize",
                    contextualize_stage(
                        row,
                        contextualizer,
                        timeout=self.timeouts.contextualize,
                        per_chunk_timeout=self.timeouts.contextualize_per_chunk,
                    ),
                )
            if topic_tagger is not None:
                max_tags = config.max_topic_tags if config is not None else 7
                await _timed(
                    "topic_tag",
                    topic_tag_stage(
                        row,
                        topic_tagger,
                        max_tags=max_tags,
                        timeout=self.timeouts.topic_tag,
                    ),
                )
            await _timed(
                "embed",
                embed_stage(
                    row,
                    embedder,
                    timeout=self.timeouts.embed,
                    per_chunk_timeout=self.timeouts.embed_per_chunk,
                ),
            )
            await _timed(
                "store",
                store_stage(
                    row,
                    self.vector_store,
                    timeout=self.timeouts.store,
                    per_chunk_timeout=self.timeouts.store_per_chunk,
                ),
            )
            return row
        finally:
            logger.bind(
                task_id=row.get("task_id"),
                filename=row.get("filename", ""),
                n_chunks=len(row.get("chunks") or []),
                **{f"ms_{name}": round(value) for name, value in timings.items()},
                ms_total=round(sum(timings.values())),
            ).info("indexing stage timings (ms)")

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
        # parsing_strategy is None => the preset doesn't override PDF parsing, so
        # defer to the global dispatcher (self.parser), which routes PDFs to the
        # deployment's configured file_loaders.pdf. Only an explicit strategy
        # goes through the factory (and lazily builds that backend's pool).
        if config is not None and self.parser_factory is not None and config.parsing_strategy is not None:
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

    def _select_vlm(self, config: IndexationPipelineConfig | None) -> tuple[VLM | None, str | None]:
        """Pick the captioning VLM instance and its endpoint name for logging
        (availability only — policy is in ``_should_caption``).

        Captioning is an enrichment step, so an unresolvable endpoint name must
        not fail the file — same rationale as ``_select_contextualizer`` /
        ``_select_topic_tagger``. A named VLM whose endpoint was deleted or
        renamed after assignment (the factory raises ``KeyError``) falls back to
        the legacy VLM with a warning instead of breaking indexing for the whole
        partition.
        """
        if config is not None and self.vlm_factory is not None:
            name = config.vlm or "default"
            try:
                return self.vlm_factory(name), name
            except KeyError as exc:
                logger.warning(f"Skipping named VLM: cannot resolve '{name}' ({exc}) — falling back to the default VLM")
                return self.vlm, "default"
        return self.vlm, "default"

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

    def _select_contextualizer(
        self, config: IndexationPipelineConfig | None
    ) -> tuple[ChunkContextualizer | None, str | None]:
        if config is not None:
            if not config.enable_contextualization:
                return None, None
            if self.contextualizer_factory is not None:
                name = config.contextualization_llm or "default"
                try:
                    return self.contextualizer_factory(name), name
                except KeyError as exc:
                    # Only an unresolvable endpoint name (the factory raises KeyError)
                    # is skipped — contextualization is an enhancement and must not fail
                    # the file over a missing/typo'd LLM. Any other factory error (prompt
                    # load, bad client config, ...) is a real fault and is left to surface.
                    logger.warning(f"Skipping contextualization: cannot resolve LLM '{name}' ({exc})")
                    return None, None
        if self.contextualizer is None:
            return None, None
        return self.contextualizer, "default"

    def _select_topic_tagger(self, config: IndexationPipelineConfig | None) -> tuple[TopicTagger | None, str | None]:
        if config is not None:
            if not config.enable_topic_tagging:
                return None, None
            if self.topic_tagger_factory is not None:
                name = config.topic_tagging_llm or "default"
                try:
                    return self.topic_tagger_factory(name), name
                except KeyError as exc:
                    # Only an unresolvable endpoint name (KeyError) is skipped; any other
                    # factory error is a real fault and is left to surface, not masked.
                    logger.warning(f"Skipping topic tagging: cannot resolve LLM '{name}' ({exc})")
                    return None, None
        if self.topic_tagger is None:
            return None, None
        return self.topic_tagger, "default"


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
