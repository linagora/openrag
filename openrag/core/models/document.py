"""Document — the input to the indexing pipeline."""

from __future__ import annotations

import asyncio
import base64
import os
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


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


class SourceFragment(BaseModel):
    """A trace from normalized content back to one untouched parser block."""

    source_block_index: int = Field(ge=0)
    page_number: int = Field(ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    bbox: tuple[float, float, float, float] | None = None
    text_start: int = Field(default=0, ge=0)
    text_end: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_ranges(self) -> SourceFragment:
        if self.char_end <= self.char_start:
            raise ValueError("char_end must be greater than char_start")
        if self.text_end is not None and self.text_end <= self.text_start:
            raise ValueError("text_end must be greater than text_start")
        return self


class PageBoundaryDecision(BaseModel):
    """Independent evidence recorded for one adjacent-page decision."""

    previous_page: int = Field(ge=1)
    next_page: int = Field(ge=1)
    same_table_confidence: float = Field(ge=0.0, le=1.0)
    row_continuation_confidence: float = Field(ge=0.0, le=1.0)
    decision: Literal["merged", "preserved"]
    reason: str


class TableCellData(BaseModel):
    """One logical cell assembled from source fragments."""

    column_index: int = Field(ge=0)
    column_name: str | None = None
    text: str = ""
    source_fragments: list[SourceFragment] = Field(default_factory=list)
    assignment_confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class TableRowData(BaseModel):
    """A logical table row, potentially reconstructed across several pages."""

    table_id: str
    row_id: str
    algorithm_version: str
    table_title: str | None = None
    section_path: list[str] = Field(default_factory=list)
    cells: list[TableCellData] = Field(default_factory=list)
    identity_columns: list[int] = Field(default_factory=list)
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    boundary_decisions: list[PageBoundaryDecision] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_page_range(self) -> TableRowData:
        if self.page_end < self.page_start:
            raise ValueError("page_end must be greater than or equal to page_start")
        return self


class NormalizationReport(BaseModel):
    """Debug record for structural normalization decisions."""

    algorithm_version: str
    status: Literal["unchanged", "normalized", "partial_fallback"]
    boundary_decisions: list[PageBoundaryDecision] = Field(default_factory=list)
    reconstructed_row_count: int = Field(default=0, ge=0)
    fallback_reasons: list[str] = Field(default_factory=list)


class TextBlock(BaseModel):
    """A block of text extracted from a document."""

    text: str
    page_number: int | None = None
    block_type: str = "paragraph"
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_fragments: list[SourceFragment] = Field(default_factory=list)
    table_row: TableRowData | None = None


class ImageBlock(BaseModel):
    """An image extracted from a document.

    Parser→caption contract:
    - Parsers emit ``ImageBlock`` with ``caption=None``. A downstream
      caption stage fills it in via a VLM.
    - When the source text contains a placeholder for the image
      (``![alt](data:image/...)``, ``![](pptx-image-3)``, ``![](marker-key-7)``,
      …), the parser stores the exact placeholder string in
      ``metadata["markdown_ref"]``. The caption stage substitutes the
      wrapped caption back into the corresponding ``TextBlock`` via
      ``str.replace`` on that ref.
    - When there is no in-text placeholder (standalone image uploads,
      EML image attachments), ``metadata["markdown_ref"]`` is omitted;
      the caption stage produces a free-standing captioned ``TextBlock``
      instead.

    Bytes vs. URL:
    - Locally-extracted images set ``image_bytes`` (raw PNG / JPEG bytes)
      and leave ``source_url`` as ``None``.
    - Remote images parsed from a markdown ``![](http://…)`` ref leave
      ``image_bytes`` empty and set ``source_url`` to the URL. A
      downstream fetch stage may populate ``image_bytes`` later.
    - The :attr:`image_url` property is the unified VLM-friendly form:
      a ``data:`` URI built from the bytes when present, otherwise the
      ``source_url`` as-is.
    """

    image_bytes: bytes = Field(default=b"", exclude=True, repr=False)
    source_url: str | None = None
    page_number: int | None = None
    caption: str | None = None
    mime_type: str = "image/png"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def image_url(self) -> str:
        """A VLM-friendly URL for this image.

        - Bytes present → ``data:{mime_type};base64,{...}`` URI.
        - Otherwise → ``source_url`` if set, else empty string.
        - On any encoding failure → falls back to ``source_url`` (or "").
        """
        if self.image_bytes:
            try:
                b64 = base64.b64encode(self.image_bytes).decode()
                return f"data:{self.mime_type};base64,{b64}"
            except Exception:
                pass
        return self.source_url or ""


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
            "svg": DocumentType.IMAGE,
            "gif": DocumentType.IMAGE,
            "webp": DocumentType.IMAGE,
            "bmp": DocumentType.IMAGE,
            "mp3": DocumentType.AUDIO,
            "wav": DocumentType.AUDIO,
            "flac": DocumentType.AUDIO,
            "ogg": DocumentType.AUDIO,
            "aac": DocumentType.AUDIO,
            "wma": DocumentType.AUDIO,
            "mp4": DocumentType.VIDEO,
            "flv": DocumentType.VIDEO,
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

    @asynccontextmanager
    async def as_temporary_file(self, *, suffix: str | None = None) -> AsyncIterator[Path]:
        """Materialize ``raw_bytes`` to a temporary file and yield its ``Path``.

        Parsers wrapping a sync library that requires a path on disk
        (Marker, Whisper, MarkItDown, python-pptx, Spire.Doc, …) use this
        helper instead of rolling their own ``NamedTemporaryFile`` dance.
        The file is removed on context exit even if the body raises.

        ``suffix`` defaults to ``filename``'s extension, falling back to
        a content-type-appropriate default.
        """
        if self.raw_bytes is None:
            raise ValueError("Document.as_temporary_file requires raw_bytes")

        if suffix is None:
            suffix = Path(self.filename).suffix or _DEFAULT_TEMPFILE_SUFFIX.get(self.content_type, "")

        raw = self.raw_bytes

        def _write_temp() -> str:
            # Close before yielding so sync callers (Marker/Whisper/MarkItDown/
            # python-pptx/Spire.Doc) can reopen the path on Windows, where
            # NamedTemporaryFile(delete=True) holds an exclusive handle.
            tf = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            try:
                tf.write(raw)
            finally:
                tf.close()
            return tf.name

        path = await asyncio.to_thread(_write_temp)
        try:
            yield Path(path)
        finally:
            await asyncio.to_thread(_safe_unlink, path)


def _safe_unlink(path: str) -> None:
    """``os.unlink`` that swallows missing-file errors (sync callers may have already removed it)."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


_DEFAULT_TEMPFILE_SUFFIX: dict[DocumentType, str] = {
    DocumentType.PDF: ".pdf",
    DocumentType.DOCX: ".docx",
    DocumentType.PPTX: ".pptx",
    DocumentType.DOC: ".doc",
    DocumentType.AUDIO: ".wav",
    DocumentType.VIDEO: ".mp4",
    DocumentType.EML: ".eml",
    DocumentType.IMAGE: ".png",
    DocumentType.HTML: ".html",
    DocumentType.MARKDOWN: ".md",
    DocumentType.TEXT: ".txt",
}


class ProcessedDocument(BaseModel):
    """Document after parsing/extraction — contains text blocks and images."""

    document_id: str = ""
    text_blocks: list[TextBlock] = Field(default_factory=list)
    raw_text_blocks: list[TextBlock] | None = None
    normalized_text_blocks: list[TextBlock] | None = None
    normalization_report: NormalizationReport | None = None
    images: list[ImageBlock] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    page_count: int = 0

    def effective_text_blocks(self) -> list[TextBlock]:
        """Return the complete block view downstream chunkers should consume."""
        return self.normalized_text_blocks if self.normalized_text_blocks is not None else self.text_blocks
