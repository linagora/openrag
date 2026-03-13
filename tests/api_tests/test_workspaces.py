"""API integration tests for workspace endpoints."""

import io
import uuid

import pytest

from .conftest import wait_for_indexing

pytestmark = pytest.mark.integration


@pytest.fixture
def workspace_partition(api_client):
    """Create a partition for workspace tests, clean up after."""
    name = f"ws-test-{uuid.uuid4().hex[:8]}"
    response = api_client.post(f"/partition/{name}")
    assert response.status_code in [200, 201]
    yield name
    try:
        api_client.delete(f"/partition/{name}")
    except Exception:
        pass


@pytest.fixture
def workspace_id():
    return f"ws-{uuid.uuid4().hex[:8]}"


class TestWorkspaceCRUD:
    def test_create_workspace(self, api_client, workspace_partition, workspace_id):
        response = api_client.post(
            f"/partition/{workspace_partition}/workspaces",
            json={"workspace_id": workspace_id, "display_name": "Test WS"},
        )
        assert response.status_code == 201
        assert response.json()["workspace_id"] == workspace_id

    def test_list_workspaces(self, api_client, workspace_partition):
        ws_id = f"ws-{uuid.uuid4().hex[:8]}"
        api_client.post(
            f"/partition/{workspace_partition}/workspaces",
            json={"workspace_id": ws_id},
        )
        response = api_client.get(f"/partition/{workspace_partition}/workspaces")
        assert response.status_code == 200
        ws_ids = [w["workspace_id"] for w in response.json()["workspaces"]]
        assert ws_id in ws_ids

    def test_get_workspace(self, api_client, workspace_partition, workspace_id):
        api_client.post(
            f"/partition/{workspace_partition}/workspaces",
            json={"workspace_id": workspace_id},
        )
        response = api_client.get(f"/partition/{workspace_partition}/workspaces/{workspace_id}")
        assert response.status_code == 200
        assert response.json()["workspace_id"] == workspace_id

    def test_get_workspace_not_found(self, api_client, workspace_partition):
        response = api_client.get(f"/partition/{workspace_partition}/workspaces/nonexistent")
        assert response.status_code == 404

    def test_delete_workspace(self, api_client, workspace_partition, workspace_id):
        api_client.post(
            f"/partition/{workspace_partition}/workspaces",
            json={"workspace_id": workspace_id},
        )
        response = api_client.delete(f"/partition/{workspace_partition}/workspaces/{workspace_id}")
        assert response.status_code == 200
        assert response.json()["status"] == "deleted"

        # Verify gone
        response = api_client.get(f"/partition/{workspace_partition}/workspaces/{workspace_id}")
        assert response.status_code == 404


class TestWorkspaceFiles:
    def test_add_files_to_workspace(self, api_client, workspace_partition, workspace_id):
        api_client.post(
            f"/partition/{workspace_partition}/workspaces",
            json={"workspace_id": workspace_id},
        )
        response = api_client.post(
            f"/partition/{workspace_partition}/workspaces/{workspace_id}/files",
            json={"file_ids": ["file-a", "file-b"]},
        )
        assert response.status_code == 200
        assert set(response.json()["file_ids"]) == {"file-a", "file-b"}

    def test_list_workspace_files(self, api_client, workspace_partition, workspace_id):
        api_client.post(
            f"/partition/{workspace_partition}/workspaces",
            json={"workspace_id": workspace_id},
        )
        api_client.post(
            f"/partition/{workspace_partition}/workspaces/{workspace_id}/files",
            json={"file_ids": ["file-a"]},
        )
        response = api_client.get(f"/partition/{workspace_partition}/workspaces/{workspace_id}/files")
        assert response.status_code == 200
        assert "file-a" in response.json()["file_ids"]

    def test_remove_file_from_workspace(self, api_client, workspace_partition, workspace_id):
        api_client.post(
            f"/partition/{workspace_partition}/workspaces",
            json={"workspace_id": workspace_id},
        )
        api_client.post(
            f"/partition/{workspace_partition}/workspaces/{workspace_id}/files",
            json={"file_ids": ["file-a", "file-b"]},
        )
        response = api_client.delete(f"/partition/{workspace_partition}/workspaces/{workspace_id}/files/file-a")
        assert response.status_code == 200

        # Verify file-a is gone but file-b remains
        files_resp = api_client.get(f"/partition/{workspace_partition}/workspaces/{workspace_id}/files")
        assert "file-a" not in files_resp.json()["file_ids"]
        assert "file-b" in files_resp.json()["file_ids"]

    def test_add_same_file_to_multiple_workspaces(self, api_client, workspace_partition):
        ws1 = f"ws-{uuid.uuid4().hex[:8]}"
        ws2 = f"ws-{uuid.uuid4().hex[:8]}"
        api_client.post(f"/partition/{workspace_partition}/workspaces", json={"workspace_id": ws1})
        api_client.post(f"/partition/{workspace_partition}/workspaces", json={"workspace_id": ws2})
        api_client.post(f"/partition/{workspace_partition}/workspaces/{ws1}/files", json={"file_ids": ["shared-file"]})
        api_client.post(f"/partition/{workspace_partition}/workspaces/{ws2}/files", json={"file_ids": ["shared-file"]})

        files1 = api_client.get(f"/partition/{workspace_partition}/workspaces/{ws1}/files").json()["file_ids"]
        files2 = api_client.get(f"/partition/{workspace_partition}/workspaces/{ws2}/files").json()["file_ids"]
        assert "shared-file" in files1
        assert "shared-file" in files2


