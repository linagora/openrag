"""Tests for OpenAI-compatible models."""

import pytest
from pydantic import ValidationError

from models.openai import Attachment, MetadataDict


class TestAttachment:
    """Test Attachment model validation."""

    def test_attachment_with_required_id(self):
        """Test attachment with only required id field."""
        attachment = Attachment(id="file-123")
        assert attachment.id == "file-123"
        assert attachment.type is None
        assert attachment.priority is None

    def test_attachment_with_all_fields(self):
        """Test attachment with all fields."""
        attachment = Attachment(id="file-123", type="file", priority=1)
        assert attachment.id == "file-123"
        assert attachment.type == "file"
        assert attachment.priority == 1

    def test_attachment_empty_id_raises_error(self):
        """Test that empty id raises validation error."""
        with pytest.raises(ValidationError) as exc_info:
            Attachment(id="")
        error_str = str(exc_info.value).lower()
        assert "min_length" in error_str or "at least 1 character" in error_str or "string_too_short" in error_str

    def test_attachment_missing_id_raises_error(self):
        """Test that missing id raises validation error."""
        with pytest.raises(ValidationError):
            Attachment()  # type: ignore

    def test_attachment_invalid_priority(self):
        """Test that negative priority raises validation error."""
        with pytest.raises(ValidationError):
            Attachment(id="file-123", priority=-1)

    def test_attachment_invalid_type(self):
        """Test that invalid type raises validation error."""
        with pytest.raises(ValidationError):
            Attachment(id="file-123", type="invalid")  # type: ignore

    def test_attachment_extra_fields_ignored(self):
        """Test that extra fields are ignored (forward compatibility)."""
        attachment = Attachment(id="file-123", extra_field="should_be_ignored")  # type: ignore
        assert attachment.id == "file-123"
        # Extra fields should not be accessible
        assert not hasattr(attachment, "extra_field")


class TestMetadataDict:
    """Test MetadataDict TypedDict usage."""

    def test_metadata_dict_empty(self):
        """Test empty metadata dict."""
        metadata: MetadataDict = {}
        assert metadata == {}

    def test_metadata_dict_with_attachments(self):
        """Test metadata dict with attachments."""
        metadata: MetadataDict = {"attachments": [{"id": "file-123"}, {"id": "file-456"}]}
        assert len(metadata["attachments"]) == 2

    def test_metadata_dict_with_all_fields(self):
        """Test metadata dict with all known fields."""
        metadata: MetadataDict = {
            "use_map_reduce": True,
            "spoken_style_answer": False,
            "websearch": True,
            "llm_override": {"model": "custom-model"},
            "attachments": [{"id": "file-123"}],
        }
        assert metadata["use_map_reduce"] is True
        assert metadata["websearch"] is True
        assert metadata["attachments"] is not None

    def test_metadata_dict_with_unknown_field(self):
        """Test that unknown fields are allowed (total=False)."""
        metadata: MetadataDict = {
            "use_map_reduce": True,
            "unknown_field": "value",  # type: ignore
        }
        assert metadata["use_map_reduce"] is True
        assert metadata.get("unknown_field") == "value"
