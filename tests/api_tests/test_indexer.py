"""Indexer API tests - file upload and indexing."""

import time
from pathlib import Path

import pytest

RESOURCES_DIR = Path(__file__).parent.parent / "resources"
PDF_FILE = RESOURCES_DIR / "test_file.pdf"
TASK_TIMEOUT = 180  # 3 minutes for file processing


def wait_for_task(api_client, task_id: str, timeout: int = TASK_TIMEOUT) -> dict:
    """Wait for task completion, polling status endpoint.

    Handles 404 responses gracefully as task may not be registered yet.
    """
    start = time.time()
    while time.time() - start < timeout:
        response = api_client.get(f"/indexer/task/{task_id}")

        # Task might not be registered yet, retry on 404
        if response.status_code == 404:
            time.sleep(0.5)
            continue

        if response.status_code != 200:
            raise AssertionError(f"Task status failed: {response.text}")

        status = response.json()
        state = status.get("task_state")

        if state == "COMPLETED":
            return status
        elif state == "FAILED":
            raise AssertionError(f"Task failed: {status}")

        time.sleep(1)

    raise TimeoutError(f"Task {task_id} did not complete within {timeout}s")


def get_task_id(response_data: dict) -> str:
    """Extract task ID from API response."""
    if "task_status_url" in response_data:
        return response_data["task_status_url"].split("/")[-1]
    elif "task_id" in response_data:
        return response_data["task_id"]
    raise ValueError("No task ID in response")


@pytest.fixture
def pdf_file_path():
    """Path to the test PDF file."""
    if not PDF_FILE.exists():
        pytest.skip(f"Test PDF not found: {PDF_FILE}")
    return PDF_FILE


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

    def test_upload_markdown_file(self, api_client, created_partition, sample_markdown_file):
        """Test uploading and indexing a markdown file."""
        file_id = "test-md-001"

        with open(sample_markdown_file, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{created_partition}/file/{file_id}",
                files={"file": ("test.md", f, "text/markdown")},
                data={"metadata": "{}"},
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
                data={"metadata": metadata},
            )

        assert response.status_code in [200, 201, 202]

    def test_upload_duplicate_file_replaces(
        self, api_client, created_partition, sample_text_file
    ):
        """Test uploading duplicate file ID - API may allow replacement or reject."""
        file_id = "duplicate-file"

        with open(sample_text_file, "rb") as f:
            first_response = api_client.post(
                f"/indexer/partition/{created_partition}/file/{file_id}",
                files={"file": ("test.txt", f, "text/plain")},
                data={"metadata": "{}"},
            )

        assert first_response.status_code in [200, 201, 202]

        # Wait briefly for first upload to register
        time.sleep(2)

        with open(sample_text_file, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{created_partition}/file/{file_id}",
                files={"file": ("test.txt", f, "text/plain")},
                data={"metadata": "{}"},
            )

        # API may allow replacement (201) or reject duplicate (400/409)
        assert response.status_code in [200, 201, 202, 400, 409]


class TestIndexedDocuments:
    """Test document retrieval after indexing."""

    def test_indexed_file_creates_documents(
        self, api_client, created_partition, pdf_file_path
    ):
        """Test that indexed file creates retrievable documents."""
        file_id = "doc-test-001"

        with open(pdf_file_path, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{created_partition}/file/{file_id}",
                files={"file": (pdf_file_path.name, f, "application/pdf")},
                data={"metadata": "{}"},
            )

        data = response.json()
        task_id = get_task_id(data)
        wait_for_task(api_client, task_id)

        # Retrieve file and verify documents
        file_response = api_client.get(f"/partition/{created_partition}/file/{file_id}")
        assert file_response.status_code == 200, f"Get file failed: {file_response.text}"

        file_data = file_response.json()
        assert "metadata" in file_data
        assert "documents" in file_data
        assert len(file_data["documents"]) > 0, "No documents created from file"


class TestTaskStatus:
    """Test task status endpoints."""

    def test_get_task_status(self, api_client, created_partition, sample_text_file):
        """Test getting task status after file upload."""
        file_id = "task-test-file"

        with open(sample_text_file, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{created_partition}/file/{file_id}",
                files={"file": ("test.txt", f, "text/plain")},
                data={"metadata": "{}"},
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

        # Check task status (retry on 404 as task may not be registered yet)
        for _ in range(10):
            task_response = api_client.get(task_path)
            if task_response.status_code != 404:
                break
            time.sleep(0.5)

        assert task_response.status_code == 200, f"Task status failed: {task_response.text}"
        task_data = task_response.json()
        assert "task_state" in task_data

    def test_get_nonexistent_task(self, api_client):
        """Test getting non-existent task returns error."""
        response = api_client.get("/indexer/task/nonexistent-task-12345")
        assert response.status_code == 404

    def test_task_state_transitions(self, api_client, created_partition, pdf_file_path):
        """Test that file processing goes through expected states."""
        file_id = "state-test-001"
        observed_states = set()
        valid_states = {"QUEUED", "SERIALIZING", "CHUNKING", "INSERTING", "COMPLETED", "FAILED"}

        with open(pdf_file_path, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{created_partition}/file/{file_id}",
                files={"file": (pdf_file_path.name, f, "application/pdf")},
                data={"metadata": "{}"},
            )

        data = response.json()
        task_id = get_task_id(data)

        start = time.time()
        while time.time() - start < TASK_TIMEOUT:
            status_response = api_client.get(f"/indexer/task/{task_id}")

            # Handle 404 for task not yet registered
            if status_response.status_code == 404:
                time.sleep(0.5)
                continue

            status = status_response.json()
            state = status.get("task_state")

            if state:
                observed_states.add(state)
                assert state in valid_states, f"Invalid state: {state}"

            if state == "COMPLETED":
                break
            elif state == "FAILED":
                pytest.fail(f"Task failed: {status}")

            time.sleep(0.5)

        assert "COMPLETED" in observed_states, f"Never completed. Observed: {observed_states}"


class TestErrorHandling:
    """Test error handling in file processing."""

    def test_invalid_file_handling(self, api_client, created_partition, tmp_path):
        """Test that invalid files are handled gracefully."""
        # Create an invalid PDF file
        invalid_file = tmp_path / "invalid.pdf"
        invalid_file.write_text("This is not a valid PDF file")

        file_id = "invalid-file-test"

        with open(invalid_file, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{created_partition}/file/{file_id}",
                files={"file": ("invalid.pdf", f, "application/pdf")},
                data={"metadata": "{}"},
            )

        # Upload should be accepted, but task may fail
        if response.status_code in [200, 201, 202]:
            data = response.json()
            task_id = get_task_id(data)

            # Wait and check if it fails gracefully
            start = time.time()
            state = None
            while time.time() - start < 60:
                status_response = api_client.get(f"/indexer/task/{task_id}")

                if status_response.status_code == 404:
                    time.sleep(0.5)
                    continue

                status = status_response.json()
                state = status.get("task_state")

                if state in ["COMPLETED", "FAILED"]:
                    break
                time.sleep(1)

            # Either state is acceptable - task should not hang
            assert state in ["COMPLETED", "FAILED"], f"Task stuck in state: {state}"
