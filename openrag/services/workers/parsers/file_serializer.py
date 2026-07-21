"""In-process ``FileSerializer`` over the new parser stack.

``ConversionService`` (the ``extractText`` tool + ``/extract`` route) needs
file → text. This implements the :class:`~core.indexing.serializer.FileSerializer`
port by running :class:`ParserDispatcher` directly: parse the file into a
``ProcessedDocument``, caption images when a VLM is configured, and join the
text blocks.

No Ray-actor hop — the GPU backends (marker/docling/whisper) still dispatch to
their pool actors from inside the dispatcher, so only the lightweight dispatch
and CPU parsers run in-process. Replaces the former ``DocSerializer`` Ray actor
+ ``DocSerializerAdapter`` (the Phase-9 shim that indexing no longer shares).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from core.indexing.serializer import FileSerializer


class ParserFileSerializer(FileSerializer):
    """Implements ``FileSerializer`` by running the parser dispatcher in-process."""

    def __init__(self) -> None:
        from core.config import load_config
        from services.workers.parsers.parser_dispatcher import (
            build_caption_vlm,
            build_parser_dispatcher,
            load_caption_prompt,
        )

        config = load_config()
        self._dispatcher = build_parser_dispatcher(config)
        self._vlm = build_caption_vlm(config)
        self._caption_prompt = load_caption_prompt(config) if self._vlm is not None else None
        # Global gate for captioning images embedded in other documents.
        # Standalone image files are always captioned (see ``serialize``).
        self._image_captioning = bool(config.loader.image_captioning)

    async def serialize(self, path: str, metadata: dict) -> str:
        from core.models.document import Document, DocumentType
        from services.workers.stages.caption import _caption_document

        metadata = dict(metadata or {})
        p = Path(path)
        name_for_type = metadata.get("filename") or p.name
        raw_bytes = await asyncio.to_thread(p.read_bytes)

        document = Document(
            filename=name_for_type,
            raw_bytes=raw_bytes,
            content_type=Document.detect_content_type(name_for_type),
            metadata=metadata,
        )

        processed = await self._dispatcher.parse(document)
        should_caption = (
            self._vlm is not None
            and processed.images
            and (document.content_type is DocumentType.IMAGE or self._image_captioning)
        )
        if should_caption:
            processed = await _caption_document(processed, self._vlm, self._caption_prompt)

        return "\n\n".join(block.text for block in processed.text_blocks if block.text)


def build_file_serializer() -> ParserFileSerializer:
    """Build the in-process file serializer. Convenience for the composition root."""
    return ParserFileSerializer()


__all__ = ["ParserFileSerializer", "build_file_serializer"]
