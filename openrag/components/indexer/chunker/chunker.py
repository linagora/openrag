"""Backward-compatibility shim — chunking primitives delegate to `openrag.core.chunking`.

Phase 5B/5.15 status:

* `BaseChunker._get_chunks` / `_prepare_md_elements` / `split_text` →
  delegated to a held `core.chunking.recursive.RecursiveSplitter` instance
  via a `Document` ↔ `ProcessedDocument` ↔ `Chunk` adapter.
* `ChunkContextualizer` and the `split_document()` orchestration sit
  outside Phase 5B (they belong to 5D / Phase 8). They stay here until
  Phase 5D ships `core/indexing/contextualize.py`.
* `ChunkerFactory` is config-driven; the new code uses
  `chunking_registry`. Both coexist until Phase 8 cutover.

Scheduled for removal in Phase 12.
"""

from typing import Literal

import openai

# Side-effect import: pre-loads the indexer-utils submodule so the legacy
# circular import between `components.utils` and `components.indexer.utils.files`
# resolves in the correct order. Removing this line breaks chunker collection.
# Slated to disappear when `components.utils` is split (Phase 6+).
from components.indexer.utils import text_sanitizer as _text_sanitizer  # noqa: F401
from components.prompts import CHUNK_CONTEXTUALIZER_PROMPT
from components.utils import detect_language, get_vlm_semaphore, load_config
from langchain_core.documents.base import Document
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from tqdm.asyncio import tqdm
from utils.logger import get_logger

from openrag.core.chunking.recursive import RecursiveSplitter as _CoreRecursiveSplitter
from openrag.core.models.document import ProcessedDocument, TextBlock

from ..embeddings import BaseEmbedding

logger = get_logger()
config = load_config()

CONTEXTUALIZATION_TIMEOUT = config.chunker.contextualization_timeout
MAX_CONCURRENT_CONTEXTUALIZATION = config.chunker.max_concurrent_contextualization

BASE_CHUNK_FORMAT = "* filename: {filename}\n\n[CHUNK_START]\n\n{content}\n\n[CHUNK_END]"
CHUNK_FORMAT = "[CONTEXT]\n\n{chunk_context}\n\n" + BASE_CHUNK_FORMAT


class ChunkContextualizer:
    """Handles contextualization of document chunks.

    Stays in `components/` until Phase 5D moves the orchestration into
    `core/indexing/contextualize.py`. The pure prompt-builders for the
    LLM call already live at `core.prompts.contextualization_builder`.
    """

    def __init__(self, llm_config: dict):
        llm_config: dict = dict(llm_config)
        llm_config.update({"timeout": CONTEXTUALIZATION_TIMEOUT})
        self.context_generator = ChatOpenAI(**llm_config)

    async def _generate_context(
        self,
        first_chunks: list[Document],
        prev_chunks: list[Document],
        current_chunk: Document,
        lang: Literal["fr", "en"] = "en",
    ) -> str:
        """Generate context for a given chunk of text."""
        filename = first_chunks[0].metadata.get("source", "unknown")

        user_msg = f"""
        Here is the context to consider for generating the context:
        - Filename: {filename}
        - First chunks:
        {"\n--\n".join(c.page_content for c in first_chunks)}

        - Previous chunks:
        {"\n--\n".join(c.page_content for c in prev_chunks)}

        Here is the current chunk to contextualize strictly in this {lang} language:
        - Current chunk:

        {current_chunk.page_content}
        """
        async with get_vlm_semaphore():
            try:
                messages = [
                    SystemMessage(content=CHUNK_CONTEXTUALIZER_PROMPT),
                    HumanMessage(content=user_msg),
                ]
                output = await self.context_generator.ainvoke(messages)
                return output.content
            except openai.APITimeoutError:
                logger.warning(
                    f"OpenAI API timeout contextualizing chunk after {CONTEXTUALIZATION_TIMEOUT}s",
                    filename=filename,
                )
                return ""
            except Exception as e:
                logger.warning(
                    "Error contextualizing chunk of document",
                    filename=filename,
                    error=str(e),
                )
                return ""

    async def contextualize_chunks(
        self,
        chunks: list[Document],
        lang: Literal["fr", "en"] = "en",
        filename: str = "",
    ) -> list[Document]:
        """Contextualize a list of document chunks.

        Processes chunks in batches to prevent overwhelming the system with
        too many concurrent LLM requests.
        """
        try:
            first_chunks = chunks[:2]
            contexts = []
            batch_size = MAX_CONCURRENT_CONTEXTUALIZATION

            for batch_start in range(0, len(chunks), batch_size):
                batch_end = min(batch_start + batch_size, len(chunks))
                batch_tasks = [
                    self._generate_context(
                        first_chunks=first_chunks,
                        prev_chunks=chunks[max(0, i - 2) : i] if i > 0 else [],
                        current_chunk=chunks[i],
                        lang=lang,
                    )
                    for i in range(batch_start, batch_end)
                ]

                batch_contexts = await tqdm.gather(
                    *batch_tasks,
                    total=len(batch_tasks),
                    desc=f"Contextualizing chunks of *{filename}* [{batch_start + 1}-{batch_end}/{len(chunks)}]",
                )
                contexts.extend(batch_contexts)

            return [
                Document(
                    page_content=CHUNK_FORMAT.format(
                        content=chunk.page_content,
                        chunk_context=context,
                        filename=filename,
                    ),
                    metadata=chunk.metadata,
                )
                for chunk, context in zip(chunks, contexts, strict=True)
            ]

        except Exception as e:
            logger.warning(f"Error contextualizing chunks from `{filename}`: {e}")
            return chunks


