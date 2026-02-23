"""
Unit tests for DocLoader .doc to .docx conversion with fallback.

Mocks spire.doc.Document entirely since it's a native .NET library
that cannot run without real .doc files.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.documents.base import Document as LCDocument


@pytest.fixture
def mock_config():
    """Create a minimal mock config for BaseLoader."""
    config = MagicMock()
    config.vlm = {"model": "mock", "base_url": "http://mock", "api_key": "mock"}
    config.loader = {"image_captioning": False, "image_captioning_url": False}
    return config


@pytest.fixture
def metadata():
    return {"file_id": "test-file-id", "partition": "test-partition"}


# All patches needed to import and instantiate DocLoader without real dependencies
_PATCHES = [
    patch("components.indexer.loaders.doc.Document"),
    patch("components.indexer.loaders.doc.DocxLoader"),
    patch("components.indexer.loaders.base.ChatOpenAI"),
    patch("components.indexer.loaders.base.load_config"),
]


def _start_patches(mock_config):
    """Start all patches and return (MockSpireDoc, MockDocxLoader)."""
    mocks = [p.start() for p in _PATCHES]
    mock_spire_doc, mock_docx_loader_cls, mock_chat, mock_load_config = mocks
    mock_load_config.return_value = mock_config
    return mock_spire_doc, mock_docx_loader_cls


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


class TestDocLoader:
    """Test DocLoader .doc to .docx conversion and fallback logic."""

    def _make_loader(self, mock_config):
        """Create a DocLoader with all dependencies mocked. Patches must be active."""
        from components.indexer.loaders.doc import DocLoader

        loader = DocLoader(config=mock_config)
        return loader

    @pytest.mark.asyncio
    async def test_successful_conversion(self, mock_config, metadata):
        """Test happy path: .doc converts to .docx successfully."""
        mock_spire_doc, _ = _start_patches(mock_config)
        loader = self._make_loader(mock_config)

        expected_doc = LCDocument(page_content="converted markdown", metadata=metadata)
        loader.MDLoader.aload_document = AsyncMock(return_value=expected_doc)

        mock_doc_instance = MagicMock()
        mock_spire_doc.return_value = mock_doc_instance

        result = await loader.aload_document("/fake/path.doc", metadata)

        mock_doc_instance.LoadFromFile.assert_called_once_with("/fake/path.doc")
        mock_doc_instance.SaveToFile.assert_called_once()
        mock_doc_instance.Close.assert_called_once()

        # DocxLoader was called with a temp .docx path
        loader.MDLoader.aload_document.assert_called_once()
        call_args = loader.MDLoader.aload_document.call_args
        assert call_args[0][0].endswith(".docx")

        assert result == expected_doc
        mock_doc_instance.GetText.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_on_spire_exception(self, mock_config, metadata):
        """Test fallback to text extraction when SaveToFile crashes."""
        mock_spire_doc, _ = _start_patches(mock_config)
        loader = self._make_loader(mock_config)

        mock_doc_instance = MagicMock()
        mock_doc_instance.SaveToFile.side_effect = Exception("TypeInitialization_Type_NoTypeAvailable")
        mock_doc_instance.GetText.return_value = "Plain text content from .doc"
        mock_spire_doc.return_value = mock_doc_instance

        result = await loader.aload_document("/fake/path.doc", metadata)

        mock_doc_instance.SaveToFile.assert_called_once()
        mock_doc_instance.GetText.assert_called_once()
        mock_doc_instance.Close.assert_called_once()

        # DocxLoader should NOT have been called
        loader.MDLoader.aload_document.assert_not_called()
        assert result.page_content == "Plain text content from .doc"
        assert result.metadata == metadata

    @pytest.mark.asyncio
    async def test_temp_file_cleaned_up_on_success(self, mock_config, metadata):
        """Test temp file is removed after successful conversion."""
        mock_spire_doc, _ = _start_patches(mock_config)
        loader = self._make_loader(mock_config)

        created_temp_files = []

        mock_doc_instance = MagicMock()

        def capture_temp_path(path, fmt):
            created_temp_files.append(path)

        mock_doc_instance.SaveToFile.side_effect = capture_temp_path
        mock_spire_doc.return_value = mock_doc_instance

        expected_doc = LCDocument(page_content="content", metadata=metadata)
        loader.MDLoader.aload_document = AsyncMock(return_value=expected_doc)

        await loader.aload_document("/fake/path.doc", metadata)

        # The temp file should have been cleaned up by the finally block
        for path in created_temp_files:
            assert not os.path.exists(path)

    @pytest.mark.asyncio
    async def test_temp_file_cleaned_up_on_failure(self, mock_config, metadata):
        """Test temp file is removed even when conversion fails."""
        mock_spire_doc, _ = _start_patches(mock_config)
        loader = self._make_loader(mock_config)

        mock_doc_instance = MagicMock()
        created_temp_files = []

        def save_then_fail(path, fmt):
            created_temp_files.append(path)
            # Create the file so we can verify it's cleaned up
            with open(path, "w") as f:
                f.write("partial")
            raise Exception("Spire crash")

        mock_doc_instance.SaveToFile.side_effect = save_then_fail
        mock_doc_instance.GetText.return_value = "fallback text"
        mock_spire_doc.return_value = mock_doc_instance

        result = await loader.aload_document("/fake/path.doc", metadata)

        assert result.page_content == "fallback text"
        for path in created_temp_files:
            assert not os.path.exists(path), f"Temp file was not cleaned up: {path}"

    @pytest.mark.asyncio
    async def test_fallback_with_save_markdown(self, mock_config, metadata, tmp_path):
        """Test fallback path respects save_markdown flag."""
        mock_spire_doc, _ = _start_patches(mock_config)
        loader = self._make_loader(mock_config)

        mock_doc_instance = MagicMock()
        mock_doc_instance.SaveToFile.side_effect = Exception("Spire crash")
        mock_doc_instance.GetText.return_value = "Extracted text"
        mock_spire_doc.return_value = mock_doc_instance

        file_path = str(tmp_path / "test.doc")

        with patch.object(loader, "save_content") as mock_save:
            result = await loader.aload_document(file_path, metadata, save_markdown=True)
            mock_save.assert_called_once_with("Extracted text", file_path)

        assert result.page_content == "Extracted text"

    @pytest.mark.asyncio
    async def test_docx_loader_error_propagates(self, mock_config, metadata):
        """Test that MDLoader errors are NOT caught by the Spire fallback."""
        mock_spire_doc, _ = _start_patches(mock_config)
        loader = self._make_loader(mock_config)

        mock_doc_instance = MagicMock()
        mock_spire_doc.return_value = mock_doc_instance

        # Spire conversion succeeds, but DocxLoader fails
        loader.MDLoader.aload_document = AsyncMock(side_effect=ValueError("DocxLoader broke"))

        with pytest.raises(ValueError, match="DocxLoader broke"):
            await loader.aload_document("/fake/path.doc", metadata)

        # GetText fallback should NOT have been used
        mock_doc_instance.GetText.assert_not_called()
        # But Close should still be called (via finally)
        mock_doc_instance.Close.assert_called_once()
