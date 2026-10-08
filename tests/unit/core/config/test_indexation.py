"""Tests for indexation config — TranscriberConfig pipe-string parsing."""

from __future__ import annotations

import pytest
from core.config import load_config
from core.config.indexation import (
    _DEFAULT_DIRECT_UPLOAD_SUFFIXES,
    LoaderConfig,
    TranscriberConfig,
)
from pydantic import ValidationError


def test_transcriber_config_default_direct_upload_suffixes():
    cfg = TranscriberConfig()
    assert cfg.direct_upload_suffixes == set(_DEFAULT_DIRECT_UPLOAD_SUFFIXES)


def test_transcriber_config_parses_pipe_delimited_string():
    """The YAML default and TRANSCRIBER_DIRECT_UPLOAD_SUFFIXES env var both
    arrive as a pipe-delimited string. The validator must split + normalize
    into a set of dot-prefixed lowercase suffixes."""
    cfg = TranscriberConfig(direct_upload_suffixes=".wav|FLAC|mp3")
    assert cfg.direct_upload_suffixes == {".wav", ".flac", ".mp3"}


def test_transcriber_config_drops_empty_components():
    cfg = TranscriberConfig(direct_upload_suffixes="|.wav||.mp3|")
    assert cfg.direct_upload_suffixes == {".wav", ".mp3"}


def test_transcriber_config_set_input_passes_through():
    cfg = TranscriberConfig(direct_upload_suffixes={".wav", ".m4a"})
    assert cfg.direct_upload_suffixes == {".wav", ".m4a"}


def test_content_deduplication_is_enabled_by_default():
    assert LoaderConfig().content_deduplication_enabled is True


def test_content_deduplication_can_be_disabled_by_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTENT_DEDUPLICATION_ENABLED", "false")

    settings = load_config(config_path=tmp_path)

    assert settings.loader.content_deduplication_enabled is False


def test_marker_child_timeout_is_strictly_inside_marker_timeout():
    """It must expire before the bounds wrapping it, or the recycle never runs."""
    cfg = LoaderConfig()

    assert cfg.marker_child_timeout < cfg.marker_timeout
    assert cfg.marker_child_timeout < cfg.parse_timeout


def test_marker_child_timeout_scales_with_marker_timeout():
    cfg = LoaderConfig(marker_timeout=900, marker_child_timeout_ratio=0.9)

    assert cfg.marker_child_timeout == 810


def test_marker_child_timeout_stays_inside_a_shortened_marker_timeout():
    cfg = LoaderConfig(marker_timeout=60)

    assert 0 < cfg.marker_child_timeout < 60


def test_marker_child_timeout_ratio_rejects_values_that_break_the_ordering():
    for bad in (1.0, 1.5, 0.0, -0.1):
        with pytest.raises(ValidationError):
            LoaderConfig(marker_child_timeout_ratio=bad)


def test_marker_child_timeout_ratio_reads_its_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("MARKER_CHILD_TIMEOUT_RATIO", "0.5")

    settings = load_config(config_path=tmp_path)

    assert settings.loader.marker_child_timeout_ratio == 0.5


def test_marker_child_timeout_follows_a_shortened_parse_timeout():
    """parse_timeout also wraps the child, so it caps the child bound too."""
    cfg = LoaderConfig(marker_timeout=3600, parse_timeout=60)

    assert cfg.marker_child_timeout < cfg.parse_timeout
    assert cfg.marker_child_timeout == 54


def test_marker_timeout_rejects_zero_and_negative_values():
    """A non-positive marker_timeout would drive marker_child_timeout to <= 0."""
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            LoaderConfig(marker_timeout=bad)
