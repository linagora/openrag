from __future__ import annotations

from typing import Any

import pytest
from core.config.root import Settings
from core.models.document import Document, DocumentType
from langchain_core.documents.base import Document as LangChainDocument
from services.workers.parsers.doc_serializer_bridge import (
    INDEXATION_CONFIG_METADATA_KEY,
    DocSerializerBridgeParser,
)


class _FakeLoader:
    seen_config: Any = None
    seen_metadata: dict[str, Any] | None = None

    def __init__(self, *, config: Any) -> None:
        type(self).seen_config = config

    async def aload_document(self, *, file_path: str, metadata: dict | None = None, save_markdown: bool = False):
        type(self).seen_metadata = dict(metadata or {})
        return LangChainDocument(page_content="hello", metadata=metadata or {})


@pytest.mark.asyncio
async def test_bridge_disables_legacy_captioning_from_indexation_config(monkeypatch):
    """Per-file Phase 14 config disables legacy loader image captioning."""

    def fake_loader_classes(config):
        return {".txt": _FakeLoader}

    monkeypatch.setattr("services.workers.parsers.legacy_loaders.get_loader_classes", fake_loader_classes)

    parser = DocSerializerBridgeParser(
        Settings(
            loader={
                "image_captioning": True,
                "image_captioning_url": True,
            }
        )
    )
    document = Document(
        filename="note.txt",
        content_type=DocumentType.TEXT,
        raw_bytes=b"hello",
        metadata={
            "source": "note.txt",
            INDEXATION_CONFIG_METADATA_KEY: {"enable_image_captioning": False},
        },
    )

    await parser.parse(document)

    assert _FakeLoader.seen_config.loader.image_captioning is False
    assert _FakeLoader.seen_config.loader.image_captioning_url is False
    assert _FakeLoader.seen_metadata == {"source": "note.txt"}
