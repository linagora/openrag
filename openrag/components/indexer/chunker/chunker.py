import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from components.prompts import CHUNK_CONTEXTUALIZER
from components.utils import get_vlm_semaphore, load_config
from langchain_core.documents.base import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
from omegaconf import OmegaConf
from tqdm.asyncio import tqdm
from utils.logger import get_logger

from ..embeddings import BaseEmbedding, EmbeddingFactory
from .utils import add_overlap, combine_chunks, combine_md_elements, split_md_elements

logger = get_logger()
config = load_config()


class BaseChunker(ABC):
    """Base class for document chunkers with built-in contextualization capability."""

    def __init__(
        self,
        chunk_size: int = 200,
        chunk_overlap_rate: int = 0.2,
        llm_config: Optional[dict] = None,
        contextual_retrieval: bool = False,
        **kwargs,
    ):
        self.chunk_size = chunk_size
        self.chunk_overlap_rate = chunk_overlap_rate
        self.chunk_overlap = int(self.chunk_size * self.chunk_overlap_rate)

        self.contextual_retrieval = contextual_retrieval
        self.context_generator = None
        self._page_pattern = re.compile(r"\[PAGE_(\d+)\]")

        self.llm = ChatOpenAI(**llm_config)

        try:
            if self.contextual_retrieval:
                prompt = ChatPromptTemplate.from_template(template=CHUNK_CONTEXTUALIZER)
                self.context_generator = (
                    prompt | ChatOpenAI(**llm_config) | StrOutputParser()
                ).with_retry(
                    retry_if_exception_type=(Exception,),
                    wait_exponential_jitter=False,
                    stop_after_attempt=2,
                )

        except Exception as e:
            raise ValueError("Error with context_generator: {}".format(e))

    async def _generate_context(
        self, first_chunks: str, prev_chunk: str, chunk: str, source: str
    ) -> str:
        """Generate context for a given chunk of text."""
        async with get_vlm_semaphore():
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
                    f"Error when contextualizing a chunk of document `{source}`: {e}"
                )
                return ""

    async def _contextualize_chunks(self, chunks: list[str], source: str) -> list[str]:
        """Contextualize a list of document chunks."""
        if not self.contextual_retrieval or len(chunks) < 2:
            return chunks

        try:
            tasks = []
            for i in range(len(chunks)):
                prev_chunk = "---\n".join(chunks[max(0, i - 2) : i]) if i > 0 else ""
                curr_chunk = chunks[i]
                first_chunks = "---\n".join(chunks[:2])  # first two chunks

                tasks.append(
                    self._generate_context(
                        first_chunks=first_chunks,
                        prev_chunk=prev_chunk,
                        chunk=curr_chunk,
                        source=source,
                    )
                )

            contexts = await tqdm.gather(
                *tasks,
                total=len(tasks),
                desc=f"Contextualizing chunks of *{Path(source).name}*",
            )

            # Format contextualized chunks
            chunk_format = """Context: {chunk_context}\n\nChunk:\n{chunk}"""
            contexts = [
                chunk_format.format(
                    chunk=chunk, chunk_context=context, source=Path(source).name
                )
                for chunk, context in zip(chunks, contexts)
            ]

            return contexts

        except Exception as e:
            logger.warning(f"Error when contextualizing chunks from `{source}`: {e}")
            return chunks

    def _get_chunk_page_info(self, chunk_str: str, previous_page=1):
        """
        Determine the start and end pages for a text chunk containing [PAGE_N] separators.
        PAGE_N marks the end of page N - text before separator is on page N.
        """
        # Find all page separator matches in the chunk
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

    @abstractmethod
    async def split_document(self, docs: list[Document], task_id: str = None):
        pass


