import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional, Tuple

from components.prompts import CHUNK_CONTEXTUALIZER
from components.utils import get_llm_semaphore, load_config
from langchain_core.documents.base import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from omegaconf import OmegaConf
from tqdm.asyncio import tqdm
from utils.logger import get_logger

from .utils import add_overlap, combine_chunks, combine_md_elements, split_md_elements

logger = get_logger()
config = load_config()


class PageTracker:
    """Handles page number tracking for chunks."""

    def __init__(self):
        self._page_pattern = re.compile(r"\[PAGE_(\d+)\]")

    def get_chunk_page_info(self, chunk_str: str, previous_page: int = 1) -> dict:
        """
        Determine the start and end pages for a text chunk containing [PAGE_N] separators.
        PAGE_N marks the end of page N - text before separator is on page N.
        """
        matches = list(self._page_pattern.finditer(chunk_str))

        if not matches:
            # No separators found - entire chunk is on previous page
            return {"start_page": previous_page, "end_page": previous_page}

        first_match = matches[0]
        last_match = matches[-1]
        last_char_idx = len(chunk_str) - 1

        # Determine start page
        if first_match.start() == 0:
            # Chunk starts with a separator - begins on next page
            start_page = int(first_match.group(1)) + 1
        else:
            # Text precedes first separator - starts on previous page
            start_page = previous_page

        # Determine end page
        if last_match.end() - 1 == last_char_idx:
            # Chunk ends exactly at a separator - ends on that page
            end_page = int(last_match.group(1))
        else:
            # Chunk ends after separator - ends on next page
            end_page = int(last_match.group(1)) + 1

        return {"start_page": start_page, "end_page": end_page}


class ChunkContextualizer:
    """Handles contextualization of document chunks."""

    def __init__(self, llm_config: dict):
        self.context_generator = self._create_context_generator(llm_config)

    def _create_context_generator(self, llm_config: dict):
        """Create the context generation chain."""
        try:
            prompt = ChatPromptTemplate.from_template(template=CHUNK_CONTEXTUALIZER)
            return (prompt | ChatOpenAI(**llm_config) | StrOutputParser()).with_retry(
                retry_if_exception_type=(Exception,),
                wait_exponential_jitter=False,
                stop_after_attempt=2,
            )
        except Exception as e:
            raise ValueError(f"Error creating context generator: {e}")

    async def _generate_context(
        self, first_chunks: str, prev_chunk: str, chunk: str, source: str
    ) -> str:
        """Generate context for a given chunk of text."""
        async with get_llm_semaphore():
            try:
                return await self.context_generator.ainvoke(
                    {
                        "first_chunks": first_chunks,
                        "prev_chunk": prev_chunk,
                        "chunk": chunk,
                        "source": source,
                    }
                )
            except Exception as e:
                logger.warning(
                    f"Error contextualizing chunk of document `{source}`: {e}"
                )
                return ""

    async def contextualize_chunks(self, chunks: List[str], source: str) -> List[str]:
        """Contextualize a list of document chunks."""
        if len(chunks) < 2:
            return chunks

        try:
            first_chunks = "---\n".join(chunks[:2])

            tasks = [
                self._generate_context(
                    first_chunks=first_chunks,
                    prev_chunk="---\n".join(chunks[max(0, i - 2) : i]) if i > 0 else "",
                    chunk=chunks[i],
                    source=source,
                )
                for i in range(len(chunks))
            ]

            contexts = await tqdm.gather(
                *tasks,
                total=len(tasks),
                desc=f"Contextualizing chunks of *{Path(source).name}*",
            )

            # Format contextualized chunks
            chunk_format = "Context: {chunk_context}\n\nChunk:\n{chunk}"
            return [
                chunk_format.format(chunk=chunk, chunk_context=context)
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

        from langchain.text_splitter import TokenTextSplitter

        self.token_text_splitter = TokenTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=self.chunk_overlap,
            encoding_name="cl100k_base",
        )

        self.llm = ChatOpenAI(**llm_config)
        self.contextual_retrieval = contextual_retrieval
        self.page_tracker = PageTracker()

        # Initialize contextualizer only if needed
        self.contextualizer = (
            ChunkContextualizer(llm_config) if contextual_retrieval else None
        )

    def _prepare_markdown_elements(self, content: str) -> List[Tuple[str, str]]:
        """Prepare and combine markdown elements from raw content."""
        splits = split_md_elements(content)
        return combine_md_elements(splits, llm=self.llm, chunk_max_size=self.chunk_size)

    def _add_overlap_to_special_chunks(
        self, splits: List[Tuple[str, str]]
    ) -> List[Tuple[str, str]]:
        """Add overlap to table and image chunks."""
        return add_overlap(
            chunks=splits,
            target_chunk_types=["table", "image"],
            add_before=True,
            add_after=True,
            chunk_overlap=self.chunk_overlap,
        )

    def _split_elements_into_chunks(self, splits: List[Tuple[str, str]]) -> List[str]:
        """Split markdown elements into chunks, only text elements are chunked."""
        chunks = []
        for chunk_type, content in splits:
            if chunk_type == "text":
                chunks.extend(self._split_text(content))
            else:
                chunks.append(content)
        return chunks

    @abstractmethod
    def _split_text(self, text: str) -> List[str]:
        """Split text content into chunks. Must be implemented by subclasses."""
        pass

    async def _apply_contextualization(
        self, chunks: List[str], source: str
    ) -> List[str]:
        """Apply contextualization if enabled."""
        if not self.contextual_retrieval or not self.contextualizer:
            return chunks
        return await self.contextualizer.contextualize_chunks(chunks, source)

    def _create_documents_from_chunks(
        self,
        chunks: List[str],
        chunks_with_context: List[str],
        metadata: dict,
    ) -> List[Document]:
        """Create Document objects from chunks with proper page tracking."""
        documents = []
        prev_page_num = 1

        for chunk, chunk_w_context in zip(chunks, chunks_with_context):
            if not chunk.strip():
                continue

            page_info = self.page_tracker.get_chunk_page_info(chunk, prev_page_num)
            prev_page_num = page_info["end_page"]

            documents.append(
                Document(
                    page_content=chunk_w_context,
                    metadata={**metadata, "page": page_info["start_page"]},
                )
            )

        return documents

    async def split_document(
        self, doc: Document, task_id: Optional[str] = None
    ) -> List[Document]:
        """Split document into chunks with optional contextualization."""
        metadata = doc.metadata
        log = logger.bind(
            file_id=metadata.get("file_id"),
            partition=metadata.get("partition"),
            task_id=task_id,
        )

        log.info("Starting document chunking")
        source = metadata["source"]

        # Process document through pipeline
        splits = self._prepare_markdown_elements(doc.page_content.strip())
        splits = self._add_overlap_to_special_chunks(splits)
        chunks = self._split_elements_into_chunks(splits)

        # Apply contextualization if enabled
        if self.contextual_retrieval:
            log.info("Contextualizing chunks")
        chunks_with_context = await self._apply_contextualization(chunks, source)

        # Create final documents
        documents = self._create_documents_from_chunks(
            chunks, chunks_with_context, metadata
        )

        log.info("Document chunking completed")
        return documents


