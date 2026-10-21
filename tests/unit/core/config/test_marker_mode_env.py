"""Tests for the global Marker mode (MARKER_MODE) and OCR concurrency (MARKER_OCR_CONCURRENCY)."""

from __future__ import annotations

import pytest
from core.config import load_config
from pydantic import ValidationError

_MINIMAL_YAML = "retriever:\n  type: single\n"


@pytest.fixture
def config_dir(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text(_MINIMAL_YAML, encoding="utf-8")
    # Empty string is skipped by the override loop, and load_dotenv() never
    # overrides an existing var — this shields the tests from any local .env.
    monkeypatch.setenv("MARKER_MODE", "")
    monkeypatch.setenv("MARKER_OCR_CONCURRENCY", "")
    return tmp_path


def test_marker_mode_defaults_to_fast(config_dir):
    loader = load_config(config_path=config_dir).loader

    assert (loader.marker_mode, loader.marker_ocr_concurrency) == ("fast", 32)


@pytest.mark.parametrize("mode", ["balanced", "fast", "fast_no_ocr"])
def test_marker_mode_env_sets_the_mode(monkeypatch, config_dir, mode):
    monkeypatch.setenv("MARKER_MODE", mode)

    assert load_config(config_path=config_dir).loader.marker_mode == mode


def test_an_unknown_marker_mode_is_refused(monkeypatch, config_dir):
    monkeypatch.setenv("MARKER_MODE", "accurate")

    with pytest.raises(ValidationError, match="marker_mode"):
        load_config(config_path=config_dir)


def test_marker_ocr_concurrency_env_sets_it(monkeypatch, config_dir):
    monkeypatch.setenv("MARKER_OCR_CONCURRENCY", "8")

    assert load_config(config_path=config_dir).loader.marker_ocr_concurrency == 8


def test_marker_ocr_concurrency_must_be_positive(monkeypatch, config_dir):
    monkeypatch.setenv("MARKER_OCR_CONCURRENCY", "0")

    with pytest.raises(ValidationError, match="marker_ocr_concurrency"):
        load_config(config_path=config_dir)


def test_the_shipped_config_sets_the_mode_explicitly():
    """Unset, Marker picks the mode from whether its process sees CUDA."""
    from pathlib import Path

    import yaml

    shipped = yaml.safe_load((Path(__file__).parents[4] / "conf" / "config.yaml").read_text(encoding="utf-8"))

    assert shipped["loader"]["marker_mode"] == "fast"
