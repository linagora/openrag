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
from services.workers.stages._common import run_with_optional_timeout
from services.workers.stages.caption import caption_stage
from services.workers.stages.chunk import chunk_stage
from services.workers.stages.contextualize import contextualize_stage
from services.workers.stages.embed import embed_stage
from services.workers.stages.parse import parse_stage
from services.workers.stages.store import store_stage
from services.workers.stages.topic_tag import topic_tag_stage

logger = get_logger()

REPLACE_OLD_CHUNK_COLLECTION_ROW_KEY = "_replace_old_chunk_collection"
REPLACE_OLD_CHUNK_IDS_ROW_KEY = "_replace_old_chunk_ids"


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
    defer_replace_cleanup: bool = False

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
            # Re-index (``replace=True``) is insert-before-delete: snapshot the
            # file's existing chunk ids *before* the store stage inserts the new
            # set, then delete exactly that old set after a successful insert.
            # Worker pipelines defer that delete until after the catalog row is
            # successfully written; direct pipeline callers clean it up here.
            # The Milvus collection is ``auto_id``, so a plain insert can never
            # overwrite the previous chunks — without this cleanup every re-index
            # duplicates the whole file (#657). Insert-before-delete also means a
            # re-index that fails before/at store leaves the old chunks intact
            # (never an empty window).
            #
            # KNOWN SEAMS (Milvus has no transactions — both are strictly better
            # than the pre-fix behaviour, which duplicated on every re-index):
            #   * Not atomic under concurrency. Two overlapping re-indexes of the
            #     *same* file snapshot the same old ids and both keep their new
            #     set, leaving duplicates. Serializing replace per (partition,
            #     file_id) belongs with the durable job/lifecycle work (#658/#660).
            #   * A crash after store but before cleanup can orphan the old
            #     chunks with no reconciler yet — the reconciliation job is
            #     tracked in #658/#660.
            row.pop(REPLACE_OLD_CHUNK_COLLECTION_ROW_KEY, None)
            row.pop(REPLACE_OLD_CHUNK_IDS_ROW_KEY, None)
            replace = bool(row.get("replace"))
            old_chunk_ids = await self._existing_chunk_ids(row) if replace else []
            await _timed(
                "store",
                store_stage(
                    row,
                    self.vector_store,
                    timeout=self.timeouts.store,
                    per_chunk_timeout=self.timeouts.store_per_chunk,
                ),
            )
            # BUG (#657 follow-up): ``store_stage`` completes successfully even
            # when it stores zero chunks — an empty/whitespace-only file, a
            # parser that extracts no text, etc. all legitimately chunk down to
            # ``[]`` without raising (see chunk_stage / BaseChunker.chunk). If the
            # delete below fired on ``old_chunk_ids`` alone, a re-index that
            # produces no new chunks would delete the *entire* previous chunk set
            # and leave the file with zero chunks in Milvus — worse than the
            # pre-fix duplication bug, and a violation of the "no empty window"
            # guarantee this whole insert-before-delete design is built on.
            # Gating on ``stored_count`` ensures cleanup only runs once we know
            # the new set actually replaced the old one.
            if replace and old_chunk_ids and row.get("stored_count"):
                if self.defer_replace_cleanup:
                    row[REPLACE_OLD_CHUNK_COLLECTION_ROW_KEY] = "default"
                    row[REPLACE_OLD_CHUNK_IDS_ROW_KEY] = old_chunk_ids
                else:
                    await self._delete_replaced_chunks(row, old_chunk_ids)
            return row
        finally:
            logger.bind(
                task_id=row.get("task_id"),
                filename=row.get("filename", ""),
                n_chunks=len(row.get("chunks") or []),
                **{f"ms_{name}": round(value) for name, value in timings.items()},
                ms_total=round(sum(timings.values())),
            ).info("indexing stage timings (ms)")

    async def _existing_chunk_ids(self, row: MutableMapping[str, Any]) -> list[str]:
        """Snapshot the chunk ids currently stored for this file (re-index only).

        Returns an empty list — skipping stale-chunk cleanup — when the target
        can't be resolved or the lookup fails. A snapshot failure must never lose
        the newly-indexed chunks: leftover duplicates are recoverable, deleting
        blindly is not.
        """
        file_id, partition = _replace_target(row)
        if not file_id or not partition:
            return []

        async def _lookup() -> list[str]:
            if not await self.vector_store.collection_exists("default"):
                return []
            return await self.vector_store.query_ids_by_filter("default", {"partition": partition, "file_id": file_id})

        try:
            # Bound the lookup by the store budget so a stalled Milvus can't hang
            # replace indexing indefinitely (a timeout just skips cleanup).
            return await run_with_optional_timeout(_lookup, self.timeouts.store)
        except Exception as exc:  # noqa: BLE001 - cleanup lookup must not fail the index
            logger.bind(task_id=row.get("task_id"), file_id=file_id, partition=partition).warning(
                f"re-index: could not snapshot existing chunks; skipping stale-chunk cleanup: {exc}"
            )
            return []

    async def _delete_replaced_chunks(self, row: MutableMapping[str, Any], ids: list[str]) -> None:
        """Delete the pre-re-index chunk set after the new chunks are stored."""
        try:
            deleted = await run_with_optional_timeout(
                lambda: self.vector_store.delete(ids, "default"), self.timeouts.store
            )
            logger.bind(task_id=row.get("task_id")).debug(f"re-index: removed {deleted} stale chunk(s) after replace")
        except Exception as exc:  # noqa: BLE001 - new chunks are stored; cleanup is best-effort
            logger.bind(task_id=row.get("task_id")).error(
                f"re-index: stored new chunks but failed to delete {len(ids)} stale chunk(s); "
                f"duplicates remain until reconciliation: {exc}"
            )

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
        parity). Images embedded in other documents are gated solely by the
        per-partition ``enable_image_captioning`` setting — the deployment's
        ``IMAGE_CAPTIONING`` env flag only seeds that setting's default on the
        ``default`` preset at first boot (see ``PresetService._finalize_seed``);
        it is not re-checked here, so a preset can enable/disable captioning
        independent of the current env value.
        """
        document = row.get("document")
        if isinstance(document, Document) and document.content_type is DocumentType.IMAGE:
            return True
        return config.enable_image_captioning if config is not None else True

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
    defer_replace_cleanup: bool = False,
) -> IndexingPipeline:
    """Build the default sequential indexing pipeline."""

    return IndexingPipeline(
        parser=parser,
        chunker=chunker,
        embedder=embedder,
        vector_store=vector_store,
        vlm=vlm,
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
        defer_replace_cleanup=defer_replace_cleanup,
    )


def _replace_target(row: MutableMapping[str, Any]) -> tuple[str | None, str | None]:
    """Resolve ``(file_id, partition)`` for a re-index row.

    ``file_id`` is the document's identity (``Document.id`` == ``Chunk.file_id``
    in Milvus); ``partition`` scopes the delete so only this file's chunks in
    this partition are ever touched.
    """
    document = row.get("document")
    file_id = getattr(document, "id", None)
    partition = row.get("partition") or getattr(document, "partition", None)
    return file_id, partition


__all__ = ["IndexingPipeline", "PipelineTimeouts", "build_indexing_pipeline"]
