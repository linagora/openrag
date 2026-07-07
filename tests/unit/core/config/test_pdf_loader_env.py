"""Tests for the PDF loader env override contract (PDFLOADER, ex-PDFLoader)."""

from __future__ import annotations

from core.config import load_config

_MINIMAL_YAML = "retriever:\n  type: single\n"


def test_pdf_loader_defaults_to_pymupdf(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text(_MINIMAL_YAML, encoding="utf-8")
    # Empty string is skipped by the override loop, and load_dotenv() never
    # overrides an existing var — this shields the test from any local .env.
    monkeypatch.setenv("PDFLOADER", "")

    settings = load_config(config_path=tmp_path)

    assert settings.loader.file_loaders.pdf == "PyMuPDFLoader"


def test_pdfloader_env_overrides_default(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text(_MINIMAL_YAML, encoding="utf-8")
    monkeypatch.setenv("PDFLOADER", "MarkerLoader")

    settings = load_config(config_path=tmp_path)

    assert settings.loader.file_loaders.pdf == "MarkerLoader"


def test_legacy_mixed_case_pdfloader_env_is_ignored(monkeypatch, tmp_path):
    """The pre-rename ``PDFLoader`` name must no longer be honored."""
    (tmp_path / "config.yaml").write_text(_MINIMAL_YAML, encoding="utf-8")
    monkeypatch.setenv("PDFLOADER", "")
    monkeypatch.setenv("PDFLoader", "MarkerLoader")

    settings = load_config(config_path=tmp_path)

    assert settings.loader.file_loaders.pdf == "PyMuPDFLoader"
