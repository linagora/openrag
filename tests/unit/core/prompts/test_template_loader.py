"""Tests for the disk-based prompt template loader."""

from __future__ import annotations

from pathlib import Path

import pytest
from core.prompts.template_loader import load_template, load_template_by_key


def test_load_template_reads_file_contents(tmp_path: Path):
    target = tmp_path / "sys.txt"
    target.write_text("hello {name}", encoding="utf-8")
    assert load_template(tmp_path, "sys.txt") == "hello {name}"


def test_load_template_accepts_str_path(tmp_path: Path):
    target = tmp_path / "sys.txt"
    target.write_text("hi", encoding="utf-8")
    assert load_template(str(tmp_path), "sys.txt") == "hi"


def test_load_template_raises_on_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="Prompt file not found"):
        load_template(tmp_path, "nope.txt")


class _Mapping:
    """Stand-in for the PromptsConfig pydantic model."""

    def __init__(self, **kwargs: str) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


def test_load_template_by_key_resolves_filename(tmp_path: Path):
    (tmp_path / "hyde.txt").write_text("hyde body", encoding="utf-8")
    mapping = _Mapping(hyde="hyde.txt")
    assert load_template_by_key(tmp_path, mapping, "hyde") == "hyde body"


def test_load_template_by_key_raises_when_attr_missing(tmp_path: Path):
    mapping = _Mapping(hyde="hyde.txt")
    with pytest.raises(ValueError, match="No associated file name"):
        load_template_by_key(tmp_path, mapping, "multi_query")


def test_load_template_by_key_raises_when_attr_falsy(tmp_path: Path):
    """A mapping value of empty string / None should be treated as "not set"."""
    mapping = _Mapping(multi_query="")
    with pytest.raises(ValueError, match="No associated file name"):
        load_template_by_key(tmp_path, mapping, "multi_query")


def test_load_template_by_key_propagates_file_not_found(tmp_path: Path):
    mapping = _Mapping(hyde="missing.txt")
    with pytest.raises(FileNotFoundError):
        load_template_by_key(tmp_path, mapping, "hyde")