def _chunks_to_documents(chunks: list, base_metadata: dict) -> list[Document]:
    """Convert a list of core domain Chunks into legacy LangChain Documents.

    Reproduces the legacy metadata shape: `page`, `chunk_type`, plus the
    document/partition keys the legacy code stamps onto every chunk.
    """
    out: list[Document] = []
    for c in chunks:
        meta = dict(c.metadata)
        meta.update(
            {
                "file_id": c.document_id,
                "partition": c.partition,
                "page": c.page_number,
                "chunk_type": c.chunk_type.value,
            }
        )
        # Preserve legacy keys that weren't lifted into core fields.
        for k, v in base_metadata.items():
            meta.setdefault(k, v)
        out.append(Document(page_content=c.text, metadata=meta))
    return out


class BaseChunker:
    """Legacy chunker shell — markdown-aware splitting delegated to core.

    Subclasses configure ``self._core_splitter`` (a
    `core.chunking.recursive.RecursiveSplitter`) in their `__init__`.
    """

    def __init__(
        self,
        chunk_size: int = 200,
        chunk_overlap_rate: float = 0.2,
        llm_config: dict | None = None,
        contextual_retrieval: bool = False,
        **kwargs,
    ):
        self.chunk_size = chunk_size
        self.chunk_overlap_rate = chunk_overlap_rate
        self.chunk_overlap = int(self.chunk_size * self.chunk_overlap_rate)

        self.llm = ChatOpenAI(**llm_config)
        self._length_function = self.llm.get_num_tokens

        self._core_splitter: _CoreRecursiveSplitter | None = None

        self.contextual_retrieval = contextual_retrieval
        self.contextualizer = ChunkContextualizer(llm_config) if contextual_retrieval else None

    async def _apply_contextualization(
        self,
        chunks: list[Document],
        lang: Literal["en", "fr"] = "en",
        filename: str = "",
    ) -> list[Document]:
        """Apply contextualization if enabled."""
        if not self.contextual_retrieval or len(chunks) < 2:
            return [
                Document(
                    page_content=BASE_CHUNK_FORMAT.format(chunk_context="", filename=filename, content=c.page_content),
                    metadata=c.metadata,
                )
                for c in chunks
            ]

        return await self.contextualizer.contextualize_chunks(chunks, lang=lang, filename=filename)

    def _get_chunks(self, content: str, metadata: dict | None = None, log=None) -> list[Document]:
        log = log or logger
        metadata = metadata or {}
        partition = metadata.get("partition", "default")

        doc = ProcessedDocument(
            document_id=metadata.get("file_id", ""),
            text_blocks=[TextBlock(text=content)],
            metadata=metadata,
        )
        chunks = self._core_splitter.chunk(doc, partition=partition)
        if not chunks:
            log.warning("No chunks created. Content is empty or image is not informative.")
            return []
        return _chunks_to_documents(chunks, base_metadata=metadata)

    async def split_document(self, doc: Document, task_id: str | None = None) -> list[Document]:
        """Split document into chunks with optional contextualization."""
        metadata = doc.metadata
        filename = metadata.get("filename", "")
        log = logger.bind(
            file_id=metadata.get("file_id"),
            partition=metadata.get("partition"),
            task_id=task_id,
        )
        log.info("Starting document chunking")

        detected_lang = detect_language(text=doc.page_content)

        chunks = self._get_chunks(doc.page_content.strip(), metadata, log=log)

        if chunks:
            log.info(
                "Contextualizing chunks",
                apply_contextualization=self.contextual_retrieval,
            )
            chunks = await self._apply_contextualization(chunks, lang=detected_lang, filename=filename)
            log.info("Document chunking completed")
            return chunks
        else:
            return []


class RecursiveSplitter(BaseChunker):
    def __init__(
        self,
        chunk_size=200,
        chunk_overlap_rate=0.2,
        llm_config=None,
        contextual_retrieval=False,
        **kwargs,
    ):
        super().__init__(chunk_size, chunk_overlap_rate, llm_config, contextual_retrieval, **kwargs)
        self._core_splitter = _CoreRecursiveSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap_rate=self.chunk_overlap_rate,
            length_function=self._length_function,
        )


class ChunkerFactory:
    CHUNKERS = {
        "recursive_splitter": RecursiveSplitter,
    }

    @staticmethod
    def create_chunker(
        config,
        embedder: BaseEmbedding | None = None,
    ) -> BaseChunker:
        chunker_params = config.chunker.model_dump()
        name = chunker_params.pop("name")

        chunker_cls: BaseChunker = ChunkerFactory.CHUNKERS.get(name)

        if not chunker_cls:
            raise ValueError(
                f"Chunker '{name}' is not recognized. Available chunkers: {list(ChunkerFactory.CHUNKERS.keys())}"
            )

        chunker_params["llm_config"] = config.vlm.model_dump()
        return chunker_cls(**chunker_params)
