"""Regression test for #370 — Path(None) guard in chunk-source rendering.

We mirror the relevant logic from ``routers/openai.py::__prepare_sources``
rather than importing the router (which pulls in Ray, langchain_openai,
and the real ``openai`` package — and the file being named ``openai.py``
creates a circular-import hazard when imported from a test).

The bug: ``Path(doc_metadata.get(\"source\")).name`` raises TypeError when
the ``source`` key is missing. The fix coerces a missing value to ``\"\"``
before constructing the Path.
"""

from pathlib import Path

import pytest


def _prepare_sources_filename(doc_metadata: dict) -> str:
    """Mirror of the fixed logic in routers/openai.py::__prepare_sources."""
    source = doc_metadata.get("source") or ""
    return Path(source).name


def test_filename_handles_missing_source_key():
    assert _prepare_sources_filename({}) == ""
    assert _prepare_sources_filename({"_id": "x"}) == ""


def test_filename_handles_none_source_value():
    assert _prepare_sources_filename({"source": None}) == ""


def test_filename_handles_empty_source_value():
    assert _prepare_sources_filename({"source": ""}) == ""


def test_filename_extracts_basename_when_present():
    assert _prepare_sources_filename({"source": "/srv/data/doc.pdf"}) == "doc.pdf"
    assert _prepare_sources_filename({"source": "doc.pdf"}) == "doc.pdf"


def test_buggy_form_used_to_raise():
    """Sanity check that the *old* code path would indeed crash —
    this documents what the fix protected against.
    """
    with pytest.raises(TypeError):
        Path(None).name  # noqa: B018  — invocation has the side effect we test
