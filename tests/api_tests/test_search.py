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

    @pytest.fixture
    def indexed_folder_partition(self, api_client, created_partition, folder_files):
        """Create partition and index multiple related files with same relationship_id."""

        for filename, (file_path, relationship_id) in folder_files.items():
            file_id = filename.replace(".", "-")

            with open(file_path, "rb") as f:
                response = api_client.post(
                    f"/indexer/partition/{created_partition}/file/{file_id}",
                    files={"file": (filename, f, "text/plain")},
                    data={"metadata": f'{{"relationship_id": "{relationship_id}"}}'},
                )

            data = response.json()

            # Wait for each file to be indexed
            if "task_status_url" in data:
                task_url = data["task_status_url"]
                task_path = "/" + "/".join(task_url.split("/")[3:])
            elif "task_id" in data:
                task_path = f"/indexer/task/{data['task_id']}"
            else:
                time.sleep(3)
                continue

            for _ in range(30):
                task_response = api_client.get(task_path)
                task_data = task_response.json()
                state = task_data.get("task_state", "")
                if state in ["SUCCESS", "COMPLETED", "success", "completed"]:
                    break
                elif state in ["FAILED", "failed", "FAILURE", "failure"]:
                    pytest.skip(f"Indexing failed for {filename}: {task_data}")
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

    def test_search_with_include_related(self, api_client, indexed_folder_partition, exact_match_query, folder_files):
        """Test search with include_related retrieves all files with same relationship_id.

        This test verifies that when searching with include_related=True, and it's relevant for folder or thread relationships.
        """
        # Search without include_related first
        response_without = api_client.get(
            f"/search/partition/{indexed_folder_partition}",
            params={"text": exact_match_query, "top_k": 1, "include_related": False},
        )
        assert response_without.status_code == 200
        data_without = response_without.json()
        assert "documents" in data_without

        # Should get at least one result (the matching chunk)

        initial_count = len(data_without.get("documents", []))
        assert initial_count > 0, "Should find at least one matching chunk"

        # Search with include_related
        response_with = api_client.get(
            f"/search/partition/{indexed_folder_partition}",
            params={"text": exact_match_query, "top_k": 1, "include_related": True},
        )
        assert response_with.status_code == 200
        data_with = response_with.json()
        assert "documents" in data_with

        # Should get more results (chunks from all 3 files in the folder)
        expanded_count = len(data_with.get("documents", []))
        assert expanded_count > initial_count, (
            f"include_related should expand results. Got {expanded_count} vs {initial_count} without"
        )

        # Verify all documents have the same relationship_id
        relationship_ids = {doc["metadata"].get("relationship_id") for doc in data_with["documents"]}
        assert None not in relationship_ids, (
            f"All documents should carry relationship_id metadata. Got: {relationship_ids}"
        )
        relationship_ids = {
            doc["metadata"].get("relationship_id")
            for doc in data_with["documents"]
            if doc["metadata"].get("relationship_id")
        }
        assert len(relationship_ids) == 1, f"All documents should have same relationship_id, got: {relationship_ids}"

        # verify that the relationship_id matches the expected one
        expected_relationship_id = folder_files["file1.txt"][1]  # relationship_id used during indexing
        assert relationship_ids.pop() == expected_relationship_id, (
            f"Documents should have relationship_id {expected_relationship_id}"
        )

        # verify that we got all 3 files' chunks
        filenames = {doc["metadata"].get("filename") for doc in data_with["documents"]}

        expected_filenames = {"file1.txt", "file2.txt", "file3.txt"}
        assert filenames == expected_filenames, f"Expected files {expected_filenames}, got {filenames}"