class RecursiveSplitter(BaseChunker):
    """RecursiveSplitter splits documents into chunks using recursive character splitting."""

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

        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            length_function=lambda x: self.llm.get_num_tokens(x)
            if self.llm
            else len(x),
        )

    async def split_document(self, doc: Document, task_id: str = None):
        metadata = doc.metadata
        log = logger.bind(
            file_id=metadata.get("file_id"),
            partition=metadata.get("partition"),
            task_id=task_id,
        )
        log.info("Starting document chunking")
        source = metadata["source"]

        # Split the document into chunks of text, tables, and images
        all_content = doc.page_content.strip()
        splits = split_md_elements(all_content)
        splits = combine_md_elements(
            splits, llm=self.llm, chunk_max_size=self.chunk_size
        )  # cause some md elements are too small

        # Add overlap to image and table chunks
        splits = add_overlap(
            chunks=splits,
            target_chunk_types=["table", "image"],
            add_before=True,
            add_after=True,
            chunk_overlap=self.chunk_overlap,
        )

        # only split text elements into chunks
        chunks = []
        for chunk_type, content in splits:
            if chunk_type == "text":
                chunks.extend(self.splitter.split_text(content))
            else:
                chunks.append(content)

        chunks_w_context = chunks  # Default to original chunks if no contextualization

        if self.contextual_retrieval:
            log.info("Contextualizing chunks")
            chunks_w_context = await self._contextualize_chunks(chunks, source=source)

        filtered_chunks = []
        prev_page_num = 1
        for chunk, chunk_w_context in zip(chunks, chunks_w_context):
            if not chunk.strip():  # skip empty chunks
                continue

            page_info = self._get_chunk_page_info(
                chunk_str=chunk, previous_page=prev_page_num
            )
            start_page = page_info["start_page"]
            end_page = page_info["end_page"]
            prev_page_num = end_page
            filtered_chunks.append(
                Document(
                    page_content=chunk_w_context,
                    metadata={**metadata, "page": start_page},
                )
            )
        log.info("Document chunking completed")
        return filtered_chunks


class SemanticSplitter(BaseChunker):
    """SemanticSplitter splits documents into semantically meaningful chunks."""

    def __init__(
        self,
        chunk_size=200,
        chunk_overlap_rate=0.2,
        llm_config=None,
        contextual_retrieval=False,
        embeddings: Optional[BaseEmbedding] = None,
        breakpoint_threshold_amount: int = 85,
    ):
        super().__init__(
            chunk_size, chunk_overlap_rate, llm_config, contextual_retrieval
        )
        from langchain_experimental.text_splitter import SemanticChunker

        min_chunk_size_chars = (
            int(chunk_size * 0.5) * 4
        )  # 1 token = 4 characters on average

        self.semantic_splitter = SemanticChunker(
            embeddings=embeddings,
            buffer_size=1,
            breakpoint_threshold_type="percentile",
            breakpoint_threshold_amount=breakpoint_threshold_amount,
            min_chunk_size=min_chunk_size_chars,
        )

        self.recursive_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            length_function=lambda x: self.llm.get_num_tokens(x)
            if self.llm
            else len(x),
        )

    def split_text(self, text: str):
        # split sematically meaningful chunks
        splits = self.semantic_splitter.split_text(text)

        # regrouping chunks based on token length
        splits = combine_chunks(
            chunks=splits, llm=self.llm, chunk_max_size=self.chunk_size
        )

        # apply recursive character splitter to each chunk (this would add overlapping between text chunks)
        splits_l = [self.recursive_splitter.split_text(s) for s in splits]
        splits = sum(splits_l, [])
        return splits

    async def split_document(self, doc: Document, task_id: str = None):
        metadata = doc.metadata
        log = logger.bind(
            file_id=metadata.get("file_id"),
            partition=metadata.get("partition"),
            task_id=task_id,
        )
        log.info("Starting document chunking")
        source = metadata["source"]

        # Split the document into chunks of text, tables, and images
        all_content = doc.page_content.strip()
        splits = split_md_elements(all_content)
        splits = combine_md_elements(
            splits, llm=self.llm, chunk_max_size=self.chunk_size
        )

        # Add overlap image and table chunks
        splits = add_overlap(
            chunks=splits,
            target_chunk_types=["table", "image"],
            add_before=True,
            add_after=True,
            chunk_overlap=self.chunk_overlap,
        )

        # only split text elements into chunks
        chunks = []
        for chunk_type, content in splits:
            if chunk_type == "text":
                chunks.extend(self.split_text(content))
            else:
                chunks.append(content)

        # regrouping chunks based on token length
        chunks = combine_chunks(
            chunks=chunks, llm=self.llm, chunk_max_size=self.chunk_size
        )

        chunks_w_context = chunks  # Default to original chunks if no contextualization
        if self.contextual_retrieval:
            log.info("Contextualizing chunks")
            chunks_w_context = await self._contextualize_chunks(chunks, source=source)

        filtered_chunks = []
        prev_page_num = 1
        for chunk, chunk_w_context in zip(chunks, chunks_w_context):
            if not chunk.strip():  # skip empty chunks
                continue

            page_info = self._get_chunk_page_info(
                chunk_str=chunk, previous_page=prev_page_num
            )
            start_page = page_info["start_page"]
            end_page = page_info["end_page"]
            prev_page_num = end_page
            filtered_chunks.append(
                Document(
                    page_content=chunk_w_context,
                    metadata={**metadata, "page": start_page},
                )
            )
        log.info("Document chunking completed")
        return filtered_chunks


