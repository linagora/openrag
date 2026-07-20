"""Tests for indexation config — TranscriberConfig pipe-string parsing."""

from __future__ import annotations

from core.config import load_config
from core.config.indexation import (
    _DEFAULT_DIRECT_UPLOAD_SUFFIXES,
    LoaderConfig,
    TranscriberConfig,
)


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

    settings = load_config(conf_dir=tmp_path)

    assert settings.loader.content_deduplication_enabled is False
