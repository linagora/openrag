"""Search API tests."""

import time
import uuid

import pytest

from . import conftest


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
                data={"metadata": "{}"},
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
            params={"text": "artificial intelligence", "top_k": 5},
        )
        assert response.status_code == 200
        data = response.json()
        assert "documents" in data

    def test_search_multiple_partitions(self, api_client, indexed_partition):
        """Test searching across partitions."""
        response = api_client.get("/search", params={"text": "machine learning", "top_k": 5})
        assert response.status_code == 200
        data = response.json()
        assert "documents" in data

    def test_search_with_top_k(self, api_client, indexed_partition):
        """Test search with different top_k values."""
        response = api_client.get(
            f"/search/partition/{indexed_partition}",
            params={"text": "deep learning", "top_k": 10},
        )
        assert response.status_code == 200
        data = response.json()
        # Results should not exceed top_k
        assert len(data.get("documents", [])) <= 10

    def test_search_empty_query(self, api_client, indexed_partition):
        """Test search with empty query."""
        response = api_client.get(f"/search/partition/{indexed_partition}", params={"text": "", "top_k": 5})
        # Should return error or empty results
        assert response.status_code in [200, 400, 422]

    def test_search_nonexistent_partition(self, api_client):
        """Test searching non-existent partition."""
        response = api_client.get(
            "/search/partition/nonexistent-partition-xyz",
            params={"text": "test", "top_k": 5},
        )
        # May return empty results or error
        assert response.status_code in [200, 404, 500]

    def test_search_all_partitions(self, api_client, indexed_partition):
        """Test searching with partitions=all."""
        response = api_client.get(
            "/search",
            params={"partitions": "all", "text": "machine learning", "top_k": 5},
        )
        assert response.status_code == 200
        data = response.json()
        assert "documents" in data


class TestMultiPartitionUserAccess:
    """Test multi-partition search with user access restrictions."""

    @pytest.fixture
    def two_partitions_with_docs(self, api_client, sample_text_file):
        """Create two partitions with indexed documents."""
        if not conftest.is_auth_enabled():
            pytest.skip("Authentication is disabled")

        partition1 = f"test-p1-{uuid.uuid4().hex[:8]}"
        partition2 = f"test-p2-{uuid.uuid4().hex[:8]}"

        # Create partitions
        resp1 = api_client.post(f"/partition/{partition1}")
        resp2 = api_client.post(f"/partition/{partition2}")
        assert resp1.status_code in [200, 201], f"Failed to create {partition1}: {resp1.text}"
        assert resp2.status_code in [200, 201], f"Failed to create {partition2}: {resp2.text}"

        def _index_and_wait(partition, doc_id):
            with open(sample_text_file, "rb") as f:
                resp = api_client.post(
                    f"/indexer/partition/{partition}/file/{doc_id}",
                    files={"file": ("test.txt", f, "text/plain")},
                    data={"metadata": "{}"},
                )
            assert resp.status_code in [200, 201], f"Indexing failed: {resp.text}"
            data = resp.json()
            if "task_id" in data:
                task_path = f"/indexer/task/{data['task_id']}"
            else:
                return
            for _ in range(30):
                task_response = api_client.get(task_path)
                if task_response.json().get("task_state") in ["SUCCESS", "COMPLETED"]:
                    return
                time.sleep(2)
            pytest.fail(f"Indexing did not complete for {partition}")

        _index_and_wait(partition1, "doc1")
        _index_and_wait(partition2, "doc2")

        yield {"partition1": partition1, "partition2": partition2}

        # Cleanup
        try:
            api_client.delete(f"/partition/{partition1}")
            api_client.delete(f"/partition/{partition2}")
        except Exception:
            pass

    def test_user_search_all_only_sees_own_partitions(
        self, api_client, user_client, created_user, two_partitions_with_docs
    ):
        """Regular user with partitions=all only sees results from their partitions."""
        partition1 = two_partitions_with_docs["partition1"]

        # Grant user access to partition1 only
        api_client.post(
            f"/partition/{partition1}/users",
            data={"user_id": created_user["id"], "role": "viewer"},
        )

        # User searches with partitions=all
        response = user_client.get(
            "/search",
            params={"partitions": "all", "text": "machine learning", "top_k": 10},
        )
        assert response.status_code == 200
        data = response.json()

        # All results should be from partition1 only
        for doc in data.get("documents", []):
            assert doc["metadata"]["partition"] == partition1

    def test_user_cannot_search_unauthorized_partition(
        self, api_client, user_client, created_user, two_partitions_with_docs
    ):
        """Regular user gets 403 when explicitly searching unauthorized partition."""
        partition1 = two_partitions_with_docs["partition1"]
        partition2 = two_partitions_with_docs["partition2"]

        # Grant user access to partition1 only
        api_client.post(
            f"/partition/{partition1}/users",
            data={"user_id": created_user["id"], "role": "viewer"},
        )

        # User tries to explicitly search partition2
        response = user_client.get(
            "/search",
            params={"partitions": partition2, "text": "test", "top_k": 5},
        )
        assert response.status_code == 403

    def test_admin_search_all_sees_all_partitions(self, api_client, two_partitions_with_docs):
        """Admin with partitions=all can see all partitions (if SUPER_ADMIN_MODE)."""
        response = api_client.get(
            "/search",
            params={"partitions": "all", "text": "machine learning", "top_k": 10},
        )
        assert response.status_code == 200
        data = response.json()
        assert "documents" in data
