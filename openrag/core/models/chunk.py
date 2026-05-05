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


# Pre-Phase-5 chunkers stamped Document metadata with the raw MDElement
# literal (`"image"`) for image elements. Deployments upgraded without
# re-indexing have those values in Milvus; map them to the current enum
# at read time so retrieval doesn't crash on legacy data.
_CHUNK_TYPE_LEGACY_ALIASES = {"image": ChunkType.IMAGE_CAPTION}


def _coerce_chunk_type(value: Any) -> ChunkType:
    if isinstance(value, ChunkType):
        return value
    if value in _CHUNK_TYPE_LEGACY_ALIASES:
        return _CHUNK_TYPE_LEGACY_ALIASES[value]
    try:
        return ChunkType(value)
    except (ValueError, TypeError):
        # Unknown value from upstream/legacy data — fall back to TEXT rather
        # than crash the retrieval call.
        return ChunkType.TEXT


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
        # Milvus assigns the primary key `_id` as INT64 (auto_id), so the value
        # comes back as a Python int. Chunk.id is typed `str`, so coerce here
        # rather than loosen the model — keeps the domain type strict while the
        # store-specific shape is contained in the conversion boundary.
        raw_id = metadata.pop("_id", None)
        chunk_id = str(raw_id) if raw_id is not None else str(uuid.uuid4())
        return cls(
            id=chunk_id,
            document_id=metadata.pop("file_id", ""),
            text=doc.page_content,
            partition=metadata.pop("partition", "default"),
            page_number=metadata.pop("page", None),
            chunk_type=_coerce_chunk_type(metadata.pop("chunk_type", "text")),
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
