"""Legacy ``.doc`` (binary Word 97-2003) ``DocumentParser``.

Converts ``.doc`` to ``.docx`` via the ``spire.doc`` library, then
delegates to :class:`DocxParser` for Markdown extraction. Falls back to
plain-text extraction (``Document.GetText()``) if Spire's conversion
fails.

Spire.Doc requires DOTNET; the constructor sets the invariant-globalization
env var that makes Spire usable without the full ICU data.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

from ...models.document import Document, DocumentType, ProcessedDocument, TextBlock
from .document_parser import DocumentParser
from .docx_parser import DocxParser
from .registry import parser_registry

logger = logging.getLogger(__name__)

os.environ.setdefault("DOTNET_SYSTEM_GLOBALIZATION_INVARIANT", "1")


@parser_registry.register("doc")
class DocParser(DocumentParser):
    """Parse legacy ``.doc`` files via .docx conversion + DocxParser."""

    def __init__(self, docx_parser: DocxParser | None = None) -> None:
        """Pass an explicit ``DocxParser`` to share VLM / semaphore config;
        otherwise a captioning-disabled instance is constructed.
        """
        self._docx = docx_parser or DocxParser()

    def supported_types(self) -> list[str]:
        return [DocumentType.DOC.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        if not document.raw_bytes:
            return ProcessedDocument(
                document_id=document.id,
                metadata=dict(document.metadata),
            )

        async with document.as_temporary_file() as src_path:
            docx_bytes, fallback_text = await asyncio.to_thread(self._convert, str(src_path))

        if docx_bytes:
            docx_doc = document.model_copy(update={"raw_bytes": docx_bytes, "content_type": DocumentType.DOCX})
            return await self._docx.parse(docx_doc)

        text = (fallback_text or "").strip()
        text_blocks = [TextBlock(text=text, page_number=1)] if text else []
        return ProcessedDocument(
            document_id=document.id,
            text_blocks=text_blocks,
            metadata=dict(document.metadata),
            page_count=1 if text else 0,
        )

    @staticmethod
    def _convert(path: str) -> tuple[bytes | None, str | None]:
        """Run blocking Spire.Doc conversion. Returns ``(docx_bytes, fallback_text)``.

        Exactly one of the two will be non-None on success; both ``None``
        means total failure (caller emits an empty ProcessedDocument).
        """
        try:
            from spire.doc import Document as SpireDocument
            from spire.doc import FileFormat
        except ImportError:
            logger.warning("spire.doc not available; cannot parse legacy .doc files")
            return None, None

        spire_doc = SpireDocument()
        out_path: str | None = None
        try:
            spire_doc.LoadFromFile(path)
            with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as out:
                out_path = out.name
            spire_doc.SaveToFile(out_path, FileFormat.Docx2016)
            return Path(out_path).read_bytes(), None
        except Exception as exc:
            logger.warning("Spire.Doc .doc → .docx conversion failed (%s); falling back to plain text", exc)
            try:
                return None, spire_doc.GetText()
            except Exception as fallback_exc:
                logger.warning("Spire.Doc fallback text extraction also failed: %s", fallback_exc)
                return None, None
        finally:
            try:
                spire_doc.Close()
            except Exception:
                pass
            if out_path and os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass
