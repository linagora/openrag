"""Document — the input to the indexing pipeline."""

from __future__ import annotations

import asyncio
import base64
import functools
import os
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
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
      downstream fetch stage may populate ``image_bytes`` later — but see
      the lifetime note below: such a stage has to run *before* the caption
      decision.
    - The :attr:`image_url` property is the unified VLM-friendly form:
      a ``data:`` URI built from the bytes when present, otherwise the
      ``source_url`` as-is.

    Lifetime of ``image_bytes`` during indexing:
    - ``IndexingPipeline`` clears it (``_release_image_bytes``) as soon as the
      caption decision resolves — whether captioning ran, was skipped or
      failed — because the payloads outlast the file itself and survived embed
      and store. A stage added after that point reads ``b""``, not the image.
      Everything else on this block survives: ``caption``, ``page_number``,
      ``mime_type``, ``source_url`` and ``metadata``.
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
    source_path: str | None = Field(None, exclude=True)
    partition: str = "default"
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @staticmethod
    def type_extension(filename: str, mimetype: str | None = None) -> str:
        """The extension that decides how a file is parsed, lowercased, without the dot.

        A *mimetype* listed in ``loader.mimetypes`` wins, through the extension
        that config maps it to; the filename's own extension is the fallback when
        the mimetype is absent or unknown.
        """
        mapped = _configured_mimetypes().get(mimetype, "") if mimetype else ""
        if mapped:
            return mapped.lstrip(".").lower()
        return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    @staticmethod
    def detect_content_type(filename: str, mimetype: str | None = None) -> DocumentType:
        """Detect DocumentType from *mimetype*, else the filename extension (see ``type_extension``)."""
        ext = Document.type_extension(filename, mimetype)
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
        """Yield a filesystem path for this document's content.

        Parsers wrapping a sync library that requires a path on disk
        (Marker, Whisper, MarkItDown, python-pptx, Spire.Doc, …) use this
        helper instead of rolling their own ``NamedTemporaryFile`` dance.

        When :attr:`source_path` is set, that path is yielded **as-is** and
        nothing is written or deleted: the document already has a file, and a
        ``NamedTemporaryFile`` written here would be visible only to the node
        that wrote it — which is what stops ``MarkerPool``/``DoclingPool``/
        ``WhisperPool`` handing a path to a worker actor on another node
        (#911).

        Whether the yielded path is reachable from another node is the
        *caller's* property, not this method's: the upload routes save under
        ``config.paths.data_dir`` (Helm ``persistence.accessMode:
        ReadWriteMany``, Compose's ``${DATA_VOLUME}:/app/data`` bind mount) and
        are placeable; ``index_url``'s download is in the node's own temp dir
        and is not.

        Otherwise ``raw_bytes`` is materialized to a temporary file, which *is*
        removed on context exit even if the body raises. That is the path for
        **derived** documents — the ``.docx`` that ``DocParser`` converts a
        legacy ``.doc`` into, EML attachments — which have no file of their own.

        ``suffix`` defaults to ``filename``'s extension, falling back to a
        content-type-appropriate default. A ``source_path`` is only yielded
        when its own extension matches, since the sync libraries below dispatch
        on it; a mismatch falls back to writing the bytes out under the
        requested suffix.
        """
        if suffix is None:
            # The filename's own extension only when it agrees with the type:
            # a type resolved from the mimetype (``report`` sent as a PDF)
            # needs the suffix the sync libraries expect for that type.
            suffix = Path(self.filename).suffix
            if not suffix or Document.detect_content_type(self.filename) is not self.content_type:
                suffix = _DEFAULT_TEMPFILE_SUFFIX.get(self.content_type, suffix)

        if self.source_path is not None and Path(self.source_path).suffix == suffix:
            # Never unlinked: this is the caller's file, not ours. The upload is
            # purged by ``indexer_pool`` after indexing settles, if configured.
            yield Path(self.source_path)
            return

        if self.raw_bytes is None:
            raise ValueError("Document.as_temporary_file requires raw_bytes or source_path")

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


@functools.cache
def _configured_mimetypes() -> dict[str, str]:
    """``loader.mimetypes`` as ``{mimetype: extension}``, read once per process."""
    from core.config import load_config

    return load_config().loader.mimetypes.to_dict()


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
    images: list[ImageBlock] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    page_count: int = 0
