"""Tests for Chunk model — backward-compat coercions on from_langchain."""

from __future__ import annotations

from langchain_core.documents.base import Document

from openrag.core.models.chunk import Chunk, ChunkType, _coerce_chunk_type


def test_from_langchain_maps_legacy_image_chunk_type():
    """Pre-Phase-5 chunkers stamped chunk_type='image' (raw MDElement
    literal). Upgraded deployments still have those values in Milvus —
    Chunk.from_langchain must not crash on them (ultrareview)."""
    doc = Document(page_content="caption", metadata={"chunk_type": "image", "_id": "x", "file_id": "f1"})
    chunk = Chunk.from_langchain(doc)
    assert chunk.chunk_type == ChunkType.IMAGE_CAPTION


def test_from_langchain_unknown_chunk_type_falls_back_to_text():
    """Defensive: any historical value that isn't in the enum and isn't
    in the legacy alias map should land on TEXT, not crash retrieval."""
    doc = Document(page_content="x", metadata={"chunk_type": "unknown_legacy_value"})
    chunk = Chunk.from_langchain(doc)
    assert chunk.chunk_type == ChunkType.TEXT


def test_from_langchain_accepts_canonical_values():
    for value, expected in [
        ("text", ChunkType.TEXT),
        ("table", ChunkType.TABLE),
        ("image_caption", ChunkType.IMAGE_CAPTION),
        ("contextualized", ChunkType.CONTEXTUALIZED),
    ]:
        doc = Document(page_content="x", metadata={"chunk_type": value})
        assert Chunk.from_langchain(doc).chunk_type == expected


def test_coerce_chunk_type_passthrough_for_enum_input():
    assert _coerce_chunk_type(ChunkType.TABLE) == ChunkType.TABLE


def test_from_langchain_coerces_int_milvus_id_to_string():
    """Milvus' `_id` primary key is INT64 (auto_id), so the value comes back
    from the Ray actor as a Python int. Chunk.id is typed `str`; the
    conversion boundary must coerce to avoid a ValidationError on every
    retrieval call (CI api-tests regression)."""
    doc = Document(page_content="hello", metadata={"_id": 466085833598567840, "file_id": "f1"})
    chunk = Chunk.from_langchain(doc)
    assert chunk.id == "466085833598567840"
    assert isinstance(chunk.id, str)
