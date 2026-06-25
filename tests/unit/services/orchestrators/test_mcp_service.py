"""Unit tests for :class:`MCPService` — the MCP application orchestrator.

The orchestrators it composes are replaced by small fakes so these tests
exercise the ACL scope/role enforcement, response shaping, pagination,
fuzzy ranking, task assembly and URL-indexation guards in isolation.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from services.orchestrators.mcp_service import MCPService

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeChunk:
    def __init__(self, *, _id, text, metadata=None):
        self.id = str(_id)
        self.text = text
        self._meta = {"_id": _id, **(metadata or {})}

    def to_langchain(self, *, with_id: bool = True):
        return SimpleNamespace(metadata=dict(self._meta))


class FakeRetrieval:
    def __init__(self, *, chunks=None):
        self._chunks = chunks if chunks is not None else []
        self.calls: list[dict] = []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return list(self._chunks)


class FakePartitions:
    def __init__(self, *, partitions=None, files=None, exists=True, chunks=None, members=None, partition_exists=True):
        self._partitions = partitions if partitions is not None else []
        self._files = files if files is not None else {}
        self._exists = exists
        self._chunks = chunks if chunks is not None else []
        self._members = members if members is not None else []
        self._partition_exists = partition_exists
        self.created: list[tuple[str, int | None, int | None]] = []

    async def list_partitions(self):
        return list(self._partitions)

    async def list_files(self, partition, limit=None):
        files = self._files.get(partition, []) if isinstance(self._files, dict) else list(self._files)
        return files[:limit] if limit is not None else files

    async def file_exists(self, file_id, partition):
        return self._exists

    async def get_file_chunks(self, partition, file_id, limit=2000):
        return list(self._chunks)

    async def list_members(self, partition):
        return list(self._members)

    async def partition_exists(self, partition):
        return self._partition_exists

    async def create_partition(self, partition, user_id, *, max_owned=None):
        # Mirror PgPartitionRepository: the creator is granted owner.
        self.created.append((partition, user_id, max_owned))
        self._members.append({"user_id": user_id, "role": "owner"})


class FakeIndexing:
    def __init__(self, *, state="COMPLETED", error="boom"):
        self._state = state
        self._error = error
        self.deleted: list[tuple[str, str]] = []
        self.updated: list[tuple] = []
        self.copied: list[dict] = []
        self.added: list[dict] = []

    async def get_task_state(self, task_id):
        return self._state

    async def get_task_error(self, task_id):
        return self._error

    async def delete_file(self, file_id, partition):
        self.deleted.append((file_id, partition))

    async def update_metadata(self, file_id, metadata, partition, user):
        self.updated.append((file_id, metadata, partition, user))

    async def copy_file(self, **kwargs):
        self.copied.append(kwargs)

    async def add_file(self, **kwargs):
        self.added.append(kwargs)
        return "task-123"


class FakeJobs:
    def __init__(self, *, details=None, tasks=None):
        self._details = details
        self._tasks = tasks if tasks is not None else []

    async def get_task_details(self, task_id):
        return self._details

    async def list_tasks(self, *, is_admin, user_id, task_status=None):
        self.last = {"is_admin": is_admin, "user_id": user_id, "task_status": task_status}
        return [dict(t) for t in self._tasks]


class FakeConversion:
    def __init__(self, *, chunk=None):
        self._chunk = chunk

    async def get_chunk(self, chunk_id):
        return self._chunk


class FakeVectorStore:
    def __init__(self, *, rows=None):
        self._rows = rows if rows is not None else []
        self.queries: list[tuple] = []

    async def query_chunks_by_filter(self, collection, filters, output_fields=None):
        self.queries.append((collection, filters, output_fields))
        return [dict(r) for r in self._rows]


def _service(
    *,
    retrieval=None,
    partitions=None,
    indexing=None,
    jobs=None,
    conversion=None,
    vector_store=None,
    default_top_k=5,
    max_top_k=50,
    similarity_threshold=0.8,
):
    return MCPService(
        retrieval_service=retrieval or FakeRetrieval(),
        partition_service=partitions or FakePartitions(),
        indexing_service=indexing or FakeIndexing(),
        job_service=jobs or FakeJobs(),
        conversion_service=conversion or FakeConversion(),
        vector_store=vector_store or FakeVectorStore(),
        collection="chunks",
        default_top_k=default_top_k,
        max_top_k=max_top_k,
        similarity_threshold=similarity_threshold,
    )


# ---------------------------------------------------------------------------
# Search + scope ACL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_empty_query_raises():
    with pytest.raises(ValueError):
        await _service().search_documents(query="   ", partitions=None, top_k=None, allowed_partitions=["all"])


@pytest.mark.asyncio
async def test_search_missing_auth_context_raises():
    with pytest.raises(PermissionError):
        await _service().search_documents(query="hi", partitions=None, top_k=None, allowed_partitions=None)


@pytest.mark.asyncio
async def test_search_admin_wildcard_passthrough():
    retrieval = FakeRetrieval()
    await _service(retrieval=retrieval).search_documents(
        query="hi", partitions=None, top_k=None, allowed_partitions=["all"]
    )
    assert retrieval.calls[0]["partitions"] == ["all"]


@pytest.mark.asyncio
async def test_search_all_request_resolves_to_allowed():
    retrieval = FakeRetrieval()
    await _service(retrieval=retrieval).search_documents(
        query="hi", partitions=["all"], top_k=None, allowed_partitions=["a", "b"]
    )
    assert retrieval.calls[0]["partitions"] == ["a", "b"]


@pytest.mark.asyncio
async def test_search_all_with_empty_allowed_partitions_fails_closed():
    with pytest.raises(PermissionError, match="No accessible partitions"):
        await _service().search_documents(query="hi", partitions=["all"], top_k=None, allowed_partitions=[])


@pytest.mark.asyncio
async def test_search_denied_partition_raises():
    with pytest.raises(PermissionError):
        await _service().search_documents(query="hi", partitions=["c"], top_k=None, allowed_partitions=["a", "b"])


@pytest.mark.asyncio
async def test_search_top_k_capped_to_max():
    retrieval = FakeRetrieval()
    out = await _service(retrieval=retrieval, max_top_k=50).search_documents(
        query="hi", partitions=["a"], top_k=999, allowed_partitions=["a"]
    )
    assert retrieval.calls[0]["top_k"] == 50
    assert out["top_k"] == 50


@pytest.mark.asyncio
async def test_search_top_k_default_used():
    retrieval = FakeRetrieval()
    await _service(retrieval=retrieval, default_top_k=7).search_documents(
        query="hi", partitions=["a"], top_k=None, allowed_partitions=["a"]
    )
    assert retrieval.calls[0]["top_k"] == 7


@pytest.mark.asyncio
async def test_search_nonpositive_top_k_raises():
    with pytest.raises(ValueError):
        await _service().search_documents(query="hi", partitions=["a"], top_k=0, allowed_partitions=["a"])


@pytest.mark.asyncio
async def test_search_file_id_builds_filter():
    retrieval = FakeRetrieval()
    svc = _service(retrieval=retrieval)
    await svc.search_documents(query="hi", partitions=["a"], top_k=3, allowed_partitions=["a"], file_id="f1")
    call = retrieval.calls[0]
    # Inlined as a literal expr (the shared searcher drops filter_params).
    assert call["filter"] == 'file_id == "f1"'
    assert "filter_params" not in call


@pytest.mark.asyncio
async def test_search_rejects_file_id_with_slash():
    with pytest.raises(ValueError):
        await _service().search_documents(
            query="hi", partitions=["a"], top_k=3, allowed_partitions=["a"], file_id="a/b"
        )


@pytest.mark.asyncio
async def test_search_shapes_chunks():
    chunks = [FakeChunk(_id=11, text="body", metadata={"file_id": "f1"})]
    out = await _service(retrieval=FakeRetrieval(chunks=chunks)).search_documents(
        query="hi", partitions=["a"], top_k=3, allowed_partitions=["a"]
    )
    assert out["count"] == 1
    doc = out["documents"][0]
    assert doc["chunk_id"] == 11
    assert doc["content"] == "body"
    assert doc["metadata"]["file_id"] == "f1"


# ---------------------------------------------------------------------------
# Partitions & files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_partitions_filtered_by_allowed():
    parts = [{"partition": "a"}, {"partition": "b"}, {"partition": "c"}]
    out = await _service(partitions=FakePartitions(partitions=parts)).list_partitions(allowed_partitions=["a", "c"])
    assert {p["partition"] for p in out["partitions"]} == {"a", "c"}
    assert out["count"] == 2


@pytest.mark.asyncio
async def test_list_partitions_admin_sees_all():
    parts = [{"partition": "a"}, {"partition": "b"}]
    out = await _service(partitions=FakePartitions(partitions=parts)).list_partitions(allowed_partitions=["all"])
    assert out["count"] == 2


@pytest.mark.asyncio
async def test_list_files_denied_raises():
    with pytest.raises(PermissionError):
        await _service().list_files(partition="x", allowed_partitions=["a"])


@pytest.mark.asyncio
async def test_get_file_info_not_found():
    svc = _service(partitions=FakePartitions(exists=False))
    with pytest.raises(FileNotFoundError):
        await svc.get_file_info(partition="a", file_id="missing", allowed_partitions=["a"])


@pytest.mark.asyncio
async def test_get_file_info_strips_id_text_vector_from_metadata():
    rows = [{"_id": 1, "file_id": "f1", "page": 2, "text": "body", "vector": [0.1, 0.2]}]
    svc = _service(partitions=FakePartitions(exists=True), vector_store=FakeVectorStore(rows=rows))
    out = await svc.get_file_info(partition="a", file_id="f1", allowed_partitions=["a"])
    assert out["chunk_count"] == 1
    assert {"_id", "text", "vector"}.isdisjoint(out["metadata"])
    assert out["metadata"]["file_id"] == "f1"


@pytest.mark.asyncio
async def test_get_file_info_count_not_capped():
    # Exact count from the vector store, not PartitionService's 2000 cap.
    rows = [{"_id": i, "file_id": "f1"} for i in range(2500)]
    svc = _service(partitions=FakePartitions(exists=True), vector_store=FakeVectorStore(rows=rows))
    out = await svc.get_file_info(partition="a", file_id="f1", allowed_partitions=["a"])
    assert out["chunk_count"] == 2500


@pytest.mark.asyncio
async def test_get_file_chunks_paginates_with_content():
    rows = [{"_id": i, "text": f"c{i}", "file_id": "f1", "partition": "a"} for i in range(5)]
    svc = _service(vector_store=FakeVectorStore(rows=rows))
    out = await svc.get_file_chunks(partition="a", file_id="f1", allowed_partitions=["a"], offset=1, limit=2)
    assert out["total_chunks"] == 5
    assert out["has_more"] is True
    assert [c["chunk_id"] for c in out["chunks"]] == [1, 2]
    assert out["chunks"][0]["content"] == "c1"
    assert "text" not in out["chunks"][0]["metadata"]


@pytest.mark.asyncio
async def test_get_file_chunks_limit_all():
    rows = [{"_id": i, "text": f"c{i}"} for i in range(3)]
    svc = _service(vector_store=FakeVectorStore(rows=rows))
    out = await svc.get_file_chunks(partition="a", file_id="f1", allowed_partitions=["a"], offset=0, limit=-1)
    assert len(out["chunks"]) == 3
    assert out["has_more"] is False


@pytest.mark.asyncio
async def test_fuzzy_search_ranks_and_cuts_off():
    files = {
        "a": [{"file_id": "annual_report", "filename": "annual_report.pdf"}, {"file_id": "zzz", "filename": "zzz.txt"}]
    }
    svc = _service(partitions=FakePartitions(files=files))
    out = await svc.fuzzy_search_files(query="annual report", allowed_partitions=["a"], partition="a", cutoff=0.4)
    assert out["count"] == 1
    assert out["files"][0]["file_id"] == "annual_report"
    assert "score" in out["files"][0]


@pytest.mark.asyncio
async def test_fuzzy_search_empty_query_raises():
    with pytest.raises(ValueError):
        await _service().fuzzy_search_files(query="  ", allowed_partitions=["a"])


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_status_not_found():
    with pytest.raises(KeyError):
        await _service(jobs=FakeJobs(details=None)).get_task_status(task_id="t1", user_id=1, is_admin=False)


@pytest.mark.asyncio
async def test_task_status_owner_mismatch_raises():
    jobs = FakeJobs(details={"user_id": 2})
    with pytest.raises(PermissionError):
        await _service(jobs=jobs).get_task_status(task_id="t1", user_id=1, is_admin=False)


@pytest.mark.asyncio
async def test_task_status_admin_bypasses_owner():
    jobs = FakeJobs(details={"user_id": 2})
    out = await _service(jobs=jobs, indexing=FakeIndexing(state="COMPLETED")).get_task_status(
        task_id="t1", user_id=1, is_admin=True
    )
    assert out["task_state"] == "COMPLETED"
    assert "error" not in out


@pytest.mark.asyncio
async def test_task_status_failed_redacts_error_for_non_admin():
    jobs = FakeJobs(details={"user_id": 1})
    out = await _service(jobs=jobs, indexing=FakeIndexing(state="FAILED", error="kaboom")).get_task_status(
        task_id="t1", user_id=1, is_admin=False
    )
    assert out["task_state"] == "FAILED"
    assert out["error"] == "Task failed. Contact an administrator for details."


@pytest.mark.asyncio
async def test_task_status_failed_includes_raw_error_for_admin():
    jobs = FakeJobs(details={"user_id": 1})
    out = await _service(jobs=jobs, indexing=FakeIndexing(state="FAILED", error="kaboom")).get_task_status(
        task_id="t1", user_id=1, is_admin=True
    )
    assert out["error"] == "kaboom"


@pytest.mark.asyncio
async def test_list_my_tasks_forwards_scope_and_redacts_failed_for_non_admin():
    jobs = FakeJobs(
        tasks=[
            {"task_id": "t1", "state": "FAILED", "details": {}},
            {"task_id": "t2", "state": "COMPLETED", "details": {}},
        ]
    )
    out = await _service(jobs=jobs, indexing=FakeIndexing(error="why")).list_my_tasks(
        user_id=5, is_admin=False, task_status="failed"
    )
    assert jobs.last == {"is_admin": False, "user_id": 5, "task_status": "failed"}
    failed = next(t for t in out["tasks"] if t["task_id"] == "t1")
    assert failed["error"] == "Task failed. Contact an administrator for details."
    completed = next(t for t in out["tasks"] if t["task_id"] == "t2")
    assert "error" not in completed


@pytest.mark.asyncio
async def test_list_my_tasks_keeps_raw_failed_error_for_admin():
    jobs = FakeJobs(tasks=[{"task_id": "t1", "state": "FAILED", "details": {}}])
    out = await _service(jobs=jobs, indexing=FakeIndexing(error="why")).list_my_tasks(
        user_id=5, is_admin=True, task_status="failed"
    )
    assert out["tasks"][0]["error"] == "why"


@pytest.mark.asyncio
async def test_get_task_logs_ownership_and_parsing(tmp_path):
    log = tmp_path / "app.json"
    lines = [
        {"record": {"time": {"repr": "T1"}, "level": {"name": "INFO"}, "message": "first", "extra": {"task_id": "t1"}}},
        {
            "record": {
                "time": {"repr": "T2"},
                "level": {"name": "INFO"},
                "message": "other",
                "extra": {"task_id": "zzz"},
            }
        },
        {
            "record": {
                "time": {"repr": "T3"},
                "level": {"name": "ERROR"},
                "message": "second",
                "extra": {"task_id": "t1"},
            }
        },
    ]
    log.write_text("\n".join(json.dumps(line) for line in lines))
    svc = _service(jobs=FakeJobs(details={"user_id": 1}))
    out = await svc.get_task_logs(task_id="t1", user_id=1, is_admin=False, log_file=log, max_lines=100)
    assert out["count"] == 2
    assert "first" in out["logs"][0]
    assert "second" in out["logs"][1]


@pytest.mark.asyncio
async def test_get_task_logs_missing_file_raises(tmp_path):
    svc = _service(jobs=FakeJobs(details={"user_id": 1}))
    with pytest.raises(FileNotFoundError):
        await svc.get_task_logs(task_id="t1", user_id=1, is_admin=False, log_file=tmp_path / "nope.json")


@pytest.mark.asyncio
async def test_get_task_logs_rejects_out_of_range_max_lines(tmp_path):
    # The shared core.collect_task_logs enforces the 1..MAX_TASK_LOG_LINES bound,
    # same as the admin task-logs route.
    log = tmp_path / "app.json"
    log.write_text("")
    svc = _service(jobs=FakeJobs(details={"user_id": 1}))
    with pytest.raises(ValueError):
        await svc.get_task_logs(task_id="t1", user_id=1, is_admin=False, log_file=log, max_lines=10_000)


# ---------------------------------------------------------------------------
# Chunk lookup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_chunk_by_id_not_found():
    with pytest.raises(KeyError):
        await _service(conversion=FakeConversion(chunk=None)).get_chunk_by_id(chunk_id="9", allowed_partitions=["a"])


@pytest.mark.asyncio
async def test_get_chunk_by_id_partition_denied():
    chunk = {"page_content": "x", "metadata": {"partition": "secret"}}
    with pytest.raises(PermissionError):
        await _service(conversion=FakeConversion(chunk=chunk)).get_chunk_by_id(chunk_id="9", allowed_partitions=["a"])


@pytest.mark.asyncio
async def test_get_chunk_by_id_allows_in_scope():
    chunk = {"page_content": "x", "metadata": {"partition": "a"}}
    out = await _service(conversion=FakeConversion(chunk=chunk)).get_chunk_by_id(chunk_id="9", allowed_partitions=["a"])
    assert out["page_content"] == "x"


# ---------------------------------------------------------------------------
# Write operations + editor ACL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_file_requires_editor():
    parts = FakePartitions(members=[{"user_id": 1, "role": "viewer"}])
    with pytest.raises(PermissionError):
        await _service(partitions=parts).delete_file(partition="a", file_id="f1", allowed_partitions=["a"], user_id=1)


@pytest.mark.asyncio
async def test_delete_file_editor_succeeds():
    parts = FakePartitions(members=[{"user_id": 1, "role": "editor"}], exists=True)
    indexing = FakeIndexing()
    out = await _service(partitions=parts, indexing=indexing).delete_file(
        partition="a", file_id="f1", allowed_partitions=["a"], user_id=1
    )
    assert indexing.deleted == [("f1", "a")]
    assert "deleted" in out["message"]


@pytest.mark.asyncio
async def test_delete_file_admin_wildcard_bypasses_membership():
    indexing = FakeIndexing()
    await _service(partitions=FakePartitions(exists=True), indexing=indexing).delete_file(
        partition="a", file_id="f1", allowed_partitions=["all"], user_id=1
    )
    assert indexing.deleted == [("f1", "a")]


@pytest.mark.asyncio
async def test_delete_file_invalid_file_id():
    with pytest.raises(ValueError):
        await _service().delete_file(partition="a", file_id="bad/id", allowed_partitions=["all"], user_id=1)


@pytest.mark.asyncio
async def test_copy_file_dest_exists_raises():
    parts = FakePartitions(exists=True)  # both source and dest "exist"
    with pytest.raises(FileExistsError):
        await _service(partitions=parts).copy_file(
            source_partition="a",
            source_file_id="s",
            dest_partition="a",
            dest_file_id="d",
            allowed_partitions=["all"],
            user_id=1,
        )


@pytest.mark.asyncio
async def test_copy_file_success():
    # source exists, dest does not
    class P(FakePartitions):
        async def file_exists(self, file_id, partition):
            return file_id == "s"

    indexing = FakeIndexing()
    out = await _service(partitions=P(), indexing=indexing).copy_file(
        source_partition="a",
        source_file_id="s",
        dest_partition="b",
        dest_file_id="d",
        allowed_partitions=["all"],
        user_id=1,
    )
    assert indexing.copied[0]["source_file_id"] == "s"
    assert indexing.copied[0]["target_partition"] == "b"
    assert out["dest_file_id"] == "d"


# ---------------------------------------------------------------------------
# index_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_url_rejects_non_http_scheme():
    with pytest.raises(ValueError):
        await _service(partitions=FakePartitions(exists=False)).index_url(
            url="ftp://x/y.pdf", partition="a", file_id="f1", allowed_partitions=["all"], user_id=1
        )


@pytest.mark.asyncio
async def test_index_url_existing_file_raises(monkeypatch):
    parts = FakePartitions(exists=True)
    with pytest.raises(FileExistsError):
        await _service(partitions=parts).index_url(
            url="https://x/y.pdf", partition="a", file_id="f1", allowed_partitions=["all"], user_id=1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x.pdf",  # loopback
        "http://localhost/x.pdf",  # localhost name
        "http://169.254.169.254/latest/meta-data",  # cloud metadata
        "http://10.0.0.5/x.pdf",  # private
        "http://2130706433/x.pdf",  # decimal-encoded 127.0.0.1
        "file:///etc/passwd",  # non-http scheme
    ],
)
async def test_index_url_blocks_ssrf_targets(url):
    parts = FakePartitions(exists=False, partition_exists=False)
    with pytest.raises(ValueError):
        await _service(partitions=parts).index_url(
            url=url, partition="newpart", file_id="f1", allowed_partitions=["all"], user_id=1
        )


@pytest.mark.asyncio
async def test_assert_host_resolves_safely_blocks_dns_to_private(monkeypatch):
    from services.orchestrators import mcp_service as mcp_mod

    class FakeLoop:
        async def getaddrinfo(self, host, port, type=None):
            return [(0, 0, 0, "", ("169.254.169.254", 0))]  # cloud metadata

    monkeypatch.setattr(mcp_mod.asyncio, "get_running_loop", lambda: FakeLoop())
    with pytest.raises(ValueError):
        await MCPService._assert_host_resolves_safely("metadata.evil.example")


@pytest.mark.asyncio
async def test_assert_host_resolves_safely_allows_public(monkeypatch):
    from services.orchestrators import mcp_service as mcp_mod

    class FakeLoop:
        async def getaddrinfo(self, host, port, type=None):
            return [(0, 0, 0, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(mcp_mod.asyncio, "get_running_loop", lambda: FakeLoop())
    await MCPService._assert_host_resolves_safely("dns.example")  # no raise


@pytest.mark.asyncio
async def test_index_url_auto_creates_partition_and_indexes(monkeypatch):
    parts = FakePartitions(exists=False, partition_exists=False)
    indexing = FakeIndexing()
    svc = _service(partitions=parts, indexing=indexing)

    async def fake_download(url, dest):
        dest.write_bytes(b"data")

    monkeypatch.setattr(svc, "_safe_download", fake_download)

    out = await svc.index_url(
        url="https://example.com/report.pdf",
        partition="newpart",
        file_id="f1",
        allowed_partitions=["other"],
        user_id=7,
        extra_metadata={"author": "me", "created_by": 999},  # created_by must be stripped
    )
    # auto-created the missing partition, owned by the caller
    assert parts.created == [("newpart", 7, 100)]
    added = indexing.added[0]
    assert added["file_id"] == "f1"
    assert added["partition"] == "newpart"
    assert added["sanitized_filename"] == "report.pdf"
    assert added["metadata"]["source_url"] == "https://example.com/report.pdf"
    assert added["metadata"]["author"] == "me"
    assert "created_by" not in added["metadata"]  # protected key dropped
    assert out["task_id"] == "task-123"


@pytest.mark.asyncio
async def test_safe_download_rejects_redirect_to_private(monkeypatch, tmp_path):
    # A public host that 302-redirects to a loopback target must be rejected on
    # the re-validated hop, not followed.
    from services.orchestrators import mcp_service as mcp_mod

    def handler(request):
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    transport = httpx.MockTransport(handler)
    real_async_client = mcp_mod.httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(mcp_mod.httpx, "AsyncClient", factory)

    svc = _service()

    async def _resolves_ok(host):  # skip real DNS for the initial public host
        return None

    monkeypatch.setattr(svc, "_assert_host_resolves_safely", _resolves_ok)

    with pytest.raises(ValueError, match="disallowed"):
        await svc._safe_download("http://public.example/file.pdf", tmp_path / "out")


# ---------------------------------------------------------------------------
# Metadata hardening + input bounds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_metadata_strips_protected_keys_keeps_move():
    indexing = FakeIndexing()
    await _service(partitions=FakePartitions(exists=True), indexing=indexing).update_file_metadata(
        partition="a",
        file_id="f1",
        metadata={"author": "x", "source": "/evil", "created_by": 999, "partition": "dest"},
        allowed_partitions=["all"],
        user_id=1,
    )
    _file_id, sent_md, _partition, _user = indexing.updated[0]
    assert sent_md["author"] == "x"
    assert sent_md["partition"] == "dest"  # authorized move control preserved
    assert "source" not in sent_md and "created_by" not in sent_md


@pytest.mark.asyncio
async def test_update_metadata_move_requires_editor_on_destination():
    # Non-wildcard caller: editor on source but only viewer on the move target.
    class P(FakePartitions):
        async def list_members(self, partition):
            role = "viewer" if partition == "dest" else "editor"
            return [{"user_id": 5, "role": role}]

    svc = _service(partitions=P(exists=True))
    with pytest.raises(PermissionError):
        await svc.update_file_metadata(
            partition="src",
            file_id="f1",
            metadata={"partition": "dest"},
            allowed_partitions=["src", "dest"],
            user_id=5,
        )


@pytest.mark.asyncio
async def test_get_file_chunks_rejects_bad_offset_and_limit():
    svc = _service(partitions=FakePartitions(exists=True))
    with pytest.raises(ValueError):
        await svc.get_file_chunks(partition="a", file_id="f1", allowed_partitions=["a"], offset=-1)
    with pytest.raises(ValueError):
        await svc.get_file_chunks(partition="a", file_id="f1", allowed_partitions=["a"], limit=0)


@pytest.mark.asyncio
async def test_get_file_chunks_caps_page_size():
    from services.orchestrators.mcp_service import _MAX_CHUNKS_PER_CALL

    rows = [{"_id": i, "text": f"c{i}"} for i in range(_MAX_CHUNKS_PER_CALL + 50)]
    svc = _service(vector_store=FakeVectorStore(rows=rows))
    out = await svc.get_file_chunks(partition="a", file_id="f1", allowed_partitions=["a"], offset=0, limit=-1)
    assert len(out["chunks"]) == _MAX_CHUNKS_PER_CALL
    assert out["has_more"] is True
