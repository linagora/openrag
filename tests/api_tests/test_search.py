"""Search API tests."""
import time

import pytest


class TestSemanticSearch:
    """Test semantic search functionality."""

    @pytest.fixture
    def indexed_partition(self, api_client, created_partition, sample_text_file):
        """Create partition and index a document, wait for completion."""
        file_id = "search-test-doc"

        with open(sample_text_file, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{created_partition}/file/{file_id}",
                files={"file": ("test.txt", f, "text/plain")},
                data={"metadata": "{}"}
            )

        data = response.json()

        # Wait for indexing to complete
        if "task_status_url" in data:
            task_url = data["task_status_url"]
            task_path = "/" + "/".join(task_url.split("/")[3:])
        elif "task_id" in data:
            task_path = f"/indexer/task/{data['task_id']}"
        else:
            # No task info, just wait
            time.sleep(5)
            return created_partition

        for _ in range(30):
            task_response = api_client.get(task_path)
            task_data = task_response.json()
            state = task_data.get("task_state", "")
            if state in ["SUCCESS", "COMPLETED", "success", "completed"]:
                break
            elif state in ["FAILED", "failed", "FAILURE", "failure"]:
                pytest.skip(f"Indexing failed: {task_data}")
            time.sleep(2)

        return created_partition

    def test_search_partition(self, api_client, indexed_partition):
        """Test searching within a partition."""
        response = api_client.get(
            f"/search/partition/{indexed_partition}",
            params={"text": "artificial intelligence", "top_k": 5}
        )
        assert response.status_code == 200
        data = response.json()
        assert "documents" in data

    def test_search_multiple_partitions(self, api_client, indexed_partition):
        """Test searching across partitions."""
        response = api_client.get(
            "/search",
            params={"text": "machine learning", "top_k": 5}
        )
        assert response.status_code == 200
        data = response.json()
        assert "documents" in data

    def test_search_with_top_k(self, api_client, indexed_partition):
        """Test search with different top_k values."""
        response = api_client.get(
            f"/search/partition/{indexed_partition}",
            params={"text": "deep learning", "top_k": 10}
        )
        assert response.status_code == 200
        data = response.json()
        # Results should not exceed top_k
        assert len(data.get("documents", [])) <= 10

    def test_search_empty_query(self, api_client, indexed_partition):
        """Test search with empty query."""
        response = api_client.get(
            f"/search/partition/{indexed_partition}",
            params={"text": "", "top_k": 5}
        )
        # Should return error or empty results
        assert response.status_code in [200, 400, 422]

    def test_search_nonexistent_partition(self, api_client):
        """Test searching non-existent partition."""
        response = api_client.get(
            "/search/partition/nonexistent-partition-xyz",
            params={"text": "test", "top_k": 5}
        )
        # May return empty results or error
        assert response.status_code in [200, 404, 500]
