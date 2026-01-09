"""Indexer API tests - file upload and indexing."""
import time

import pytest


class TestSupportedTypes:
    """Test supported file types endpoint."""

    def test_get_supported_types(self, api_client):
        """Test getting supported file types."""
        response = api_client.get("/indexer/supported/types")
        assert response.status_code == 200
        data = response.json()
        assert "extensions" in data
        assert "mimetypes" in data
        # Check common types are supported
        assert "txt" in data["extensions"]
        assert "pdf" in data["extensions"]
        assert "md" in data["extensions"]


class TestFileIndexing:
    """Test file upload and indexing operations."""

    def test_upload_text_file(self, api_client, created_partition, sample_text_file):
        """Test uploading and indexing a text file."""
        file_id = "test-file-001"

        with open(sample_text_file, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{created_partition}/file/{file_id}",
                files={"file": ("test.txt", f, "text/plain")},
                data={"metadata": "{}"}
            )

        assert response.status_code in [200, 201, 202]
        data = response.json()
        assert "task_status_url" in data or "task_id" in data

    def test_upload_markdown_file(self, api_client, created_partition, sample_markdown_file):
        """Test uploading and indexing a markdown file."""
        file_id = "test-md-001"

        with open(sample_markdown_file, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{created_partition}/file/{file_id}",
                files={"file": ("test.md", f, "text/markdown")},
                data={"metadata": "{}"}
            )

        assert response.status_code in [200, 201, 202]

    def test_upload_with_metadata(self, api_client, created_partition, sample_text_file):
        """Test uploading file with custom metadata."""
        file_id = "test-metadata-001"
        metadata = '{"author": "test", "category": "documentation"}'

        with open(sample_text_file, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{created_partition}/file/{file_id}",
                files={"file": ("test.txt", f, "text/plain")},
                data={"metadata": metadata}
            )

        assert response.status_code in [200, 201, 202]

    def test_upload_duplicate_file_replaces(self, api_client, created_partition, sample_text_file):
        """Test uploading duplicate file ID - API may allow replacement or reject."""
        file_id = "duplicate-file"

        with open(sample_text_file, "rb") as f:
            first_response = api_client.post(
                f"/indexer/partition/{created_partition}/file/{file_id}",
                files={"file": ("test.txt", f, "text/plain")},
                data={"metadata": "{}"}
            )

        assert first_response.status_code in [200, 201, 202]

        # Wait briefly for first upload to register
        time.sleep(2)

        with open(sample_text_file, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{created_partition}/file/{file_id}",
                files={"file": ("test.txt", f, "text/plain")},
                data={"metadata": "{}"}
            )

        # API may allow replacement (201) or reject duplicate (400/409)
        assert response.status_code in [200, 201, 202, 400, 409]


class TestTaskStatus:
    """Test task status endpoints."""

    def test_get_task_status(self, api_client, created_partition, sample_text_file):
        """Test getting task status after file upload."""
        file_id = "task-test-file"

        with open(sample_text_file, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{created_partition}/file/{file_id}",
                files={"file": ("test.txt", f, "text/plain")},
                data={"metadata": "{}"}
            )

        data = response.json()

        # Extract task ID from response
        if "task_status_url" in data:
            task_url = data["task_status_url"]
            # Get relative path
            task_path = "/" + "/".join(task_url.split("/")[3:])
        elif "task_id" in data:
            task_path = f"/indexer/task/{data['task_id']}"
        else:
            pytest.skip("No task ID in response")

        # Check task status
        task_response = api_client.get(task_path)
        assert task_response.status_code == 200
        task_data = task_response.json()
        assert "task_state" in task_data

    def test_get_nonexistent_task(self, api_client):
        """Test getting non-existent task returns error."""
        response = api_client.get("/indexer/task/nonexistent-task-12345")
        assert response.status_code == 404
