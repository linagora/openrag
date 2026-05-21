"""Regression test for CustomDocLoader page accumulation (#376).

The previous loop body used ``s = ...`` instead of ``s += ...``, so only
the final page survived. This test confirms every page's content is now
in the returned ``Document``.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.documents.base import Document as LCDocument


@pytest.mark.asyncio
async def test_customdocloader_accumulates_all_pages(tmp_path):
    from components.indexer.loaders.CustomDocLoader import CustomDocLoader

    fake_pages = [
        LCDocument(page_content="page-one"),
        LCDocument(page_content="page-two"),
        LCDocument(page_content="page-three"),
    ]
    fake_loader_instance = MagicMock()
    fake_loader_instance.aload = AsyncMock(return_value=fake_pages)
    fake_loader_cls = MagicMock(return_value=fake_loader_instance)

    file_path = tmp_path / "stub.docx"
    file_path.write_text("ignored")

    with patch.dict(CustomDocLoader.doc_loaders, {".docx": fake_loader_cls}, clear=True):
        # BaseLoader.__init__ pulls a config; we bypass it with object.__new__
        loader = object.__new__(CustomDocLoader)
        result = await loader.aload_document(str(file_path), metadata={"src": "x"})

    assert "page-one" in result.page_content
    assert "page-two" in result.page_content
    assert "page-three" in result.page_content
    assert "[PAGE_1]" in result.page_content
    assert "[PAGE_2]" in result.page_content
    assert "[PAGE_3]" in result.page_content
