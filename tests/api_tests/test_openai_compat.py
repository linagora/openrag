"""OpenAI-compatible API tests."""

import time
import uuid

import pytest

from . import conftest


class TestOpenAICompatibleAPI:
    """Test OpenAI-compatible endpoints.

    Note: These endpoints may not be available if WITH_OPENAI_API=false
    or if no LLM is configured.
    """

    def test_list_models(self, api_client):
        """Test listing models - may not be available without LLM."""
        response = api_client.get("/v1/models")
        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert data["object"] == "list"


    def test_completions_endpoint(self, api_client):
        """Test completions endpoint exists or is disabled."""
        response = api_client.post("/v1/completions", json={"model": "openrag-all", "prompt": "Test"})
        assert response.status_code == 200

        response = api_client.post(
            "/v1/chat/completions", json={"model": "openrag-all", "messages": [{"role": "user", "content": "Test"}]}
        )
        assert response.status_code == 200

    def test_chat_completions_invalid_messages(self, api_client):
        """Test chat completions endpoint with invalid messages."""
        response = api_client.post("/v1/chat/completions", json={"model": "openrag-all", "messages": []})
        assert response.status_code == 400


    def test_chat_completions_invalid_model(self, api_client):
        """Test chat completions with invalid model."""
        response = api_client.post(
            "/v1/chat/completions",
            json={
                "model": "nonexistent-model",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert response.status_code == 404


class TestChatCompletionsMultiPartition:
    """Test chat completions with multi-partition access."""

    @pytest.fixture
    def indexed_partition_for_chat(self, api_client, sample_text_file):
        """Create partition with indexed document for chat tests."""
        partition = f"test-chat-{uuid.uuid4().hex[:8]}"

        # Create partition
        response = api_client.post(f"/partition/{partition}")
        assert response.status_code in [200, 201]

        # Index file
        with open(sample_text_file, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{partition}/file/chat-doc",
                files={"file": ("test.txt", f, "text/plain")},
                data={"metadata": "{}"},
            )

        # Wait for indexing
        if response.status_code in [200, 201]:
            data = response.json()
            if "task_id" in data:
                task_path = f"/indexer/task/{data['task_id']}"
                for _ in range(30):
                    task_response = api_client.get(task_path)
                    if task_response.json().get("task_state") in ["SUCCESS", "COMPLETED"]:
                        break
                    time.sleep(2)

        yield partition

        # Cleanup
        try:
            api_client.delete(f"/partition/{partition}")
        except Exception:
            pass

    def test_chat_completions_with_openrag_all(self, api_client, indexed_partition_for_chat):
        """Test chat completions with openrag-all model."""
        response = api_client.post(
            "/v1/chat/completions",
            json={
                "model": "openrag-all",
                "messages": [{"role": "user", "content": "What is machine learning?"}],
            },
        )
        assert response.status_code == 200
        assert response.json()["model"] == "openrag-all"

    def test_chat_completions_with_specific_partition(self, api_client, indexed_partition_for_chat):
        """Test chat completions with specific partition model."""
        response = api_client.post(
            "/v1/chat/completions",
            json={
                "model": f"openrag-{indexed_partition_for_chat}",
                "messages": [{"role": "user", "content": "What is machine learning?"}],
            },
        )
        assert response.status_code == 200
        assert response.json()["model"] == f"openrag-{indexed_partition_for_chat}"

    def test_models_list_includes_openrag_all(self, api_client, indexed_partition_for_chat):
        """Test that models list includes openrag-all."""
        response = api_client.get("/v1/models")
        assert response.status_code == 200

        data = response.json()
        model_ids = [m["id"] for m in data.get("data", [])]
        assert "openrag-all" in model_ids


class TestChatCompletionsUserAccess:
    """Test chat completions with user access restrictions."""

    @pytest.fixture
    def two_partitions_for_chat(self, api_client, tmp_path):
        """Create two partitions with indexed documents."""
        if not conftest.is_auth_enabled():
            pytest.skip("Authentication is disabled")

        partition1 = f"test-chat-p1-{uuid.uuid4().hex[:8]}"
        partition2 = f"test-chat-p2-{uuid.uuid4().hex[:8]}"

        # Create partitions
        api_client.post(f"/partition/{partition1}")
        api_client.post(f"/partition/{partition2}")

        # Create and index file in partition1
        content = "Machine learning is a subset of artificial intelligence."
        file_path = tmp_path / "test.txt"
        file_path.write_text(content)

        with open(file_path, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{partition1}/file/doc1",
                files={"file": ("test.txt", f, "text/plain")},
                data={"metadata": "{}"},
            )

        # Wait for indexing
        if response.status_code in [200, 201]:
            data = response.json()
            if "task_id" in data:
                task_path = f"/indexer/task/{data['task_id']}"
                for _ in range(30):
                    task_response = api_client.get(task_path)
                    if task_response.json().get("task_state") in ["SUCCESS", "COMPLETED"]:
                        break
                    time.sleep(2)

        yield {"partition1": partition1, "partition2": partition2}

        # Cleanup
        try:
            api_client.delete(f"/partition/{partition1}")
            api_client.delete(f"/partition/{partition2}")
        except Exception:
            pass

    def test_user_chat_all_only_uses_own_partitions(
        self, api_client, user_client, created_user, two_partitions_for_chat
    ):
        """Regular user with openrag-all only queries their accessible partitions."""
        partition1 = two_partitions_for_chat["partition1"]

        # Grant user access to partition1 only
        api_client.post(
            f"/partition/{partition1}/users",
            data={"user_id": created_user["id"], "role": "viewer"},
        )

        # User uses openrag-all model
        response = user_client.post(
            "/v1/chat/completions",
            json={
                "model": "openrag-all",
                "messages": [{"role": "user", "content": "What is machine learning?"}],
            },
        )
        assert response.status_code == 200

    def test_user_cannot_chat_unauthorized_partition(
        self, api_client, user_client, created_user, two_partitions_for_chat
    ):
        """Regular user gets 403 when using model for unauthorized partition."""
        partition1 = two_partitions_for_chat["partition1"]
        partition2 = two_partitions_for_chat["partition2"]

        # Grant user access to partition1 only
        api_client.post(
            f"/partition/{partition1}/users",
            data={"user_id": created_user["id"], "role": "viewer"},
        )

        # User tries to use partition2 model
        response = user_client.post(
            "/v1/chat/completions",
            json={
                "model": f"openrag-{partition2}",
                "messages": [{"role": "user", "content": "test"}],
            },
        )
        assert response.status_code == 403

    def test_user_models_list_only_shows_accessible(
        self, api_client, user_client, created_user, two_partitions_for_chat
    ):
        """Regular user's model list only shows their accessible partitions."""
        partition1 = two_partitions_for_chat["partition1"]
        partition2 = two_partitions_for_chat["partition2"]

        # Grant user access to partition1 only
        api_client.post(
            f"/partition/{partition1}/users",
            data={"user_id": created_user["id"], "role": "viewer"},
        )

        response = user_client.get("/v1/models")
        assert response.status_code == 200

        data = response.json()
        model_ids = [m["id"] for m in data.get("data", [])]

        # Should see partition1 and openrag-all
        assert f"openrag-{partition1}" in model_ids
        assert "openrag-all" in model_ids

        # Should NOT see partition2
        assert f"openrag-{partition2}" not in model_ids
