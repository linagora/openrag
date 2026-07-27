"""Regression test for #370 — document source-link building.

These tests exercise the real ``build_document_source_link`` helper used by
``api/routers/user/chat.py::__prepare_sources``. The helper lives in the
import-light ``api/routers/user/source_links`` module so it can be tested
without importing ``chat.py`` (which pulls in Ray, the audio loaders, and
the OpenAI SDK).

Covered:
- ``Path(source)`` must not crash when ``source`` is missing/None (the #370 bug).
- ``file_url`` must be omitted when there is no filename, and present and
  URL-encoded when there is.
"""

from api.routers.user.source_links import build_document_source_link


def _static(extract_id) -> str:
    # Authorized download is keyed by chunk id, not a raw filename.
    return f"https://host/static/{extract_id}"


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


def test_file_url_keyed_by_chunk_id_when_source_present():
    # A chunk with a source gets a download URL keyed by its id (not the raw
    # filename), so the URL never leaks an unguarded filesystem path.
    link = _build({"_id": "42", "source": "/srv/data/my doc.pdf"})
    assert link["file_url"] == "https://host/static/42"


def test_file_url_independent_of_source_basename():
    link = _build({"_id": "7", "source": "/srv/data/doc.pdf"})
    assert link["file_url"] == "https://host/static/7"


def test_metadata_is_passed_through():
    link = _build({"_id": "x", "source": "doc.pdf", "author": "alice"})
    assert link["author"] == "alice"
    assert link["chunk_url"] == "https://host/extract/x"


def test_metadata_cannot_override_authoritative_source_fields():
    link = _build(
        {
            "_id": "42",
            "source": "diagram.png",
            "file_url": "https://attacker.example/file",
            "chunk_url": "https://attacker.example/chunk",
            "source_type": "web",
        }
    )

    assert link["source_type"] == "document"
    assert link["file_url"] == "https://host/static/42"
    assert link["chunk_url"] == "https://host/extract/42"


def test_metadata_file_url_is_removed_when_source_is_missing():
    link = _build({"_id": "42", "file_url": "https://attacker.example/file"})

    assert "file_url" not in link
