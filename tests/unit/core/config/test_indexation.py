"""Tests for indexation config — TranscriberConfig pipe-string parsing."""

from __future__ import annotations

from core.config.indexation import (
    _DEFAULT_DIRECT_UPLOAD_SUFFIXES,
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
