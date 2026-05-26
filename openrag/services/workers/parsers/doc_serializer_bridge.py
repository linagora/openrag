from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from core.indexing.parsers.document_parser import DocumentParser
from core.models.document import Document, DocumentType, ProcessedDocument, TextBlock


class DocSerializerBridgeParser(DocumentParser):
    """Transitional parser backed by the legacy loader registry."""

    def __init__(self, config: Any) -> None:
        from components.indexer.loaders import get_loader_classes

        self._config = config
        self._loader_classes = get_loader_classes(config=config)
        self._save_markdown = getattr(config.loader, "save_markdown", False)

    def supported_types(self) -> list[str]:
        return [doc_type.value for doc_type in DocumentType]

    async def parse(self, document: Document) -> ProcessedDocument:
        suffix = _suffix_from_document(document)
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(document.raw_bytes or b"")
            temp_path = handle.name
        try:
            return await self._load_via_legacy(temp_path, document)
        finally:
            Path(temp_path).unlink(missing_ok=True)

    async def _load_via_legacy(self, path: str, document: Document) -> ProcessedDocument:
        metadata = dict(document.metadata or {})
        loader_cls = self._loader_for(path, metadata)
        if loader_cls is None:
            raise ValueError(f"No loader registered for file extension {Path(path).suffix.lower()!r}")

        loader = loader_cls(config=self._config)
        lang_doc = await loader.aload_document(
            file_path=path,
            metadata=metadata,
            save_markdown=self._save_markdown,
        )

        return ProcessedDocument(
            document_id=document.filename or "unknown",
            text_blocks=[TextBlock(text=lang_doc.page_content or "")],
            metadata=lang_doc.metadata or {},
        )

    def _loader_for(self, path: str, metadata: dict[str, Any]) -> Any | None:
        mimetype = metadata.get("mimetype")
        if mimetype:
            try:
                from services.workers.parsers.doc_serializer import DICT_MIMETYPES

                loader_cls = self._loader_classes.get(DICT_MIMETYPES.get(mimetype))
            except Exception:
                loader_cls = None
            if loader_cls is not None:
                return loader_cls

        return self._loader_classes.get(Path(path).suffix.lower())


def _suffix_from_document(document: Document) -> str:
    source = (document.metadata or {}).get("source")
    if source:
        suffix = Path(str(source)).suffix
        if suffix:
            return suffix
    if document.filename:
        suffix = Path(document.filename).suffix
        if suffix:
            return suffix
    return f".{document.content_type.value}" if document.content_type else ""


__all__ = ["DocSerializerBridgeParser"]