class MarkDownSplitter(BaseChunker):
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
        headers_to_split_on = [
            ("#", "Header 1"),
            ("##", "Header 2"),
            ("###", "Header 3"),
            # ("####", "Header 4"),
        ]
        self.md_header_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=headers_to_split_on,
            strip_headers=False,
        )

        self.recursive_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            length_function=lambda x: self.llm.get_num_tokens(x),
        )

    def split_md_chunks(self, text: str) -> list[str]:
        # split the text into chunks based on headers
        splits: list[Document] = self.md_header_splitter.split_text(text)

        # regrouping chunks based on token length
        combined_elements = combine_chunks(
            chunks=splits, llm=self.llm, chunk_max_size=self.chunk_size
        )

        # use recursive splitter to further split the chunks (this would add overlapping between text chunks)
        overlapped_elements = list(
            map(lambda x: self.recursive_splitter.split_text(x), combined_elements)
        )
        return sum(overlapped_elements, [])

    async def split_document(self, doc: Document, task_id: str = None):
        metadata = doc.metadata
        log = logger.bind(
            file_id=metadata.get("file_id"),
            partition=metadata.get("partition"),
            task_id=task_id,
        )
        log.info("Starting document chunking")
        source = metadata["source"]

        # Split the document into chunks of text, tables, and images
        all_content = doc.page_content.strip()
        splits = split_md_elements(all_content)
        splits = combine_md_elements(
            splits, llm=self.llm, chunk_max_size=self.chunk_size
        )

        # Add overlap image and table chunks
        splits = add_overlap(
            chunks=splits,
            target_chunk_types=["table", "image"],
            add_before=True,
            add_after=True,
            chunk_overlap=self.chunk_overlap,
        )

        # only split text elements into chunks
        chunks = []
        for chunk_type, content in splits:
            if chunk_type == "text":
                chunks.extend(self.split_md_chunks(content))
            else:
                chunks.append(content)

        chunks_w_context = chunks  # Default to original chunks if no contextualization
        if self.contextual_retrieval:
            log.info("Contextualizing chunks")
            chunks_w_context = await self._contextualize_chunks(chunks, source=source)

        filtered_chunks = []
        prev_page_num = 1
        for chunk, chunk_w_context in zip(chunks, chunks_w_context):
            if not chunk.strip():  # skip empty chunks
                continue

            page_info = self._get_chunk_page_info(
                chunk_str=chunk, previous_page=prev_page_num
            )
            start_page = page_info["start_page"]
            end_page = page_info["end_page"]
            prev_page_num = end_page
            filtered_chunks.append(
                Document(
                    page_content=chunk_w_context,
                    metadata={**metadata, "page": start_page},
                )
            )
        log.info("Document chunking completed")
        return filtered_chunks


class ChunkerFactory:
    CHUNKERS = {
        "recursive_splitter": RecursiveSplitter,
        "semantic_splitter": SemanticSplitter,
        "markdown_splitter": MarkDownSplitter,
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
