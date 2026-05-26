"""Backward-compatibility shim — chunking primitives delegate to `openrag.core.chunking`.

`ChunkerFactory` is config-driven; the new code uses `chunking_registry`. Both
coexist until Phase 8 cutover.

Scheduled for removal in Phase 12.
"""

from typing import TYPE_CHECKING, Any, ClassVar, Literal

# Side-effect import: pre-loads the indexer-utils submodule so the legacy
# circular import between `components.utils` and `components.indexer.utils.files`
# resolves in the correct order. Removing this line breaks chunker collection.
# Slated to disappear when `components.utils` is split (Phase 6+).
from components.indexer.utils import text_sanitizer as _text_sanitizer  # noqa: F401
from components.prompts import CHUNK_CONTEXTUALIZER_PROMPT
from components.utils import detect_language, get_vlm_semaphore, load_config
from core.chunking.recursive import RecursiveSplitter as _CoreRecursiveSplitter
from core.indexing.contextualize import ChunkContextualizer as _CoreChunkContextualizer
from core.llm.llm import LLM as _CoreLLM
from core.models.chunk import Chunk as _CoreChunk
from core.models.document import ProcessedDocument, TextBlock
from core.prompts.contextualization_builder import wrap_chunk_with_context
from langchain_core.documents.base import Document
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from utils.logger import get_logger

if TYPE_CHECKING:
    from components.indexer.embeddings import BaseEmbedding
    from langchain_openai import ChatOpenAI

logger = get_logger()
config = load_config()

CONTEXTUALIZATION_TIMEOUT = config.chunker.contextualization_timeout
MAX_CONCURRENT_CONTEXTUALIZATION = config.chunker.max_concurrent_contextualization


class _LangChainLLMAdapter(_CoreLLM):
    """Wraps a LangChain ``ChatOpenAI`` so it satisfies the core ``LLM`` ABC."""

    _ROLE_MAP: ClassVar[dict] = {"user": HumanMessage, "system": SystemMessage, "assistant": AIMessage}

    def __init__(self, lc_llm: "ChatOpenAI") -> None:
        self._llm = lc_llm

    async def generate(self, prompt: str, **kwargs) -> str:
        out = await self._llm.ainvoke(prompt)
        return out.content if hasattr(out, "content") else str(out)

    async def chat(self, messages: list[dict[str, str]], **kwargs) -> str:
        lc_msgs = [self._ROLE_MAP[m["role"]](content=m["content"]) for m in messages]
        out = await self._llm.ainvoke(lc_msgs)
        return out.content

    async def stream_chat(self, messages: list[dict[str, str]], **kwargs) -> Any:
        pass  # Not implemented since the contextualizer never streams.


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
        from langchain_openai import ChatOpenAI

        self.chunk_size = chunk_size
        self.chunk_overlap_rate = chunk_overlap_rate
        self.chunk_overlap = int(self.chunk_size * self.chunk_overlap_rate)

        self.llm = ChatOpenAI(**llm_config)
        self._length_function = self.llm.get_num_tokens

        self._core_splitter: _CoreRecursiveSplitter | None = None

        self.contextual_retrieval = contextual_retrieval
        if contextual_retrieval:
            _lc_llm = ChatOpenAI(**{**llm_config, "timeout": CONTEXTUALIZATION_TIMEOUT})
            self.contextualizer: _CoreChunkContextualizer | None = _CoreChunkContextualizer(
                llm=_LangChainLLMAdapter(_lc_llm),
                system_prompt=CHUNK_CONTEXTUALIZER_PROMPT,
                timeout_seconds=CONTEXTUALIZATION_TIMEOUT,
                max_concurrent=MAX_CONCURRENT_CONTEXTUALIZATION,
                semaphore=get_vlm_semaphore(),
            )
        else:
            self.contextualizer = None

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
                    page_content=wrap_chunk_with_context(c.page_content, filename),
                    metadata=c.metadata,
                )
                for c in chunks
            ]

        core_chunks = [_CoreChunk.from_langchain(c) for c in chunks]
        contextualized = await self.contextualizer.contextualize(core_chunks, filename=filename, lang=lang)
        return [c.to_langchain(with_id=False) for c in contextualized]

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
        embedder: "BaseEmbedding | None" = None,
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
