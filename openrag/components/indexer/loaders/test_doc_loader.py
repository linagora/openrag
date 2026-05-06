"""
Unit tests for the legacy ``DocLoader`` shim.

The .doc → .docx → markdown conversion logic itself is tested in
``core/indexing/parsers/test_doc_parser.py``. These tests cover only
shim-level concerns: the langchain ``Document`` round-trip, the
``save_markdown=True`` integration with ``BaseLoader.save_content``,
and that errors raised by the underlying ``DocParser`` propagate
without being swallowed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from config.models import LoaderConfig, VLMConfig
from core.models.document import ProcessedDocument, TextBlock
from langchain_core.documents.base import Document as LCDocument


@pytest.fixture
def mock_config():
    """Minimal mock config for BaseLoader."""
    config = MagicMock()
    config.vlm = VLMConfig(model="mock", base_url="http://mock", api_key="mock")
    config.loader = LoaderConfig(image_captioning=False, image_captioning_url=False)
    return config


@pytest.fixture
def metadata():
    return {"file_id": "test-file-id", "partition": "test-partition"}


_PATCHES = [
    patch("components.indexer.loaders.doc.DocParser"),
    patch("components.indexer.loaders.base.ChatOpenAI"),
    patch("components.indexer.loaders.base.load_config"),
]


def _start_patches(mock_config):
    mocks = [p.start() for p in _PATCHES]
    _mock_doc_parser_cls, _mock_chat, mock_load_config = mocks
    mock_load_config.return_value = mock_config


def _stop_patches():
    for p in _PATCHES:
        try:
            p.stop()
        except RuntimeError:
            pass


@pytest.fixture(autouse=True)
def _patch_cleanup():
    yield
    _stop_patches()


def _make_loader(mock_config):
    from components.indexer.loaders.doc import DocLoader

    return DocLoader(config=mock_config)


def _processed(text: str = "markdown") -> ProcessedDocument:
    return ProcessedDocument(
        document_id="test",
        text_blocks=[TextBlock(text=text, page_number=1)] if text else [],
        metadata={},
        page_count=1 if text else 0,
    )


class TestDocLoaderShim:
    """Shim-level integration: ``DocParser`` ↔ langchain ``Document`` ↔ ``BaseLoader``."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_langchain_document(self, mock_config, metadata, tmp_path):
        """Parser output is joined into ``page_content``; ``metadata`` is passed through."""
        _start_patches(mock_config)
        loader = _make_loader(mock_config)
        loader._parser = MagicMock()
        loader._parser.parse = AsyncMock(return_value=_processed("converted markdown"))

        file_path = tmp_path / "x.doc"
        file_path.write_bytes(b"\xd0\xcf\x11\xe0fake-doc")

        result = await loader.aload_document(str(file_path), metadata)

        assert isinstance(result, LCDocument)
        assert result.page_content == "converted markdown"
        assert result.metadata == metadata

        loader._parser.parse.assert_awaited_once()
        forwarded = loader._parser.parse.await_args.args[0]
        assert forwarded.raw_bytes == b"\xd0\xcf\x11\xe0fake-doc"
        assert forwarded.filename == "x.doc"

    @pytest.mark.asyncio
    async def test_empty_parser_result_yields_empty_content(self, mock_config, metadata, tmp_path):
        """An empty ``ProcessedDocument`` yields an empty langchain document."""
        _start_patches(mock_config)
        loader = _make_loader(mock_config)
        loader._parser = MagicMock()
        loader._parser.parse = AsyncMock(return_value=_processed(text=""))

        file_path = tmp_path / "x.doc"
        file_path.write_bytes(b"")

        result = await loader.aload_document(str(file_path), metadata)
        assert result.page_content == ""
        assert result.metadata == metadata

    @pytest.mark.asyncio
    async def test_save_markdown_writes_extracted_content(self, mock_config, metadata, tmp_path):
        """``save_markdown=True`` calls ``BaseLoader.save_content`` with the extracted text and source path."""
        _start_patches(mock_config)
        loader = _make_loader(mock_config)
        loader._parser = MagicMock()
        loader._parser.parse = AsyncMock(return_value=_processed("Extracted text"))

        file_path = tmp_path / "x.doc"
        file_path.write_bytes(b"x")

        with patch.object(loader, "save_content") as mock_save:
            result = await loader.aload_document(str(file_path), metadata, save_markdown=True)
            mock_save.assert_called_once_with("Extracted text", str(file_path))

        assert result.page_content == "Extracted text"

    @pytest.mark.asyncio
    async def test_parser_error_propagates(self, mock_config, metadata, tmp_path):
        """Exceptions from the underlying ``DocParser`` are not swallowed."""
        _start_patches(mock_config)
        loader = _make_loader(mock_config)
        loader._parser = MagicMock()
        loader._parser.parse = AsyncMock(side_effect=ValueError("DocParser broke"))

        file_path = tmp_path / "x.doc"
        file_path.write_bytes(b"x")

        with pytest.raises(ValueError, match="DocParser broke"):
            await loader.aload_document(str(file_path), metadata)