class TokenSplitter(BaseChunker):
    """Splits documents using token text splitting."""

    def _split_text(self, text: str) -> List[str]:
        """Split text using token-based splitting."""
        return self.token_text_splitter.split_text(text)


class SemanticSplitter(BaseChunker):
    """Splits documents into semantically meaningful chunks."""

    def __init__(
        self,
        chunk_size: int = 200,
        chunk_overlap_rate: float = 0.2,
        llm_config: Optional[dict] = None,
        contextual_retrieval: bool = False,
        embeddings: Optional[OpenAIEmbeddings] = None,
        breakpoint_threshold_amount: int = 85,
        **kwargs,
    ):
        super().__init__(
            chunk_size, chunk_overlap_rate, llm_config, contextual_retrieval, **kwargs
        )

        from langchain_experimental.text_splitter import SemanticChunker

        min_chunk_size_chars = int(chunk_size * 0.5) * 4

        self.semantic_splitter = SemanticChunker(
            embeddings=embeddings,
            buffer_size=1,
            breakpoint_threshold_type="percentile",
            breakpoint_threshold_amount=breakpoint_threshold_amount,
            min_chunk_size=min_chunk_size_chars,
        )

    def _split_text(self, text: str) -> List[str]:
        """Split text using semantic chunking."""
        # Create semantically meaningful chunks
        splits = self.semantic_splitter.split_text(text)

        # Regroup chunks based on token length (to avoid having too small chunks)
        splits = combine_chunks(
            chunks=splits, llm=self.llm, chunk_max_size=self.chunk_size
        )

        # Apply token text splitter to cut down any remaining large chunks
        splits_nested = [self.token_text_splitter.split_text(s) for s in splits]
        return [chunk for sublist in splits_nested for chunk in sublist]


class MarkDownSplitter(BaseChunker):
    """Splits documents based on markdown headers."""

    def __init__(
        self,
        chunk_size: int = 200,
        chunk_overlap_rate: float = 0.2,
        llm_config: Optional[dict] = None,
        contextual_retrieval: bool = False,
        **kwargs,
    ):
        super().__init__(
            chunk_size, chunk_overlap_rate, llm_config, contextual_retrieval, **kwargs
        )

        headers_to_split_on = [
            ("#", "Header 1"),
            ("##", "Header 2"),
            ("###", "Header 3"),
        ]
        from langchain_text_splitters import MarkdownHeaderTextSplitter

        self.md_header_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=headers_to_split_on,
            strip_headers=False,
        )

    def _split_text(self, text: str) -> List[str]:
        """Split text based on markdown headers."""
        # Split by headers
        splits: List[Document] = self.md_header_splitter.split_text(text)

        # Regroup based on token length
        combined = combine_chunks(
            chunks=splits, llm=self.llm, chunk_max_size=self.chunk_size
        )

        # Apply token text splitter to cut down any remaining large chunks
        overlapped_nested = [
            self.token_text_splitter.split_text(chunk) for chunk in combined
        ]
        return [chunk for sublist in overlapped_nested for chunk in sublist]


class ChunkerFactory:
    """Factory for creating chunker instances."""

    CHUNKERS = {
        "token_splitter": TokenSplitter,
        "semantic_splitter": SemanticSplitter,
        "markdown_splitter": MarkDownSplitter,
    }

    @staticmethod
    def create_chunker(config) -> BaseChunker:
        """Create a chunker instance based on configuration."""
        chunker_params = OmegaConf.to_container(config.chunker, resolve=True)
        name = chunker_params.pop("name")

        chunker_cls = ChunkerFactory.CHUNKERS.get(name)
        if not chunker_cls:
            raise ValueError(
                f"Chunker '{name}' not recognized. "
                f"Available: {list(ChunkerFactory.CHUNKERS.keys())}"
            )

        # Add embeddings for semantic splitter
        if name == "semantic_splitter":
            chunker_params["embeddings"] = OpenAIEmbeddings(
                model=config.embedder.get("model_name"),
                base_url=config.embedder.get("base_url"),
                api_key=config.embedder.get("api_key"),
            )

        chunker_params["llm_config"] = config.vlm
        return chunker_cls(**chunker_params)
