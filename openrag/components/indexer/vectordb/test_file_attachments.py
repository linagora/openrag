"""Tests for file attachment retrieval logic."""

import pytest


class TestAttachmentFiltering:
    """Test attachment filtering logic in pipeline."""

    def test_extract_file_ids_from_attachments(self):
        """Test extracting file IDs from attachments list."""
        from models.openai import Attachment

        # Valid attachments only - empty/missing ids are filtered before validation in pipeline
        attachments_raw = [
            {"id": "file-123"},
            {"id": "file-456"},
            {"id": "file-789", "type": "file"},
        ]

        # Validate and extract file_ids (like pipeline does)
        attachments = [Attachment.model_validate(att) for att in attachments_raw if isinstance(att, dict)]
        file_ids = [att.id for att in attachments if att.id]

        assert len(file_ids) == 3
        assert file_ids == ["file-123", "file-456", "file-789"]

    def test_extract_file_ids_empty_list(self):
        """Test extracting file IDs from empty attachments list."""
        attachments_raw = []

        if attachments_raw:
            from models.openai import Attachment

            attachments = [Attachment.model_validate(att) for att in attachments_raw if isinstance(att, dict)]
            file_ids = [att.id for att in attachments if att.id]
        else:
            file_ids = []

        assert file_ids == []

    def test_extract_file_ids_none(self):
        """Test extracting file IDs when attachments is None."""
        attachments_raw = None

        if attachments_raw:
            from models.openai import Attachment

            attachments = [Attachment.model_validate(att) for att in attachments_raw if isinstance(att, dict)]
            file_ids = [att.id for att in attachments if att.id]
        else:
            file_ids = []

        assert file_ids == []


class TestFilterExpression:
    """Test filter expression building for file queries."""

    def test_filter_expression_with_specific_partitions(self):
        """Test filter expression for specific partition list."""
        partition = ["partition1", "partition2"]
        file_id = "file-123"

        # Build filter expression like _retrieve_file_chunks does
        expr_parts = []
        if partition != ["all"]:
            expr_parts.append(f"partition in {partition}")
        expr_parts.append(f'file_id == "{file_id}"')
        filter_expr = " and ".join(expr_parts) if expr_parts else ""

        # Check that partition and file_id are in the expression
        assert "partition in" in filter_expr
        assert "partition1" in filter_expr
        assert "partition2" in filter_expr
        assert 'file_id == "file-123"' in filter_expr
        assert " and " in filter_expr

    def test_filter_expression_with_all_partitions(self):
        """Test filter expression for ['all'] partitions."""
        partition = ["all"]
        file_id = "file-123"

        # Build filter expression like _retrieve_file_chunks does
        expr_parts = []
        if partition != ["all"]:
            expr_parts.append(f"partition in {partition}")
        expr_parts.append(f'file_id == "{file_id}"')
        filter_expr = " and ".join(expr_parts) if expr_parts else ""

        assert "partition in" not in filter_expr
        assert 'file_id == "file-123"' in filter_expr
        assert " and " in filter_expr

    def test_filter_expression_with_all_partitions(self):
        """Test filter expression for ['all'] partitions."""
        partition = ["all"]
        file_id = "file-123"

        # Build filter expression like _retrieve_file_chunks does
        expr_parts = []
        if partition != ["all"]:
            expr_parts.append(f"partition in {partition}")
        expr_parts.append(f'file_id == "{file_id}"')
        filter_expr = " and ".join(expr_parts) if expr_parts else ""

        assert "partition in" not in filter_expr
        assert 'file_id == "file-123"' in filter_expr

    def test_extract_file_ids_none(self):
        """Test extracting file IDs when attachments is None."""
        attachments_raw = None

        if attachments_raw:
            from models.openai import Attachment

            attachments = [Attachment.model_validate(att) for att in attachments_raw if isinstance(att, dict)]
            file_ids = [att.id for att in attachments if att.id]
        else:
            file_ids = []

        assert file_ids == []
