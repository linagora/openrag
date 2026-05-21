"""Regression test for #370 — document source-link building.

These tests exercise the real ``build_document_source_link`` helper used by
``routers/openai.py::__prepare_sources``. The helper lives in the
import-light ``routers/source_links`` module so it can be tested without
importing ``routers/openai.py`` (which pulls in Ray, the audio loaders, and
the OpenAI SDK).

Covered:
- ``Path(source)`` must not crash when ``source`` is missing/None (the #370 bug).
- ``file_url`` must be omitted when there is no filename, and present and
  URL-encoded when there is.
"""

from routers.source_links import build_document_source_link


def _static(filename: str) -> str:
    return f"https://host/static/{filename}"


def _chunk(extract_id) -> str:
    return f"https://host/extract/{extract_id}"


def _build(doc_metadata: dict) -> dict:
    return build_document_source_link(doc_metadata, _static, _chunk)


def test_missing_source_key_does_not_crash_and_omits_file_url():
    link = _build({"_id": "x"})
    assert link["source_type"] == "document"
    assert "file_url" not in link
    assert link["chunk_url"] == "https://host/extract/x"


def test_none_source_value_omits_file_url():
    link = _build({"_id": "x", "source": None})
    assert "file_url" not in link


def test_empty_source_value_omits_file_url():
    link = _build({"_id": "x", "source": ""})
    assert "file_url" not in link


def test_file_url_emitted_and_encoded_when_source_present():
    link = _build({"_id": "x", "source": "/srv/data/my doc.pdf"})
    # basename only, with spaces percent-encoded by quote()
    assert link["file_url"] == "https://host/static/my%20doc.pdf"


def test_basename_extracted_from_path():
    link = _build({"_id": "x", "source": "/srv/data/doc.pdf"})
    assert link["file_url"] == "https://host/static/doc.pdf"


def test_metadata_is_passed_through():
    link = _build({"_id": "x", "source": "doc.pdf", "author": "alice"})
    assert link["author"] == "alice"
    assert link["chunk_url"] == "https://host/extract/x"
