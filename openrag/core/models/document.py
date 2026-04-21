"""Document — the input to the indexing pipeline."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class DocumentType(str, Enum):
    PDF = "pdf"
    TEXT = "text"
    HTML = "html"
    MARKDOWN = "markdown"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DOCX = "docx"
    PPTX = "pptx"
    DOC = "doc"
    EML = "eml"


class TextBlock(BaseModel):
    """A block of text extracted from a document."""

    text: str
    page_number: int | None = None
    block_type: str = "paragraph"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ImageBlock(BaseModel):
    """An image extracted from a document."""

    image_bytes: bytes = Field(exclude=True, repr=False)
    page_number: int | None = None
    caption: str | None = None
    mime_type: str = "image/png"
    metadata: dict[str, Any] = Field(default_factory=dict)


class Document(BaseModel):
    """A document before or during indexing."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    filename: str = ""
    content_type: DocumentType = DocumentType.TEXT
    text: str | None = None
    raw_bytes: bytes | None = Field(None, exclude=True)
    partition: str = "default"
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @staticmethod
    def detect_content_type(filename: str) -> DocumentType:
        """Detect DocumentType from filename extension."""
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        mapping = {
            "pdf": DocumentType.PDF,
            "txt": DocumentType.TEXT,
            "md": DocumentType.MARKDOWN,
            "html": DocumentType.HTML,
            "htm": DocumentType.HTML,
            "png": DocumentType.IMAGE,
            "jpg": DocumentType.IMAGE,
            "jpeg": DocumentType.IMAGE,
            "mp3": DocumentType.AUDIO,
            "wav": DocumentType.AUDIO,
            "mp4": DocumentType.VIDEO,
            "docx": DocumentType.DOCX,
            "pptx": DocumentType.PPTX,
            "doc": DocumentType.DOC,
            "eml": DocumentType.EML,
        }
        return mapping.get(ext, DocumentType.TEXT)

    @classmethod
    def from_langchain(cls, doc: Any) -> Document:
        """Convert a LangChain Document to a domain Document."""
        metadata = dict(doc.metadata) if doc.metadata else {}
        return cls(
            filename=metadata.pop("source", ""),
            text=doc.page_content,
            partition=metadata.pop("partition", "default"),
            metadata=metadata,
        )

    def to_langchain(self) -> Any:
        """Convert back to a LangChain Document."""
        from langchain_core.documents.base import Document as LCDocument

        metadata = {
            **self.metadata,
            "source": self.filename,
            "partition": self.partition,
        }
        return LCDocument(page_content=self.text or "", metadata=metadata)


class ProcessedDocument(BaseModel):
    """Document after parsing/extraction — contains text blocks and images."""

    document_id: str = ""
    text_blocks: list[TextBlock] = Field(default_factory=list)
    images: list[ImageBlock] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    page_count: int = 0
