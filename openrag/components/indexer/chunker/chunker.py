import asyncio
from typing import Literal, Optional

from components.indexer.utils.text_sanitizer import sanitize_text
from components.prompts import CHUNK_CONTEXTUALIZER_PROMPT
from components.utils import detect_language, get_vlm_semaphore, load_config
from langchain_core.documents.base import Document
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from omegaconf import OmegaConf
from tqdm.asyncio import tqdm
from utils.logger import get_logger

from openrag.consts import IMAGE_PLACEHOLDER

from ..embeddings import BaseEmbedding
from .utils import MDElement, chunk_table, get_chunk_page_number, split_md_elements

logger = get_logger()
config = load_config()

# Timeout for individual chunk contextualization LLM calls (in seconds)
CONTEXTUALIZATION_TIMEOUT = config.chunker.get("contextualization_timeout", 120)
# Maximum concurrent contextualization tasks to prevent system overload
MAX_CONCURRENT_CONTEXTUALIZATION = config.chunker.get(
    "max_concurrent_contextualization", 10
)

BASE_CHUNK_FORMAT = (
    "* filename: {filename}\n\n[CHUNK_START]\n\n{content}\n\n[CHUNK_END]"
)
CHUNK_FORMAT = "[CONTEXT]\n\n{chunk_context}\n\n" + BASE_CHUNK_FORMAT