class TestWorkspaceSearch:
    def test_search_empty_workspace_returns_empty(self, api_client, workspace_partition, workspace_id):
        """Searching an empty workspace should return no results."""
        api_client.post(
            f"/partition/{workspace_partition}/workspaces",
            json={"workspace_id": workspace_id},
        )
        response = api_client.get(
            f"/search/partition/{workspace_partition}",
            params={"text": "test query", "workspace": workspace_id},
        )
        assert response.status_code == 200
        assert response.json()["documents"] == []


class TestPartitionDeletionCascade:
    def test_delete_partition_cascades_workspaces(self, api_client):
        """Deleting a partition should cascade-delete its workspaces."""
        partition = f"ws-cascade-{uuid.uuid4().hex[:8]}"
        ws_id = f"ws-{uuid.uuid4().hex[:8]}"

        api_client.post(f"/partition/{partition}")
        api_client.post(f"/partition/{partition}/workspaces", json={"workspace_id": ws_id})

        # Delete partition
        api_client.delete(f"/partition/{partition}")

        # Workspace should be gone (partition is gone, so 404 from partition check)
        # Re-create partition to verify workspace is gone
        api_client.post(f"/partition/{partition}")
        response = api_client.get(f"/partition/{partition}/workspaces")
        assert response.status_code == 200
        assert response.json()["workspaces"] == []

        # Cleanup
        api_client.delete(f"/partition/{partition}")


class TestFileWorkspaceMembership:
    """Test that file workspace memberships survive file replace operations."""

    def _upload_file(self, api_client, partition: str, file_id: str, content: str = "Test content"):
        file_obj = io.BytesIO(content.encode())
        return api_client.post(
            f"/indexer/partition/{partition}/file/{file_id}",
            files={"file": (f"{file_id}.txt", file_obj, "text/plain")},
            data={"metadata": "{}"},
        )

    def _get_file_workspaces(self, api_client, partition: str, file_id: str) -> list[str]:
        response = api_client.get(f"/partition/{partition}/files/{file_id}/workspaces")
        assert response.status_code == 200
        return response.json()["workspace_ids"]

    def test_workspace_memberships_preserved_after_put(self, api_client, workspace_partition):
        """After a PUT (file replace), the file's workspace memberships must be intact."""
        file_id = f"file-{uuid.uuid4().hex[:8]}"
        ws1 = f"ws-{uuid.uuid4().hex[:8]}"
        ws2 = f"ws-{uuid.uuid4().hex[:8]}"
        ws3 = f"ws-{uuid.uuid4().hex[:8]}"

        # Create 3 workspaces
        for ws in [ws1, ws2, ws3]:
            r = api_client.post(
                f"/partition/{workspace_partition}/workspaces",
                json={"workspace_id": ws},
            )
            assert r.status_code == 201

        # Upload and index the file
        response = self._upload_file(api_client, workspace_partition, file_id)
        assert response.status_code in [200, 201, 202]
        wait_for_indexing(api_client, response.json())

        # Add the file to ws1 and ws2 (not ws3)
        for ws in [ws1, ws2]:
            r = api_client.post(
                f"/partition/{workspace_partition}/workspaces/{ws}/files",
                json={"file_ids": [file_id]},
            )
            assert r.status_code == 200

        # Verify initial membership
        ws_before = self._get_file_workspaces(api_client, workspace_partition, file_id)
        assert set(ws_before) == {ws1, ws2}

        # Replace the file via PUT
        replace_obj = io.BytesIO(b"Updated content")
        r = api_client.put(
            f"/indexer/partition/{workspace_partition}/file/{file_id}",
            files={"file": (f"{file_id}.txt", replace_obj, "text/plain")},
            data={"metadata": "{}"},
        )
        assert r.status_code in [200, 201, 202]
        wait_for_indexing(api_client, r.json())

        # Workspace memberships must be restored
        ws_after = self._get_file_workspaces(api_client, workspace_partition, file_id)
        assert set(ws_after) == {ws1, ws2}, (
            f"Expected workspace memberships {{{ws1}, {ws2}}} after PUT, got {set(ws_after)}"
        )
        assert ws3 not in ws_after

    def test_workspace_memberships_preserved_after_patch(self, api_client, workspace_partition):
        """After a PATCH (metadata update), the file's workspace memberships must be intact."""
        file_id = f"file-{uuid.uuid4().hex[:8]}"
        ws1 = f"ws-{uuid.uuid4().hex[:8]}"
        ws2 = f"ws-{uuid.uuid4().hex[:8]}"
        ws3 = f"ws-{uuid.uuid4().hex[:8]}"

        # Create 3 workspaces
        for ws in [ws1, ws2, ws3]:
            r = api_client.post(
                f"/partition/{workspace_partition}/workspaces",
                json={"workspace_id": ws},
            )
            assert r.status_code == 201

        # Upload and index the file
        response = self._upload_file(api_client, workspace_partition, file_id)
        assert response.status_code in [200, 201, 202]
        wait_for_indexing(api_client, response.json())

        # Add the file to ws1 and ws2 (not ws3)
        for ws in [ws1, ws2]:
            r = api_client.post(
                f"/partition/{workspace_partition}/workspaces/{ws}/files",
                json={"file_ids": [file_id]},
            )
            assert r.status_code == 200

        # Verify initial membership
        ws_before = self._get_file_workspaces(api_client, workspace_partition, file_id)
        assert set(ws_before) == {ws1, ws2}

        # Update file metadata via PATCH
        r = api_client.patch(
            f"/indexer/partition/{workspace_partition}/file/{file_id}",
            data={"metadata": '{"updated": true}'},
        )
        assert r.status_code == 200

        # Workspace memberships must still be intact
        ws_after = self._get_file_workspaces(api_client, workspace_partition, file_id)
        assert set(ws_after) == {ws1, ws2}, (
            f"Expected workspace memberships {{{ws1}, {ws2}}} after PATCH, got {set(ws_after)}"
        )
        assert ws3 not in ws_after
