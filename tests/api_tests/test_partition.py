"""Partition management API tests."""
import pytest


class TestPartitionCRUD:
    """Test partition create, read, update, delete operations."""

    def test_list_partitions(self, api_client):
        """Test listing partitions."""
        response = api_client.get("/partition/")
        assert response.status_code == 200
        data = response.json()
        assert "partitions" in data

    def test_create_partition(self, api_client, test_partition_name):
        """Test creating a new partition."""
        response = api_client.post(f"/partition/{test_partition_name}")
        assert response.status_code in [200, 201]

        # Verify it exists
        response = api_client.get("/partition/")
        partitions = [p["partition"] for p in response.json()["partitions"]]
        assert test_partition_name in partitions

        # Cleanup
        api_client.delete(f"/partition/{test_partition_name}")

    def test_create_duplicate_partition_fails(self, api_client, test_partition_name):
        """Test creating duplicate partition returns error."""
        first_response = api_client.post(f"/partition/{test_partition_name}")
        assert first_response.status_code in [200, 201]

        response = api_client.post(f"/partition/{test_partition_name}")
        assert response.status_code in [400, 409]

        # Cleanup
        api_client.delete(f"/partition/{test_partition_name}")

    def test_delete_partition(self, api_client, test_partition_name):
        """Test deleting a partition."""
        api_client.post(f"/partition/{test_partition_name}")
        response = api_client.delete(f"/partition/{test_partition_name}")
        assert response.status_code in [200, 204]

    def test_list_partition_files_empty(self, api_client, created_partition):
        """Test listing files in empty partition."""
        response = api_client.get(f"/partition/{created_partition}")
        assert response.status_code == 200
        data = response.json()
        assert "files" in data
        assert data["files"] == []

    def test_delete_nonexistent_partition(self, api_client):
        """Test deleting non-existent partition returns error."""
        response = api_client.delete("/partition/nonexistent-partition-xyz123")
        assert response.status_code == 404
