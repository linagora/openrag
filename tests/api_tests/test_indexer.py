"""Indexer API tests - file upload and indexing."""

import os
import time
from pathlib import Path

import pytest

from .conftest import TASK_TIMEOUT, wait_for_task

# Check if image captioning is enabled (disabled in CI)
IMAGE_CAPTIONING_ENABLED = os.environ.get("IMAGE_CAPTIONING", "").lower() not in ("false", "0", "")

RESOURCES_DIR = Path(__file__).parent.parent / "resources"
PDF_FILE = RESOURCES_DIR / "test_file.pdf"


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

    def test_upload_duplicate_file_replaces(self, api_client, created_partition, sample_text_file):
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

    def test_indexed_file_creates_documents(self, api_client, created_partition, pdf_file_path):
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


class TestSVGIndexing:
    """Test SVG file indexing (Issue #127)."""

    def test_upload_svg_file(self, api_client, created_partition):
        """Test uploading and indexing an SVG file."""
        svg_file = RESOURCES_DIR / "test_file.svg"
        file_id = "test-svg-001"

        with open(svg_file, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{created_partition}/file/{file_id}",
                files={"file": ("test_file.svg", f, "image/svg+xml")},
                data={"metadata": "{}"},
            )

        assert response.status_code in [200, 201, 202]

        data = response.json()
        task_id = get_task_id(data)
        wait_for_task(api_client, task_id)

        # Verify file was indexed
        file_response = api_client.get(f"/partition/{created_partition}/file/{file_id}")
        assert file_response.status_code == 200

        file_data = file_response.json()
        assert "documents" in file_data
        assert len(file_data["documents"]) > 0, "No documents created from SVG file"


@pytest.mark.skipif(not IMAGE_CAPTIONING_ENABLED, reason="IMAGE_CAPTIONING disabled")
class TestImageCaptioning:
    """Test image captioning during file indexing."""

    def test_markdown_with_data_uri_image_gets_captioned(
        self, api_client, created_partition, sample_markdown_with_image
    ):
        """Test that data URI images in markdown are captioned."""
        file_id = "image-caption-test"

        with open(sample_markdown_with_image, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{created_partition}/file/{file_id}",
                files={"file": ("test.md", f, "text/markdown")},
                data={"metadata": "{}"},
            )

        task_id = get_task_id(response.json())
        wait_for_task(api_client, task_id)

        # Retrieve indexed content
        file_response = api_client.get(f"/partition/{created_partition}/file/{file_id}")
        assert file_response.status_code == 200

        file_data = file_response.json()
        documents = file_data.get("documents", [])
        assert len(documents) > 0

        # Verify image was replaced with caption (not raw data URI)
        indexed_content = " ".join(doc.get("page_content", "") for doc in documents)
        assert "data:image/png;base64" not in indexed_content, "Image should be captioned, not raw"
        assert "<image_description>" in indexed_content or "image" in indexed_content.lower()

    def test_markdown_with_http_image_url(self, api_client, created_partition, tmp_path):
        """Test that HTTP image URLs in markdown are handled based on config.

        When image_captioning_url=true: URL should be replaced with caption.
        When image_captioning_url=false: URL should remain unchanged.
        """
        # Use a placeholder URL (won't actually be fetched if captioning disabled)
        image_url = "https://example.com/test-image.png"
        content = f"# Test\n\n![test image]({image_url})\n\nSome text."

        md_file = tmp_path / "test_http_image.md"
        md_file.write_text(content)
        file_id = "http-image-caption-test"

        with open(md_file, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{created_partition}/file/{file_id}",
                files={"file": ("test.md", f, "text/markdown")},
                data={"metadata": "{}"},
            )

        task_id = get_task_id(response.json())
        wait_for_task(api_client, task_id)

        # Retrieve indexed content
        file_response = api_client.get(f"/partition/{created_partition}/file/{file_id}")
        assert file_response.status_code == 200

        file_data = file_response.json()
        documents = file_data.get("documents", [])
        assert len(documents) > 0

        indexed_content = " ".join(doc.get("page_content", "") for doc in documents)

        # Check behavior based on whether URL captioning is enabled
        url_was_captioned = "<image_description>" in indexed_content
        url_preserved = image_url in indexed_content

        # One of these must be true (either captioned or preserved)
        assert url_was_captioned or url_preserved, "HTTP image URL should either be captioned or preserved unchanged"

        # They should be mutually exclusive
        if url_was_captioned:
            assert not url_preserved, "URL should not appear if it was captioned"
        if url_preserved:
            assert not url_was_captioned, "Caption should not appear if URL was preserved"


