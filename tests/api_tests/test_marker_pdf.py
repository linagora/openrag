"""Marker PDF processing integration tests.

These tests verify the full PDF indexing pipeline using MarkerLoader:
1. PDF upload and task state transitions
2. Document chunking and storage
3. Text extraction via extractText tool
4. Timeout and cancellation handling
"""

import time
from pathlib import Path

import pytest

RESOURCES_DIR = Path(__file__).parent.parent / "resources"
PDF_FILE = RESOURCES_DIR / "test_file.pdf"
TASK_TIMEOUT = 180  # 3 minutes for PDF processing


@pytest.fixture
def pdf_file_path():
    """Path to the test PDF file."""
    if not PDF_FILE.exists():
        pytest.skip(f"Test PDF not found: {PDF_FILE}")
    return PDF_FILE


def wait_for_task(api_client, task_id: str, timeout: int = TASK_TIMEOUT) -> dict:
    """Wait for task completion, polling status endpoint."""
    start = time.time()
    while time.time() - start < timeout:
        response = api_client.get(f"/indexer/task/{task_id}")
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


class TestMarkerPDFIndexing:
    """Tests for PDF indexing through MarkerLoader."""

    def test_index_pdf_completes(self, api_client, created_partition, pdf_file_path):
        """Test that PDF indexing completes successfully."""
        file_id = "marker-pdf-test-1"

        with open(pdf_file_path, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{created_partition}/file/{file_id}",
                files={"file": (pdf_file_path.name, f, "application/pdf")},
                data={"metadata": "{}"},
            )

        assert response.status_code in [200, 201, 202], f"Upload failed: {response.text}"
        data = response.json()

        # Extract task ID
        if "task_status_url" in data:
            task_id = data["task_status_url"].split("/")[-1]
        elif "task_id" in data:
            task_id = data["task_id"]
        else:
            pytest.fail("No task ID in response")

        # Wait for completion
        final_status = wait_for_task(api_client, task_id)
        assert final_status["task_state"] == "COMPLETED"

    def test_pdf_creates_documents(self, api_client, created_partition, pdf_file_path):
        """Test that indexed PDF creates retrievable documents."""
        file_id = "marker-pdf-test-2"

        # Upload and wait for indexing
        with open(pdf_file_path, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{created_partition}/file/{file_id}",
                files={"file": (pdf_file_path.name, f, "application/pdf")},
                data={"metadata": "{}"},
            )

        data = response.json()
        task_id = data.get("task_status_url", "").split("/")[-1] or data.get("task_id")
        wait_for_task(api_client, task_id)

        # Retrieve file and verify documents
        file_response = api_client.get(f"/partition/{created_partition}/file/{file_id}")
        assert file_response.status_code == 200, f"Get file failed: {file_response.text}"

        file_data = file_response.json()
        assert "metadata" in file_data
        assert "documents" in file_data
        assert len(file_data["documents"]) > 0, "No documents created from PDF"

    def test_pdf_task_state_transitions(self, api_client, created_partition, pdf_file_path):
        """Test that PDF processing goes through expected states."""
        file_id = "marker-pdf-test-3"
        observed_states = set()
        valid_states = {"SERIALIZING", "CHUNKING", "INSERTING", "COMPLETED", "FAILED"}

        with open(pdf_file_path, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{created_partition}/file/{file_id}",
                files={"file": (pdf_file_path.name, f, "application/pdf")},
                data={"metadata": "{}"},
            )

        data = response.json()
        task_id = data.get("task_status_url", "").split("/")[-1] or data.get("task_id")

        start = time.time()
        while time.time() - start < TASK_TIMEOUT:
            status_response = api_client.get(f"/indexer/task/{task_id}")
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


class TestExtractTextPDF:
    """Tests for extractText tool with PDF files."""

    def test_extract_text_from_pdf(self, api_client, pdf_file_path):
        """Test extracting text from PDF via extractText tool."""
        with open(pdf_file_path, "rb") as f:
            response = api_client.post(
                "/tools/execute",
                files={"file": (pdf_file_path.name, f, "application/pdf")},
                data={"tool": "extractText"},
            )

        assert response.status_code == 200, f"Extract failed: {response.text}"
        result = response.json()

        assert "message" in result
        assert isinstance(result["message"], str)
        assert len(result["message"]) > 0, "Extracted text is empty"

    def test_extract_text_returns_content(self, api_client, pdf_file_path):
        """Test that extracted text contains expected content."""
        with open(pdf_file_path, "rb") as f:
            response = api_client.post(
                "/tools/execute",
                files={"file": (pdf_file_path.name, f, "application/pdf")},
                data={"tool": "extractText"},
            )

        result = response.json()
        text = result.get("message", "")

        # At minimum, we should get some text back
        assert len(text) > 10, f"Extracted text too short: {text}"


class TestPDFErrorHandling:
    """Tests for error handling in PDF processing."""

    def test_invalid_pdf_handling(self, api_client, created_partition, tmp_path):
        """Test that invalid PDF files are handled gracefully."""
        # Create an invalid PDF file
        invalid_pdf = tmp_path / "invalid.pdf"
        invalid_pdf.write_text("This is not a valid PDF file")

        file_id = "invalid-pdf-test"

        with open(invalid_pdf, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{created_partition}/file/{file_id}",
                files={"file": ("invalid.pdf", f, "application/pdf")},
                data={"metadata": "{}"},
            )

        # Upload should be accepted, but task may fail
        if response.status_code in [200, 201, 202]:
            data = response.json()
            task_id = data.get("task_status_url", "").split("/")[-1] or data.get("task_id")

            # Wait and check if it fails gracefully
            start = time.time()
            while time.time() - start < 60:
                status_response = api_client.get(f"/indexer/task/{task_id}")
                status = status_response.json()
                state = status.get("task_state")

                if state in ["COMPLETED", "FAILED"]:
                    break
                time.sleep(1)

            # Either state is acceptable - task should not hang
            assert state in ["COMPLETED", "FAILED"], f"Task stuck in state: {state}"
