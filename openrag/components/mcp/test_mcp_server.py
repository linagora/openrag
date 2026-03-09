"""Unit tests for MCP server tools wiring.

These tests validate that every `@server.tool` function in `mcp_server.py`
forwards arguments and auth context correctly to the underlying services.

Ray dependencies are stubbed via `sys.modules` before importing `mcp_server`.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest
from components.mcp.auth_context import set_auth_context

# ---------------------------------------------------------------------------
# Stub utils.dependencies BEFORE importing mcp_server
# ---------------------------------------------------------------------------
_stub_deps = ModuleType("utils.dependencies")
_stub_deps.get_vectordb = MagicMock()
_stub_deps.get_task_state_manager = MagicMock(return_value=MagicMock())
_stub_deps.get_indexer = MagicMock()
sys.modules.setdefault("utils.dependencies", _stub_deps)

import mcp_server as mcp_mod  # noqa: E402


class _FakeSearchService:
    def __init__(self):
        self.calls: list[dict] = []

    async def search_documents(self, **kwargs):
        self.calls.append(kwargs)
        return {"tool": "search", "kwargs": kwargs}


class _FakeIndexationService:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def _record(self, name: str, kwargs: dict):
        self.calls.append((name, kwargs))
        return {"tool": name, "kwargs": kwargs}

    async def list_partitions(self, **kwargs):
        return self._record("list_partitions", kwargs)

    async def list_files(self, **kwargs):
        return self._record("list_files", kwargs)

    async def get_file_info(self, **kwargs):
        return self._record("get_file_info", kwargs)

    async def get_file_chunks(self, **kwargs):
        return self._record("get_file_chunks", kwargs)

    async def fuzzy_search_files(self, **kwargs):
        return self._record("fuzzy_search_files", kwargs)

    async def get_task_status(self, **kwargs):
        return self._record("get_task_status", kwargs)

    async def list_my_tasks(self, **kwargs):
        return self._record("list_my_tasks", kwargs)

    async def get_task_logs(self, **kwargs):
        return self._record("get_task_logs", kwargs)

    async def get_chunk_by_id(self, **kwargs):
        return self._record("get_chunk_by_id", kwargs)

    async def delete_file(self, **kwargs):
        return self._record("delete_file", kwargs)

    async def update_file_metadata(self, **kwargs):
        return self._record("update_file_metadata", kwargs)

    async def copy_file(self, **kwargs):
        return self._record("copy_file", kwargs)

    async def index_url(self, **kwargs):
        return self._record("index_url", kwargs)


@pytest.fixture()
def patched_services(monkeypatch):
    search = _FakeSearchService()
    indexation = _FakeIndexationService()
    # mcp_server now routes through `app_service`
    composite = MagicMock()
    composite.search_documents = search.search_documents
    composite.list_partitions = indexation.list_partitions
    composite.list_files = indexation.list_files
    composite.get_file_info = indexation.get_file_info
    composite.get_file_chunks = indexation.get_file_chunks
    composite.fuzzy_search_files = indexation.fuzzy_search_files
    composite.get_task_status = indexation.get_task_status
    composite.list_my_tasks = indexation.list_my_tasks
    composite.get_task_logs = indexation.get_task_logs
    composite.get_chunk_by_id = indexation.get_chunk_by_id
    composite.delete_file = indexation.delete_file
    composite.update_file_metadata = indexation.update_file_metadata
    composite.copy_file = indexation.copy_file
    composite.index_url = indexation.index_url
    monkeypatch.setattr(mcp_mod, "app_service", composite)
    monkeypatch.setattr(mcp_mod, "search_service", composite)
    monkeypatch.setattr(mcp_mod, "indexation_service", composite)
    return search, indexation


@pytest.fixture()
def auth_ctx():
    def _set(user_id=7, partitions=None):
        if partitions is None:
            partitions = ["p1", "p2"]
        set_auth_context(user_id=user_id, partitions=partitions)

    yield _set
    set_auth_context(user_id=None, partitions=None)


@pytest.mark.asyncio
async def test_search_documents_tool(patched_services, auth_ctx):
    search, _ = patched_services
    auth_ctx(user_id=9, partitions=["p1"])

    result = await mcp_mod.search_documents(query="hello", partitions=["p1"], top_k=3)

    assert result["tool"] == "search"
    assert search.calls[-1] == {
        "query": "hello",
        "partitions": ["p1"],
        "top_k": 3,
        "allowed_partitions": ["p1"],
    }


@pytest.mark.asyncio
async def test_search_partition_tool(patched_services, auth_ctx):
    search, _ = patched_services
    auth_ctx(partitions=["p1", "p2"])

    await mcp_mod.search_partition(query="hello", partition="p2", top_k=4)

    assert search.calls[-1] == {
        "query": "hello",
        "partitions": ["p2"],
        "top_k": 4,
        "allowed_partitions": ["p1", "p2"],
    }


@pytest.mark.asyncio
async def test_search_file_tool(patched_services, auth_ctx):
    search, _ = patched_services
    auth_ctx(partitions=["p1"])

    await mcp_mod.search_file(query="hello", partition="p1", file_id="f-1", top_k=2)

    assert search.calls[-1] == {
        "query": "hello",
        "partitions": ["p1"],
        "top_k": 2,
        "file_id": "f-1",
        "allowed_partitions": ["p1"],
    }


@pytest.mark.asyncio
async def test_list_partitions_tool(patched_services, auth_ctx):
    _, idx = patched_services
    auth_ctx(partitions=["all"])

    await mcp_mod.list_partitions()

    assert idx.calls[-1] == ("list_partitions", {"allowed_partitions": ["all"]})


@pytest.mark.asyncio
async def test_list_files_tool(patched_services, auth_ctx):
    _, idx = patched_services
    auth_ctx(partitions=["p1"])

    await mcp_mod.list_files(partition="p1", limit=10)

    assert idx.calls[-1] == (
        "list_files",
        {"partition": "p1", "allowed_partitions": ["p1"], "limit": 10},
    )


@pytest.mark.asyncio
async def test_get_file_info_tool(patched_services, auth_ctx):
    _, idx = patched_services
    auth_ctx(partitions=["p1"])

    await mcp_mod.get_file_info(partition="p1", file_id="file-1")

    assert idx.calls[-1] == (
        "get_file_info",
        {"partition": "p1", "file_id": "file-1", "allowed_partitions": ["p1"]},
    )


@pytest.mark.asyncio
async def test_get_file_chunks_tool(patched_services, auth_ctx):
    _, idx = patched_services
    auth_ctx(partitions=["p1"])

    await mcp_mod.get_file_chunks(partition="p1", file_id="file-1")

    assert idx.calls[-1] == (
        "get_file_chunks",
        {"partition": "p1", "file_id": "file-1", "allowed_partitions": ["p1"], "offset": 0, "limit": 3},
    )


@pytest.mark.asyncio
async def test_fuzzy_search_files_tool(patched_services, auth_ctx):
    _, idx = patched_services
    auth_ctx(partitions=["p1", "p2"])

    await mcp_mod.fuzzy_search_files(query="rep", partition="p2", cutoff=0.5, limit=7)

    assert idx.calls[-1] == (
        "fuzzy_search_files",
        {
            "query": "rep",
            "allowed_partitions": ["p1", "p2"],
            "partition": "p2",
            "cutoff": 0.5,
            "limit": 7,
        },
    )


@pytest.mark.asyncio
async def test_get_indexation_task_status_tool(patched_services, auth_ctx):
    _, idx = patched_services
    auth_ctx(user_id=123, partitions=["p1"])

    await mcp_mod.get_indexation_task_status(task_id="task-1")

    assert idx.calls[-1] == ("get_task_status", {"task_id": "task-1", "user_id": 123})


@pytest.mark.asyncio
async def test_list_my_tasks_tool(patched_services, auth_ctx):
    _, idx = patched_services
    auth_ctx(user_id=42, partitions=["p1"])

    await mcp_mod.list_my_tasks(task_status="active")

    assert idx.calls[-1] == (
        "list_my_tasks",
        {"user_id": 42, "task_status": "active"},
    )


@pytest.mark.asyncio
async def test_get_task_logs_tool(patched_services, auth_ctx):
    _, idx = patched_services
    auth_ctx(user_id=42, partitions=["p1"])

    await mcp_mod.get_task_logs(task_id="task-2", max_lines=55)

    assert idx.calls[-1] == (
        "get_task_logs",
        {
            "task_id": "task-2",
            "user_id": 42,
            "log_file": mcp_mod.LOG_FILE,
            "max_lines": 55,
        },
    )


@pytest.mark.asyncio
async def test_get_chunk_by_id_tool(patched_services, auth_ctx):
    _, idx = patched_services
    auth_ctx(partitions=["p2"])

    await mcp_mod.get_chunk_by_id(chunk_id="chunk-1")

    assert idx.calls[-1] == (
        "get_chunk_by_id",
        {"chunk_id": "chunk-1", "allowed_partitions": ["p2"]},
    )


@pytest.mark.asyncio
async def test_delete_file_tool(patched_services, auth_ctx):
    _, idx = patched_services
    auth_ctx(partitions=["p1"])

    await mcp_mod.delete_file(partition="p1", file_id="f-del")

    assert idx.calls[-1] == (
        "delete_file",
        {"partition": "p1", "file_id": "f-del", "allowed_partitions": ["p1"]},
    )


@pytest.mark.asyncio
async def test_update_file_metadata_tool(patched_services, auth_ctx):
    _, idx = patched_services
    auth_ctx(partitions=["p1"])
    metadata = {"author": "Alice"}

    await mcp_mod.update_file_metadata(partition="p1", file_id="f1", metadata=metadata)

    assert idx.calls[-1] == (
        "update_file_metadata",
        {
            "partition": "p1",
            "file_id": "f1",
            "metadata": metadata,
            "allowed_partitions": ["p1"],
        },
    )


@pytest.mark.asyncio
async def test_copy_file_tool(patched_services, auth_ctx):
    _, idx = patched_services
    auth_ctx(partitions=["src", "dst"])
    extra = {"tag": "copied"}

    await mcp_mod.copy_file(
        source_partition="src",
        source_file_id="f-src",
        dest_partition="dst",
        dest_file_id="f-dst",
        extra_metadata=extra,
    )

    assert idx.calls[-1] == (
        "copy_file",
        {
            "source_partition": "src",
            "source_file_id": "f-src",
            "dest_partition": "dst",
            "dest_file_id": "f-dst",
            "allowed_partitions": ["src", "dst"],
            "extra_metadata": extra,
        },
    )


@pytest.mark.asyncio
async def test_index_url_tool(patched_services, auth_ctx):
    _, idx = patched_services
    auth_ctx(partitions=["p1"])
    extra = {"source": "crawler"}

    await mcp_mod.index_url(
        url="https://example.com/report.pdf",
        partition="p1",
        file_id="report-1",
        extra_metadata=extra,
    )

    assert idx.calls[-1] == (
        "index_url",
        {
            "url": "https://example.com/report.pdf",
            "partition": "p1",
            "file_id": "report-1",
            "allowed_partitions": ["p1"],
            "extra_metadata": extra,
        },
    )
