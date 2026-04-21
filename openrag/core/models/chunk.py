"""Chunk — the unit of indexable and retrievable text."""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ChunkType(str, Enum):
    TEXT = "text"
    IMAGE_CAPTION = "image_caption"
    TABLE = "table"
    CONTEXTUALIZED = "contextualized"


class Chunk(BaseModel):
    """A chunk of text extracted from a document, optionally embedded."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    document_id: str = ""
    text: str = ""
    chunk_index: int = 0
    chunk_type: ChunkType = ChunkType.TEXT
    embedding: list[float] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    partition: str = "default"
    page_number: int | None = None
    token_count: int | None = None
    header: str | None = None
    context: str | None = None
    content: str | None = None

    def with_embedding(self, embedding: list[float]) -> Chunk:
        """Return a copy with the embedding set."""
        return self.model_copy(update={"embedding": embedding})

    @classmethod
    def from_langchain(cls, doc: Any) -> Chunk:
        """Convert a LangChain Document to a Chunk.

        Import is deferred to method body so core/ stays pure at import time.
        """
        metadata = dict(doc.metadata) if doc.metadata else {}
        return cls(
            id=metadata.pop("_id", str(uuid.uuid4())),
            document_id=metadata.pop("file_id", ""),
            text=doc.page_content,
            partition=metadata.pop("partition", "default"),
            page_number=metadata.pop("page", None),
            chunk_type=ChunkType(metadata.pop("chunk_type", "text")),
            metadata=metadata,
        )

    def to_langchain(self) -> Any:
        """Convert back to a LangChain Document.

        Import is deferred to method body so core/ stays pure at import time.
        """
        from langchain_core.documents.base import Document

        metadata = {
            **self.metadata,
            "_id": self.id,
            "file_id": self.document_id,
            "partition": self.partition,
            "page": self.page_number,
            "chunk_type": self.chunk_type.value,
        }
        return Document(page_content=self.text, metadata=metadata)
