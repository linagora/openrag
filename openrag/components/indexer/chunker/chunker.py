from abc import ABC
from pathlib import Path
from typing import Optional

from components.prompts import CHUNK_CONTEXTUALIZER_PMPT
from components.utils import get_vlm_semaphore, load_config
from langchain_core.documents.base import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from omegaconf import OmegaConf
from tqdm.asyncio import tqdm
from utils.logger import get_logger

from ..embeddings import BaseEmbedding, EmbeddingFactory
from .utils import MDElement, chunk_table, get_chunk_page_number, split_md_elements

logger = get_logger()
config = load_config()


BASE_CHUNK_FORMAT = "[CHUNK_START]\n\n{chunk}\n\n[CHUNK_END]"
CHUNK_WITH_CONTEXT_FORMAT = "[CONTEXT]\n\n{chunk_context}\n\n" + BASE_CHUNK_FORMAT


class ChunkContextualizer:
    """Handles contextualization of document chunks."""

    def __init__(self, llm_config: dict):
        self.context_generator = self._create_context_generator(llm_config)

    def _create_context_generator(self, llm_config: dict):
        """Create the context generation chain."""
        try:
            prompt = ChatPromptTemplate.from_template(
                template=CHUNK_CONTEXTUALIZER_PMPT
            )
            return (prompt | ChatOpenAI(**llm_config) | StrOutputParser()).with_retry(
                retry_if_exception_type=(Exception,),
                wait_exponential_jitter=False,
                stop_after_attempt=2,
            )
        except Exception as e:
            raise ValueError(f"Error creating context generator: {e}")

    async def _generate_context(
        self,
        first_chunks: list[Document],
        prev_chunks: list[Document],
        current_chunk: Document,
        source: str,
    ) -> str:
        """Generate context for a given chunk of text."""
        async with get_vlm_semaphore():
            try:
                return await self.context_generator.ainvoke(
                    {
                        "first_chunks": "\n".join(c.page_content for c in first_chunks),
                        "prev_chunks": "\n".join(c.page_content for c in prev_chunks),
                        "chunk": (current_chunk.page_content),
                        "source": source,
                    }
                )
            except Exception as e:
                logger.warning(
                    f"Error contextualizing chunk of document `{source}`: {e}"
                )
                return ""

    async def contextualize_chunks(
        self, chunks: list[Document], metadata: dict = None
    ) -> list[Document]:
        """Contextualize a list of document chunks."""

        if len(chunks) < 2:
            return [
                Document(
                    page_content=BASE_CHUNK_FORMAT.format(chunk=chunk.page_content),
                    metadata=chunk.metadata,
                )
                for chunk in chunks
            ]

        source = chunks[0].metadata.get("source")
        try:
            first_chunks = chunks[:2]
            tasks = [
                self._generate_context(
                    first_chunks=first_chunks,
                    prev_chunks=chunks[max(0, i - 2) : i] if i > 0 else [],
                    current_chunk=chunks[i],
                    source=source,
                )
                for i in range(len(chunks))
            ]

            contexts = await tqdm.gather(
                *tasks,
                total=len(tasks),
                desc=f"Contextualizing chunks of *{Path(source).name}*",
            )

            return [
                Document(
                    page_content=CHUNK_WITH_CONTEXT_FORMAT.format(
                        chunk=chunk.page_content, chunk_context=context
                    ),
                    metadata=chunk.metadata,
                )
                for chunk, context in zip(chunks, contexts)
            ]

        except Exception as e:
            logger.warning(f"Error contextualizing chunks from `{source}`: {e}")
            return chunks


class BaseChunker(ABC):
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
        self, chunks: list[Document], metadata: dict = None
    ) -> list[Document]:
        """Apply contextualization if enabled."""
        if not self.contextual_retrieval:
            return chunks

        return await self.contextualizer.contextualize_chunks(chunks, metadata=metadata)

    def _prepare_md_elements(
        self, content: str
    ) -> tuple[list[MDElement], list[MDElement]]:
        """Prepare and combine markdown elements from raw content."""
        md_elements: list[MDElement] = split_md_elements(content)

        tables_and_images, texts = [], []
        for e in md_elements:
            if e.type in ("table", "image"):
                if (
                    e.type == "image" and "[Image Placeholder]" in e.content
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
        self, content: str, metadata: dict = None, log=None
    ) -> list[Document]:
        log = log or logger
        texts, tables_and_images = self._prepare_md_elements(content=content)
        combined_texts = "\n".join([e.content for e in texts])
        text_chunks = self.split_text(combined_texts)

        # Manage tables and images as separate chunks
        chunks = []
        for e in tables_and_images:
            if e.type == "table" and self._length_function(e.content) > self.chunk_size:
                # Chunk large tables separately
                log.debug(f"Chunking tables from page {e.page_number}")
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

        chunks.sort(key=lambda d: d.metadata.get("page"))
        return chunks

    async def split_document(
        self, doc: Document, task_id: Optional[str] = None
    ) -> list[Document]:
        """Split document into chunks with optional contextualization."""
        metadata = doc.metadata
        log = logger.bind(
            file_id=metadata.get("file_id"),
            partition=metadata.get("partition"),
            task_id=task_id,
        )
        log.info("Starting document chunking")

        # Process document through pipeline
        chunks = self._get_chunks(doc.page_content.strip(), metadata, log=log)

        # Apply contextualization if enabled
        if self.contextual_retrieval:
            log.info("Contextualizing chunks")
        chunks = await self._apply_contextualization(chunks)

        log.info("Document chunking completed")
        return chunks


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

        # Add embeddings if semantic splitter is selected
        if name == "semantic_splitter":
            embedder = EmbeddingFactory.get_embedder(
                embeddings_config=dict(config.embedder)
            )
            chunker_params["embeddings"] = embedder

        chunker_params["llm_config"] = config.vlm
        return chunker_cls(**chunker_params)
