"""Unit tests for ``core.indexing.validators``."""

from __future__ import annotations

import pytest

from ..utils.exceptions import ValidationError
from .validators import parse_metadata, validate_file_format, validate_file_id


class TestParseMetadata:
    def test_none_returns_empty(self):
        assert parse_metadata(None) == {}

    def test_empty_string_returns_empty(self):
        assert parse_metadata("") == {}

    def test_dict_passthrough(self):
        d = {"a": 1, "b": [1, 2]}
        assert parse_metadata(d) is d

    def test_valid_json_string(self):
        assert parse_metadata('{"k": "v"}') == {"k": "v"}

    def test_invalid_json_raises_400(self):
        with pytest.raises(ValidationError) as exc:
            parse_metadata("{not-json")
        assert exc.value.status_code == 400

    def test_non_object_json_raises_400(self):
        with pytest.raises(ValidationError) as exc:
            parse_metadata('["a", "b"]')
        assert exc.value.status_code == 400


class TestValidateFileId:
    def test_valid(self):
        assert validate_file_id("abc-123") == "abc-123"

    def test_default_forbidden_slash(self):
        with pytest.raises(ValidationError) as exc:
            validate_file_id("a/b")
        assert exc.value.status_code == 400

    def test_empty_or_whitespace_raises(self):
        for bad in ("", "   "):
            with pytest.raises(ValidationError):
                validate_file_id(bad)

    def test_custom_forbidden_chars(self):
        with pytest.raises(ValidationError):
            validate_file_id("hello?world", forbidden_chars="?")
        assert validate_file_id("hello/world", forbidden_chars="?") == "hello/world"


class TestValidateFileFormat:
    formats = ("pdf", "docx")
    mimetypes = ("application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    def test_extension_match(self):
        assert validate_file_format("doc.PDF", self.formats, self.mimetypes) == "pdf"

    def test_mimetype_match_when_no_extension(self):
        assert validate_file_format("noext", self.formats, self.mimetypes, mimetype="application/pdf") == ""

    def test_unsupported_raises_415(self):
        with pytest.raises(ValidationError) as exc:
            validate_file_format("img.exe", self.formats, self.mimetypes, mimetype="application/x-msdownload")
        assert exc.value.status_code == 415