class TestUserQuotaEnforcement:
    """Test user file quota enforcement during file uploads."""

    def _create_user_with_quota(self, api_client, display_name: str, file_quota: int | None = None):
        """Helper to create a user with specific quota and return user data with token."""
        data = {"display_name": display_name}
        if file_quota is not None:
            data["file_quota"] = file_quota
        response = api_client.post("/users/", data=data)
        assert response.status_code == 201, f"Failed to create user: {response.text}"
        return response.json()

    def _create_partition(self, api_client, partition_name: str, user_token: str):
        """Helper to create a partition for user user given its token."""
        # Create partition as the user
        response = api_client.post(
            f"/partition/{partition_name}",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert response.status_code in [200, 201], f"Failed to create partition: {response.text}"

    def _upload_file(self, api_client, partition: str, file_id: str, user_token: str, content: str = "Test content"):
        """Helper to upload a file as a specific user."""
        import io

        file_obj = io.BytesIO(content.encode())
        headers = {"Authorization": f"Bearer {user_token}"}
        response = api_client.post(
            f"/indexer/partition/{partition}/file/{file_id}",
            files={"file": (f"{file_id}.txt", file_obj, "text/plain")},
            data={"metadata": "{}"},
            headers=headers,
        )
        return response

    def _cleanup_user(self, api_client, user_id: int):
        """Helper to clean up a user."""
        try:
            api_client.delete(f"/users/{user_id}")
        except Exception:
            pass

    def _cleanup_partition(self, api_client, partition_name: str):
        """Helper to clean up a partition."""
        try:
            api_client.delete(f"/partition/{partition_name}")
        except Exception:
            pass

    def test_unlimited_quota_user_can_upload(self, api_client, tmp_path):
        """Test that user with unlimited quota (<0) can upload files."""
        user = self._create_user_with_quota(api_client, "unlimited_quota_user", file_quota=-1)
        user_id = user["id"]
        user_token = user["token"]
        partition_name = f"quota-test-unlimited-{user_id}"

        try:
            self._create_partition(api_client, partition_name, user_token)

            for i in range(2):
                response = self._upload_file(api_client, partition_name, f"file-{i}", user_token, f"Content {i}")
                assert response.status_code in [200, 201, 202], (
                    f"File {i} upload failed with status {response.status_code}: {response.text}"
                )
                data = response.json()
                if "task_status_url" in data:
                    task_id = get_task_id(data)
                    wait_for_task(api_client, task_id, headers={"Authorization": f"Bearer {user_token}"})

        finally:
            self._cleanup_partition(api_client, partition_name)
            self._cleanup_user(api_client, user_id)

    def test_quota_limit_blocks_excess_uploads(self, api_client, tmp_path):
        """Test that user with quota 5 cannot upload a 6th file."""
        # Create user with quota of 5
        user = self._create_user_with_quota(api_client, "limited_quota_user", file_quota=5)
        user_id = user["id"]
        user_token = user["token"]
        partition_name = f"quota-test-limited-{user_id}"

        try:
            self._create_partition(api_client, partition_name, user_token)

            # Upload 5 files successfully
            for i in range(5):
                response = self._upload_file(api_client, partition_name, f"file-{i}", user_token, f"Content {i}")
                assert response.status_code in [200, 201, 202], (
                    f"File {i} upload should succeed, got {response.status_code}: {response.text}"
                )
                # Wait for task to complete
                if response.status_code in [200, 201, 202]:
                    data = response.json()
                    if "task_status_url" in data:
                        task_id = get_task_id(data)
                        wait_for_task(api_client, task_id, headers={"Authorization": f"Bearer {user_token}"})

            # 6th file should be rejected due to quota
            response = self._upload_file(api_client, partition_name, "file-5", user_token, "Content 5")
            assert response.status_code == 403, (
                f"6th file should be rejected with 403 Forbidden, got {response.status_code}: {response.text}"
            )

        finally:
            self._cleanup_partition(api_client, partition_name)
            self._cleanup_user(api_client, user_id)

    def _get_user_file_count(self, api_client, user_token: str) -> int:
        """Helper to get the file_count for a user."""
        response = api_client.get(
            "/users/info",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert response.status_code == 200, f"Failed to get user info: {response.text}"
        return response.json().get("file_count", 0)

    def test_file_count_increments_on_upload(self, api_client):
        """Test that file_count increments when a user uploads files."""
        user = self._create_user_with_quota(api_client, "file_count_test_user", file_quota=-1)
        user_id = user["id"]
        user_token = user["token"]
        partition_name = f"file-count-test-{user_id}"

        try:
            # Initial file_count should be 0
            initial_count = self._get_user_file_count(api_client, user_token)
            assert initial_count == 0, f"Initial file_count should be 0, got {initial_count}"

            self._create_partition(api_client, partition_name, user_token)

            # Upload 3 files and verify count increments
            for i in range(1, 4):
                response = self._upload_file(api_client, partition_name, f"file-{i}", user_token, f"Content {i}")
                assert response.status_code in [200, 201, 202], (
                    f"File {i} upload failed with status {response.status_code}: {response.text}"
                )
                # Wait for task to complete
                data = response.json()
                if "task_status_url" in data:
                    task_id = get_task_id(data)
                    wait_for_task(api_client, task_id, headers={"Authorization": f"Bearer {user_token}"})

                # Verify file_count incremented
                current_count = self._get_user_file_count(api_client, user_token)
                expected_count = i
                assert current_count == expected_count, (
                    f"After uploading file {i}, file_count should be {expected_count}, got {current_count}"
                )

        finally:
            self._cleanup_partition(api_client, partition_name)
            self._cleanup_user(api_client, user_id)

    def test_file_count_decrements_on_delete(self, api_client):
        """Test that file_count decrements when a user deletes files or a partition."""
        user = self._create_user_with_quota(api_client, "file_count_delete_test_user", file_quota=-1)
        user_id = user["id"]
        user_token = user["token"]
        partition_name = f"file-count-delete-test-{user_id}"

        try:
            self._create_partition(api_client, partition_name, user_token)

            # Upload 5 files
            for i in range(5):
                response = self._upload_file(api_client, partition_name, f"file-{i}", user_token, f"Content {i}")
                assert response.status_code in [200, 201, 202]
                data = response.json()
                if "task_status_url" in data:
                    task_id = get_task_id(data)
                    wait_for_task(api_client, task_id, headers={"Authorization": f"Bearer {user_token}"})

            # Verify file_count is 5
            count_after_upload = self._get_user_file_count(api_client, user_token)
            assert count_after_upload == 5, f"After uploading 5 files, file_count should be 5, got {count_after_upload}"

            headers = {"Authorization": f"Bearer {user_token}"}

            # Delete one file
            response = api_client.delete(f"/indexer/partition/{partition_name}/file/file-0", headers=headers)

            assert response.status_code in [200, 204], f"Failed to delete file: {response.text}"

            # Verify file_count decremented to 4
            count_after_delete = self._get_user_file_count(api_client, user_token)
            assert count_after_delete == 4, f"After deleting 1 file, file_count should be 4, got {count_after_delete}"

            # Delete another file
            response = api_client.delete(f"/indexer/partition/{partition_name}/file/file-1", headers=headers)
            assert response.status_code in [200, 204], f"Failed to delete file: {response.text}"

            # Verify file_count decremented to 3
            count_after_second_delete = self._get_user_file_count(api_client, user_token)
            assert count_after_second_delete == 3, (
                f"After deleting 2 files, file_count should be 3, got {count_after_second_delete}"
            )

            # Delete the partition (with remaining 3 files)
            response = api_client.delete(f"/partition/{partition_name}", headers=headers)
            assert response.status_code in [200, 204], f"Failed to delete partition: {response.text}"

            # Verify file_count decremented to 0
            count_after_partition_delete = self._get_user_file_count(api_client, user_token)
            assert count_after_partition_delete == 0, (
                f"After deleting partition with 3 files, file_count should be 0, got {count_after_partition_delete}"
            )

        finally:
            self._cleanup_partition(api_client, partition_name)
            self._cleanup_user(api_client, user_id)

    def test_file_count_tracks_uploader_not_owner(self, api_client):
        """Test that file_count increments for the uploader, not the partition owner."""
        # Create owner (User A) and editor (User B)
        owner = self._create_user_with_quota(api_client, "partition_owner", file_quota=-1)
        editor = self._create_user_with_quota(api_client, "partition_editor", file_quota=-1)
        owner_token = owner["token"]
        editor_token = editor["token"]
        partition_name = f"uploader-track-test-{owner['id']}"

        try:
            self._create_partition(api_client, partition_name, owner_token)

            # Add editor to partition
            response = api_client.post(
                f"/partition/{partition_name}/users",
                data={"user_id": editor["id"], "role": "editor"},
                headers={"Authorization": f"Bearer {owner_token}"},
            )
            assert response.status_code == 201, f"Failed to add editor: {response.text}"

            # Editor uploads 2 files
            for i in range(2):
                response = self._upload_file(
                    api_client, partition_name, f"editor-file-{i}", editor_token, f"Content {i}"
                )
                assert response.status_code in [200, 201, 202]
                data = response.json()
                if "task_status_url" in data:
                    task_id = get_task_id(data)
                    wait_for_task(api_client, task_id, headers={"Authorization": f"Bearer {editor_token}"})

            # Owner uploads 1 file
            response = self._upload_file(api_client, partition_name, "owner-file-0", owner_token, "Owner content")
            assert response.status_code in [200, 201, 202]
            data = response.json()
            if "task_status_url" in data:
                task_id = get_task_id(data)
                wait_for_task(api_client, task_id, headers={"Authorization": f"Bearer {owner_token}"})

            # Verify: editor has count 2, owner has count 1
            editor_count = self._get_user_file_count(api_client, editor_token)
            owner_count = self._get_user_file_count(api_client, owner_token)
            assert editor_count == 2, f"Editor file_count should be 2, got {editor_count}"
            assert owner_count == 1, f"Owner file_count should be 1, got {owner_count}"

        finally:
            self._cleanup_partition(api_client, partition_name)
            self._cleanup_user(api_client, editor["id"])
            self._cleanup_user(api_client, owner["id"])

    def test_partition_delete_decrements_per_uploader(self, api_client):
        """Test that deleting a partition decrements file_count for each uploader independently."""
        user_a = self._create_user_with_quota(api_client, "partition_del_user_a", file_quota=-1)
        user_b = self._create_user_with_quota(api_client, "partition_del_user_b", file_quota=-1)
        token_a = user_a["token"]
        token_b = user_b["token"]
        partition_name = f"partition-del-multi-{user_a['id']}"

        try:
            self._create_partition(api_client, partition_name, token_a)

            # Add user B as editor
            response = api_client.post(
                f"/partition/{partition_name}/users",
                data={"user_id": user_b["id"], "role": "editor"},
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert response.status_code == 201, f"Failed to add user B: {response.text}"

            # User A uploads 2 files
            for i in range(2):
                response = self._upload_file(api_client, partition_name, f"a-file-{i}", token_a, f"A content {i}")
                assert response.status_code in [200, 201, 202]
                data = response.json()
                if "task_status_url" in data:
                    wait_for_task(api_client, get_task_id(data), headers={"Authorization": f"Bearer {token_a}"})

            # User B uploads 2 files
            for i in range(2):
                response = self._upload_file(api_client, partition_name, f"b-file-{i}", token_b, f"B content {i}")
                assert response.status_code in [200, 201, 202]
                data = response.json()
                if "task_status_url" in data:
                    wait_for_task(api_client, get_task_id(data), headers={"Authorization": f"Bearer {token_b}"})

            # Verify counts before deletion
            count_a = self._get_user_file_count(api_client, token_a)
            count_b = self._get_user_file_count(api_client, token_b)
            assert count_a == 2, f"User A should have 2 files, got {count_a}"
            assert count_b == 2, f"User B should have 2 files, got {count_b}"

            # Delete partition — should decrement each uploader's count independently
            response = api_client.delete(f"/partition/{partition_name}")
            assert response.status_code in [200, 204], f"Failed to delete partition: {response.text}"

            # Both users' counts should be 0
            count_a = self._get_user_file_count(api_client, token_a)
            count_b = self._get_user_file_count(api_client, token_b)
            assert count_a == 0, f"User A file_count should be 0 after partition delete, got {count_a}"
            assert count_b == 0, f"User B file_count should be 0 after partition delete, got {count_b}"

        finally:
            self._cleanup_partition(api_client, partition_name)
            self._cleanup_user(api_client, user_b["id"])
            self._cleanup_user(api_client, user_a["id"])

    def test_file_count_stable_on_replace(self, api_client):
        """Test that put_file (replace) keeps file_count unchanged."""
        import io

        user = self._create_user_with_quota(api_client, "replace_test_user", file_quota=-1)
        user_id = user["id"]
        user_token = user["token"]
        partition_name = f"replace-test-{user_id}"
        headers = {"Authorization": f"Bearer {user_token}"}

        try:
            self._create_partition(api_client, partition_name, user_token)

            # Upload a file
            response = self._upload_file(api_client, partition_name, "replaceable-file", user_token, "Original")
            assert response.status_code in [200, 201, 202]
            data = response.json()
            if "task_status_url" in data:
                task_id = get_task_id(data)
                wait_for_task(api_client, task_id, headers=headers)

            count_before = self._get_user_file_count(api_client, user_token)
            assert count_before == 1, f"Expected file_count 1 before replace, got {count_before}"

            # Replace the file via PUT
            file_obj = io.BytesIO(b"Updated content")
            response = api_client.put(
                f"/indexer/partition/{partition_name}/file/replaceable-file",
                files={"file": ("replaceable-file.txt", file_obj, "text/plain")},
                data={"metadata": "{}"},
                headers=headers,
            )
            assert response.status_code in [200, 201, 202], f"Replace failed: {response.text}"
            data = response.json()
            if "task_status_url" in data:
                task_id = get_task_id(data)
                wait_for_task(api_client, task_id, headers=headers)

            # file_count should still be 1
            count_after = self._get_user_file_count(api_client, user_token)
            assert count_after == 1, f"Expected file_count 1 after replace, got {count_after}"

        finally:
            self._cleanup_partition(api_client, partition_name)
            self._cleanup_user(api_client, user_id)
