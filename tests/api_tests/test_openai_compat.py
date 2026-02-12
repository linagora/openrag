"""OpenAI-compatible API tests."""

import json
import time
import uuid

import pytest

from . import conftest
from .conftest import wait_for_indexing


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

    @pytest.mark.parametrize(
        "endpoint, payload",
        [
            (
                "/v1/chat/completions",
                {
                    "model": "",
                    "messages": [{"role": "user", "content": "test " * 20000}],
                    "max_tokens": 100000,
                },
            ),
            (
                "/v1/completions",
                {
                    "model": "",
                    "prompt": "test " * 20000,
                    "max_tokens": 100000,
                },
            ),
        ],
        ids=["chat_completions", "completions"],
    )
    def test_exceeds_token_limit(self, api_client, endpoint, payload):
        """When requested tokens exceed model limit, expect HTTP 413."""
        response = api_client.post(endpoint, json=payload)
        assert response.status_code == 413
        body = response.json()
        assert "exceeds maximum token limit" in body["detail"].lower()


class TestSourceFiltering:
    """Test source citation filtering in chat completion responses.

    These tests require a partition with indexed documents so the RAG pipeline
    produces numbered [Source N] context and the mock LLM appends [Sources: 1].
    """

    @pytest.fixture(scope="class")
    def indexed_partition(self, api_client, tmp_path_factory):
        """Create a partition with an indexed file, shared across all tests in the class."""
        partition = f"test-src-filter-{uuid.uuid4().hex[:8]}"
        response = api_client.post(f"/partition/{partition}")
        assert response.status_code in [200, 201], f"Failed to create partition: {response.text}"

        content = "This is a test document about artificial intelligence and machine learning.\n"
        file_path = tmp_path_factory.mktemp("source_filter") / "test_doc.txt"
        file_path.write_text(content)

        with open(file_path, "rb") as f:
            response = api_client.post(
                f"/indexer/partition/{partition}/file/source-filter-doc",
                files={"file": ("test.txt", f, "text/plain")},
                data={"metadata": "{}"},
            )
        assert response.status_code in [200, 201, 202]
        wait_for_indexing(api_client, response.json())

        yield partition

        try:
            api_client.delete(f"/partition/{partition}")
        except Exception:
            pass

    def test_non_streaming_sources_stripped_from_content(self, api_client, indexed_partition):
        """Non-streaming: [Sources: ...] block should be stripped from response content."""
        response = api_client.post(
            "/v1/chat/completions",
            json={
                "model": f"openrag-{indexed_partition}",
                "messages": [{"role": "user", "content": "Tell me about machine learning"}],
                "stream": False,
            },
        )
        assert response.status_code == 200
        data = response.json()

        # Content should not contain the [Sources: ...] block
        content = data["choices"][0]["message"]["content"]
        assert "[Sources:" not in content, f"Sources block should be stripped, got: {content!r}"

    def test_non_streaming_extra_has_filtered_sources(self, api_client, indexed_partition):
        """Non-streaming: extra field should contain filtered source list."""
        response = api_client.post(
            "/v1/chat/completions",
            json={
                "model": f"openrag-{indexed_partition}",
                "messages": [{"role": "user", "content": "Tell me about machine learning"}],
                "stream": False,
            },
        )
        assert response.status_code == 200
        data = response.json()

        assert "extra" in data
        extra = json.loads(data["extra"])
        assert "sources" in extra
        assert isinstance(extra["sources"], list)
        assert len(extra["sources"]) > 0, "Should have at least one filtered source"

    def test_streaming_content_clean(self, api_client, indexed_partition):
        """Streaming: accumulated content should not contain [Sources: ...] block."""
        with api_client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": f"openrag-{indexed_partition}",
                "messages": [{"role": "user", "content": "Tell me about machine learning"}],
                "stream": True,
            },
        ) as response:
            assert response.status_code == 200

            full_content = ""
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                if "[DONE]" in line:
                    break
                chunk = json.loads(line[len("data: ") :])
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                if delta.get("content"):
                    full_content += delta["content"]

        assert "[Sources:" not in full_content, f"Sources block should be stripped, got: {full_content!r}"

    def test_streaming_has_role_delta(self, api_client, indexed_partition):
        """Streaming: first chunk should contain role delta."""
        with api_client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": f"openrag-{indexed_partition}",
                "messages": [{"role": "user", "content": "Tell me about machine learning"}],
                "stream": True,
            },
        ) as response:
            saw_role = False
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                if "[DONE]" in line:
                    break
                chunk = json.loads(line[len("data: ") :])
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                if "role" in delta:
                    saw_role = True
                    break

        assert saw_role, "Should emit a role delta chunk"

    def test_streaming_has_finish_reason(self, api_client, indexed_partition):
        """Streaming: should emit a chunk with finish_reason before DONE."""
        with api_client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": f"openrag-{indexed_partition}",
                "messages": [{"role": "user", "content": "Tell me about machine learning"}],
                "stream": True,
            },
        ) as response:
            finish_reason = None
            finish_extra = None
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                if "[DONE]" in line:
                    break
                chunk = json.loads(line[len("data: ") :])
                choice = chunk.get("choices", [{}])[0]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                    finish_extra = chunk.get("extra")

        assert finish_reason == "stop", f"Expected finish_reason='stop', got {finish_reason!r}"
        # Finish chunk should carry filtered sources in extra
        assert finish_extra is not None, "Finish chunk should have extra field"
        extra = json.loads(finish_extra)
        assert "sources" in extra
        assert isinstance(extra["sources"], list)


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