class ChunkContextualizer:
    """Handles contextualization of document chunks."""

    def __init__(self, llm_config: dict):
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
                output = await asyncio.wait_for(
                    self.context_generator.ainvoke(messages),
                    timeout=CONTEXTUALIZATION_TIMEOUT,
                )
                return output.content
            except asyncio.TimeoutError:
                logger.warning(
                    f"Timeout contextualizing chunk of document `{filename}` "
                    f"after {CONTEXTUALIZATION_TIMEOUT}s"
                )
                return ""
            except Exception as e:
                logger.warning(
                    f"Error contextualizing chunk of document `{filename}`: {e}"
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

            # Process chunks in batches to limit concurrent LLM calls
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
                    desc=f"Contextualizing chunks of *{filename}* "
                    f"[{batch_start + 1}-{batch_end}/{len(chunks)}]",
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


class BaseChunker:
    """Base class for document chunkers with built-in contextualization capability."""

    def __init__(
        self,
        chunk_size: int = 200,
        chunk_overlap_rate: float = 0.2,
        llm_config: Optional[dict] = None,
        contextual_retrieval: bool = False,
        **kwargs,
    ):
        self.chunk_size = chunk_size
        self.chunk_overlap_rate = chunk_overlap_rate
        self.chunk_overlap = int(self.chunk_size * self.chunk_overlap_rate)

        self.llm = ChatOpenAI(**llm_config)
        self._length_function = self.llm.get_num_tokens

        self.text_splitter = None

        self.contextual_retrieval = contextual_retrieval

        # Initialize contextualizer only if needed
        self.contextualizer = (
            ChunkContextualizer(llm_config) if contextual_retrieval else None
        )

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
                    page_content=BASE_CHUNK_FORMAT.format(
                        chunk_context="", filename=filename, content=c.page_content
                    ),
                    metadata=c.metadata,
                )
                for c in chunks
            ]

        return await self.contextualizer.contextualize_chunks(
            chunks, lang=lang, filename=filename
        )

    def _prepare_md_elements(
        self, content: str
    ) -> tuple[list[MDElement], list[MDElement]]:
        """Prepare and combine markdown elements from raw content."""
        md_elements: list[MDElement] = split_md_elements(content)

        tables_and_images, texts = [], []

        for e in md_elements:
            if e.type in ("table", "image"):
                if (
                    e.type == "image" and IMAGE_PLACEHOLDER.lower() in e.content.lower()
                ):  # skip placeholder images
                    continue

                if (
                    self._length_function(e.content) <= 100
                ):  # do not isolate small tables/images
                    texts.append(e)
                else:
                    tables_and_images.append(e)
            else:
                texts.append(e)

        return texts, tables_and_images

    def split_text(self, text: str) -> list[str]:
        """Split text into chunks using the text splitter."""
        if not self.text_splitter:
            logger.warning(
                "Text splitter not initialized. Initializing with default RecursiveCharacterTextSplitter."
            )
            from langchain.text_splitter import RecursiveCharacterTextSplitter

            self.text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=self.chunk_size,
                chunk_overlap=self.chunk_overlap,
                length_function=self._length_function,
            )

        return self.text_splitter.split_text(text)

    def _get_chunks(
        self, content: str, metadata: Optional[dict] = None, log=None
    ) -> list[Document]:
        log = log or logger
        texts, tables_and_images = self._prepare_md_elements(content=content)
        combined_texts = "\n".join([e.content for e in texts])

        # Sanitize the combined text before chunking to remove excessive whitespace
        # and useless characters, which saves tokens and improves quality
        sanitized_texts = sanitize_text(
            combined_texts,
            normalize_whitespace=True,
            remove_control_chars=True,
            remove_zero_width_chars=True,
            max_consecutive_newlines=2,
            normalize_unicode=True,
        )

        text_chunks = self.split_text(sanitized_texts)

        # Manage tables and images as separate chunks
        chunks = []
        for e in tables_and_images:
            if e.type == "table" and self._length_function(e.content) > self.chunk_size:
                # Chunk large tables separately
                subtables = chunk_table(
                    table_element=e,
                    chunk_size=self.chunk_size,
                    length_function=self._length_function,
                )

                s = [
                    Document(
                        page_content=subtable.content.strip(),
                        metadata={
                            **metadata,
                            "page": subtable.page_number,
                            "chunk_type": "table",
                        },
                    )
                    for subtable in subtables
                ]

            else:
                s = [
                    Document(
                        page_content=e.content.strip(),
                        metadata={
                            **metadata,
                            "page": e.page_number,
                            "chunk_type": e.type,
                        },
                    )
                ]
            chunks.extend(s)

        prev_page_num = 1
        for c in text_chunks:
            page_info = get_chunk_page_number(
                chunk_str=c, previous_chunk_ending_page=prev_page_num
            )
            start_page = page_info["start_page"]
            prev_page_num = page_info["end_page"]
            chunks.append(
                Document(
                    page_content=c.strip(),
                    metadata={**metadata, "page": start_page, "chunk_type": "text"},
                )
            )

        if chunks:
            chunks.sort(key=lambda d: d.metadata.get("page"))
            return chunks
        else:
            log.warning(
                "No chunks created. Content is empty or image is not informative."
            )
            return []

    async def split_document(
        self, doc: Document, task_id: Optional[str] = None
    ) -> list[Document]:
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

        # Process document through pipeline
        chunks = self._get_chunks(doc.page_content.strip(), metadata, log=log)

        if chunks:
            # Apply contextualization if enabled
            log.info(
                "Contextualizing chunks",
                apply_contextualization=self.contextual_retrieval,
            )
            chunks = await self._apply_contextualization(
                chunks, lang=detected_lang, filename=filename
            )
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
        super().__init__(
            chunk_size, chunk_overlap_rate, llm_config, contextual_retrieval, **kwargs
        )

        from langchain.text_splitter import RecursiveCharacterTextSplitter

        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            length_function=self._length_function,
            is_separator_regex=True,
            separators=["\n", r"(?<=[\.\?\!])"],
        )


class ChunkerFactory:
    CHUNKERS = {
        "recursive_splitter": RecursiveSplitter,
    }

    @staticmethod
    def create_chunker(
        config: OmegaConf,
        embedder: Optional[BaseEmbedding] = None,
    ) -> BaseChunker:
        # Extract parameters
        chunker_params = OmegaConf.to_container(config.chunker, resolve=True)
        name = chunker_params.pop("name")

        # Initialize and return the chunker
        chunker_cls: BaseChunker = ChunkerFactory.CHUNKERS.get(name)

        if not chunker_cls:
            raise ValueError(
                f"Chunker '{name}' is not recognized."
                f" Available chunkers: {list(ChunkerFactory.CHUNKERS.keys())}"
            )

        chunker_params["llm_config"] = config.vlm
        return chunker_cls(**chunker_params)
