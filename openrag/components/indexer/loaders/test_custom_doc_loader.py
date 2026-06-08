"""
Unit tests for ``CustomDocLoader.aload_document``.

These cover the page-aggregation logic that stitches the langchain loader's
per-page ``Document`` objects into a single markdown string. The underlying
langchain loader is mocked so the test stays independent of the binary
``.doc``/``.odt`` parsing backends.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.documents.base import Document

from .CustomDocLoader import CustomDocLoader


def _make_loader():
    """Create a ``CustomDocLoader`` without running ``BaseLoader.__init__``.

    ``aload_document`` does not touch any Hydra-config-backed state, so the
    instance can be built directly without a VLM/config setup.
    """
    return object.__new__(CustomDocLoader)


@pytest.mark.asyncio
async def test_load_preserves_all_pages(tmp_path):
    """Every page's text survives aggregation, not just the final page."""
    pages = [
        Document(page_content="First page content"),
        Document(page_content="Second page content"),
        Document(page_content="Third page content"),
    ]
    mock_loader = MagicMock()
    mock_loader.aload = AsyncMock(return_value=pages)
    mock_loader_cls = MagicMock(return_value=mock_loader)

    file_path = tmp_path / "fixture.doc"
    file_path.write_bytes(b"")

    loader = _make_loader()
    with patch.dict(CustomDocLoader.doc_loaders, {".doc": mock_loader_cls}):
        result = await loader.aload_document(str(file_path), metadata={"file_id": "x"})

    assert "First page content" in result.page_content
    assert "Second page content" in result.page_content
    assert "Third page content" in result.page_content
    assert "[PAGE_1]" in result.page_content
    assert "[PAGE_2]" in result.page_content
    assert "[PAGE_3]" in result.page_content
    assert result.metadata == {"file_id": "x"}
