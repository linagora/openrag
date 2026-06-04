"""Tests for the FRAMES benchmark eval pipeline (setup_frames + eval_frames).

Run the fast (mocked / pure) tests:
    uv run pytest tests/load/automatic-evaluation-pipeline/test_frames.py

The live contract test (marked `integration`) hits a running OpenRAG and is
skipped unless OPENRAG_SMOKE_URL + OPENRAG_SMOKE_TOKEN are set, e.g.:
    OPENRAG_SMOKE_URL=http://localhost:8163 OPENRAG_SMOKE_TOKEN=or-test \
        uv run pytest tests/load/automatic-evaluation-pipeline/test_frames.py -m integration
"""

import asyncio
import json
import os

import eval_frames as ev
import httpx
import pytest
import respx
import setup_frames as su

# ─── Pure helpers: Wikipedia parsing ─────────────────────────────────────────


class TestExtractTitle:
    @pytest.mark.parametrize("module", [ev, su])
    def test_basic(self, module):
        assert module.extract_title_from_url("https://en.wikipedia.org/wiki/Alan_Turing") == "Alan Turing"

    @pytest.mark.parametrize("module", [ev, su])
    def test_url_encoded(self, module):
        assert module.extract_title_from_url("https://en.wikipedia.org/wiki/Cura%C3%A7ao") == "Curaçao"

    @pytest.mark.parametrize("module", [ev, su])
    def test_non_wikipedia_returns_none(self, module):
        assert module.extract_title_from_url("https://example.com/wiki/Foo") is None

    @pytest.mark.parametrize("module", [ev, su])
    def test_no_wiki_path(self, module):
        assert module.extract_title_from_url("https://en.wikipedia.org/") is None


class TestParseWikiLinks:
    def test_none(self):
        assert ev.parse_wiki_links({"wiki_links": None}) == []
        assert ev.parse_wiki_links({}) == []

    def test_python_list_literal(self):
        row = {"wiki_links": "['https://en.wikipedia.org/wiki/A', 'https://en.wikipedia.org/wiki/B']"}
        assert ev.parse_wiki_links(row) == [
            "https://en.wikipedia.org/wiki/A",
            "https://en.wikipedia.org/wiki/B",
        ]

    def test_actual_list(self):
        row = {"wiki_links": ["https://en.wikipedia.org/wiki/A", "https://fr.wikipedia.org/wiki/B"]}
        assert ev.parse_wiki_links(row) == [
            "https://en.wikipedia.org/wiki/A",
            "https://fr.wikipedia.org/wiki/B",
        ]

    def test_filters_non_wikipedia(self):
        row = {"wiki_links": ["https://example.com/x", "https://en.wikipedia.org/wiki/A"]}
        assert ev.parse_wiki_links(row) == ["https://en.wikipedia.org/wiki/A"]

    def test_dedup(self):
        row = {"wiki_links": ["https://en.wikipedia.org/wiki/A", "https://en.wikipedia.org/wiki/A"]}
        assert ev.parse_wiki_links(row) == ["https://en.wikipedia.org/wiki/A"]

    def test_two_urls_glued_in_one_string(self):
        glued = "https://en.wikipedia.org/wiki/A https://en.wikipedia.org/wiki/B"
        row = {"wiki_links": [glued]}
        assert ev.parse_wiki_links(row) == [
            "https://en.wikipedia.org/wiki/A",
            "https://en.wikipedia.org/wiki/B",
        ]

    def test_comma_separated_fallback(self):
        # Not a list literal / JSON -> comma split path.
        row = {"wiki_links": "https://en.wikipedia.org/wiki/A, https://en.wikipedia.org/wiki/B"}
        assert ev.parse_wiki_links(row) == [
            "https://en.wikipedia.org/wiki/A",
            "https://en.wikipedia.org/wiki/B",
        ]


class TestExtractAllTitles:
    def test_dedup_sorted(self):
        ds = [
            {"wiki_links": ["https://en.wikipedia.org/wiki/Banana", "https://en.wikipedia.org/wiki/Apple"]},
            {"wiki_links": ["https://en.wikipedia.org/wiki/Apple"]},
            {"wiki_links": None},
        ]
        assert su.extract_all_titles(ds) == ["Apple", "Banana"]

    def test_glued_urls_are_split(self):
        # Two URLs in one string must both be downloaded, matching eval's
        # parse_wiki_links so oracle/gold files line up.
        ds = [{"wiki_links": ["https://en.wikipedia.org/wiki/A https://en.wikipedia.org/wiki/B"]}]
        assert su.extract_all_titles(ds) == ["A", "B"]


class TestParsersAgree:
    """setup_frames and eval_frames must resolve the same article set."""

    @pytest.mark.parametrize("wiki_links", [
        None,
        ["https://en.wikipedia.org/wiki/Alan_Turing"],
        "['https://en.wikipedia.org/wiki/A', 'https://en.wikipedia.org/wiki/B']",
        ["https://en.wikipedia.org/wiki/A https://en.wikipedia.org/wiki/B"],
        ["https://example.com/x", "https://en.wikipedia.org/wiki/A"],
        "https://en.wikipedia.org/wiki/A, https://en.wikipedia.org/wiki/B",
    ])
    def test_parse_wiki_links_identical(self, wiki_links):
        row = {"wiki_links": wiki_links}
        assert su.parse_wiki_links(row) == ev.parse_wiki_links(row)


# ─── Pure helpers: filenames ─────────────────────────────────────────────────


class TestSafeFilename:
    @pytest.mark.parametrize("module", [ev, su])
    def test_spaces_to_underscore(self, module):
        fn = module._safe_filename if module is ev else module.safe_filename
        assert fn("Alan Turing") == "Alan_Turing"

    @pytest.mark.parametrize("module", [ev, su])
    def test_strips_unsafe(self, module):
        fn = module._safe_filename if module is ev else module.safe_filename
        assert fn("AC/DC: Back?") == "ACDC_Back"

    def test_empty_title_fallback_is_stable_and_cross_module(self):
        # The empty-title fallback must be stable across processes and identical
        # in both scripts so oracle / gold-file lookups line up.
        weird = "©®™"
        a = ev._safe_filename(weird)
        b = su.safe_filename(weird)
        assert a == b
        assert a.startswith("article_")
        assert a == ev._safe_filename(weird)  # deterministic


class TestWikiSlug:
    def test_spaces_to_underscore(self):
        assert su.title_to_wiki_slug("Alan Turing") == "Alan_Turing"

    def test_preserves_safe_chars(self):
        assert su.title_to_wiki_slug("C++") == "C++"


# ─── Pure helpers: Retry-After parsing ───────────────────────────────────────


def _resp_with_retry_after(value):
    headers = {"retry-after": value} if value is not None else {}
    return httpx.Response(429, headers=headers)


class TestRetryAfter:
    def test_numeric(self):
        assert su._retry_after_seconds(_resp_with_retry_after("7"), 0) == 7.0

    def test_http_date_in_past_clamps_to_zero(self):
        assert su._retry_after_seconds(
            _resp_with_retry_after("Wed, 21 Oct 2015 07:28:00 GMT"), 0
        ) == 0.0

    def test_unparseable_falls_back_to_backoff(self):
        # attempt=2 -> min(2**2+1, 60) == 5
        assert su._retry_after_seconds(_resp_with_retry_after("soon"), 2) == 5.0

    def test_absent_falls_back_to_backoff(self):
        # attempt=1 -> min(2**1+1, 60) == 3
        assert su._retry_after_seconds(_resp_with_retry_after(None), 1) == 3.0


# ─── Pure helpers: scoring / context ─────────────────────────────────────────


class TestExactMatch:
    def test_normalization(self):
        assert ev._normalize_exact_match_text("  Hello, World!  ") == "hello world"

    def test_match_ignores_case_and_punctuation(self):
        assert ev._compute_exact_match("Paris.", ["paris"]) is True

    def test_no_match(self):
        assert ev._compute_exact_match("London", ["Paris"]) is False

    def test_empty_generated_is_false(self):
        assert ev._compute_exact_match("", ["anything"]) is False

    def test_annotate_uses_expected_list(self):
        results = [{"generated_answer": "42", "expected_exact_match_answers": ["forty two", "42"]}]
        ev.annotate_exact_match(results)
        assert results[0]["exact_match"] is True


class TestOracleContext:
    def test_assembles_matching_articles(self):
        row = {"wiki_links": ["https://en.wikipedia.org/wiki/A", "https://en.wikipedia.org/wiki/B"]}
        articles = {"A": "alpha", "B": "bravo"}
        ctx = ev.build_oracle_context(row, articles, max_chars=0)
        assert "=== A ===" in ctx and "alpha" in ctx
        assert "=== B ===" in ctx and "bravo" in ctx

    def test_empty_when_no_match(self):
        row = {"wiki_links": ["https://en.wikipedia.org/wiki/Z"]}
        assert ev.build_oracle_context(row, {"A": "alpha"}, max_chars=0) == ""

    def test_truncates_per_article_under_budget(self):
        row = {"wiki_links": ["https://en.wikipedia.org/wiki/A", "https://en.wikipedia.org/wiki/B"]}
        articles = {"A": "x" * 5000, "B": "y" * 5000}
        ctx = ev.build_oracle_context(row, articles, max_chars=2000)
        assert len(ctx) <= 2000 + 100  # headers overhead slack


class TestStripHtml:
    def test_removes_script_style_comments(self):
        html = "<html><script>evil()</script><style>x{}</style><!-- c --><p>Keep</p></html>"
        out = ev._strip_html_for_oracle(html)
        assert "evil" not in out and "x{}" not in out and "c " not in out
        assert "Keep" in out


class TestBuildResult:
    def test_falls_back_to_answer_for_expected(self):
        row = {"Prompt": "Q?", "Answer": "A"}
        r = ev._build_result(row, "gen", "src", "rag")
        assert r["expected_exact_match_answers"] == ["A"]
        assert r["question"] == "Q?" and r["gold_answer"] == "A"
        assert r["mode"] == "rag" and r["generated_answer"] == "gen"

    def test_none_answer_becomes_empty_string(self):
        row = {"Prompt": "Q?", "Answer": "A"}
        assert ev._build_result(row, None, "", "rag")["generated_answer"] == ""

    def test_extra_kwargs_merged(self):
        row = {"Prompt": "Q?", "Answer": "A"}
        r = ev._build_result(row, "g", "", "gold_workspaces", workspace_id="ws-1", question_index=3)
        assert r["workspace_id"] == "ws-1" and r["question_index"] == 3


class TestGoldHelpers:
    def test_gold_name_zero_padded(self):
        assert ev._build_gold_name("FRAMES-goldws", 7) == "FRAMES-goldws-q0007"

    def test_gold_file_ids(self):
        row = {"wiki_links": ["https://en.wikipedia.org/wiki/Alan_Turing"]}
        assert ev._get_gold_file_ids(row) == ["Alan_Turing"]


# ─── Mocked HTTP: retry + health + partitions ────────────────────────────────


@pytest.fixture
def no_sleep(monkeypatch):
    async def _instant(*_a, **_k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _instant)


@pytest.mark.asyncio
@respx.mock
async def test_http_with_retry_retries_5xx_then_succeeds(no_sleep):
    route = respx.get("http://t/x").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"ok": True})]
    )
    async with httpx.AsyncClient() as client:
        resp = await ev._http_with_retry(client, "GET", "http://t/x")
    assert resp.json() == {"ok": True}
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_http_with_retry_raises_on_4xx_immediately(no_sleep):
    route = respx.get("http://t/x").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await ev._http_with_retry(client, "GET", "http://t/x")
    assert route.call_count == 1  # no retry on client error


@pytest.mark.asyncio
@respx.mock
async def test_http_with_retry_retries_network_error(no_sleep):
    route = respx.get("http://t/x").mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={})]
    )
    async with httpx.AsyncClient() as client:
        resp = await ev._http_with_retry(client, "GET", "http://t/x")
    assert resp.status_code == 200
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_check_health(monkeypatch):
    respx.get(f"{su.OPENRAG_BASE_URL}/health_check").mock(return_value=httpx.Response(200, text="ok"))
    async with httpx.AsyncClient() as client:
        assert await su.check_health(client) is True
    respx.get(f"{su.OPENRAG_BASE_URL}/health_check").mock(side_effect=httpx.ConnectError("down"))
    async with httpx.AsyncClient() as client:
        assert await su.check_health(client) is False


@pytest.mark.asyncio
@respx.mock
async def test_create_partition_201_409_and_error():
    base = su.OPENRAG_BASE_URL
    respx.post(f"{base}/partition/P").mock(return_value=httpx.Response(201))
    async with httpx.AsyncClient() as client:
        await su.create_partition(client, "P")  # no raise

    respx.post(f"{base}/partition/P").mock(return_value=httpx.Response(409))
    async with httpx.AsyncClient() as client:
        await su.create_partition(client, "P")  # no raise

    respx.post(f"{base}/partition/P").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await su.create_partition(client, "P")


@pytest.mark.asyncio
@respx.mock
async def test_get_available_partitions_filters_all():
    respx.get(f"{ev.OPENRAG_BASE_URL}/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [
            {"id": "openrag-FRAMES"},
            {"id": "openrag-all"},
            {"id": "openrag-Other"},
            {"id": "not-openrag"},
        ]})
    )
    async with httpx.AsyncClient() as client:
        parts = await ev.get_available_openrag_partitions(client)
    assert parts == ["FRAMES", "Other"]


@pytest.mark.asyncio
@respx.mock
async def test_get_available_workspaces():
    respx.get(f"{ev.OPENRAG_BASE_URL}/partition/P/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [
            {"workspace_id": "w1"}, {"workspace_id": "w2"}, {"no_id": True},
        ]})
    )
    async with httpx.AsyncClient() as client:
        assert await ev.get_available_workspaces(client, "P") == ["w1", "w2"]


# ─── Mocked HTTP: chat + sources ─────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_query_openrag_answer_with_sources():
    base = ev.OPENRAG_BASE_URL
    respx.post(f"{base}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": "The answer is 42."}}],
            "extra": json.dumps({"sources": [{"chunk_url": f"{base}/extract/abc"}]}),
        })
    )
    respx.get(f"{base}/extract/abc").mock(
        return_value=httpx.Response(200, json={"page_content": "chunk text"})
    )
    sem = asyncio.Semaphore(1)
    answer, sources = await ev.query_openrag_answer("Q?", "FRAMES", sem)
    assert answer == "The answer is 42."
    assert sources == "chunk text"


@pytest.mark.asyncio
@respx.mock
async def test_query_openrag_answer_sets_workspace_metadata():
    base = ev.OPENRAG_BASE_URL
    captured = {}

    def _capture(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "extra": ""})

    respx.post(f"{base}/v1/chat/completions").mock(side_effect=_capture)
    sem = asyncio.Semaphore(1)
    answer, _ = await ev.query_openrag_answer("Q?", "FRAMES", sem, workspace="ws-9")
    assert answer == "ok"
    assert captured["metadata"] == {"workspace": "ws-9"}
    assert captured["model"] == "openrag-FRAMES"


@pytest.mark.asyncio
@respx.mock
async def test_query_openrag_answer_returns_none_on_error(no_sleep):
    respx.post(f"{ev.OPENRAG_BASE_URL}/v1/chat/completions").mock(return_value=httpx.Response(500))
    sem = asyncio.Semaphore(1)
    answer, sources = await ev.query_openrag_answer("Q?", "FRAMES", sem)
    assert answer is None and sources == ""


# ─── Mocked HTTP: workspaces ─────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_ensure_workspace_states():
    base = ev.OPENRAG_BASE_URL
    respx.post(f"{base}/partition/P/workspaces").mock(return_value=httpx.Response(201))
    async with httpx.AsyncClient() as client:
        assert await ev.ensure_workspace(client, "P", "w") is True
    respx.post(f"{base}/partition/P/workspaces").mock(return_value=httpx.Response(409))
    async with httpx.AsyncClient() as client:
        assert await ev.ensure_workspace(client, "P", "w") is False


@pytest.mark.asyncio
@respx.mock
async def test_add_files_to_workspace_tolerates_rejection():
    # A rejected attach must not raise, so one bad file can't abort the run.
    base = ev.OPENRAG_BASE_URL
    respx.post(f"{base}/partition/P/workspaces/w/files").mock(
        return_value=httpx.Response(400, text="file not found")
    )
    async with httpx.AsyncClient() as client:
        await ev.add_files_to_workspace(client, "P", "w", ["missing"])  # no raise


@pytest.mark.asyncio
@respx.mock
async def test_add_files_to_workspace_success():
    base = ev.OPENRAG_BASE_URL
    route = respx.post(f"{base}/partition/P/workspaces/w/files").mock(return_value=httpx.Response(200))
    async with httpx.AsyncClient() as client:
        await ev.add_files_to_workspace(client, "P", "w", ["a", "b"])
    assert json.loads(route.calls[0].request.content) == {"file_ids": ["a", "b"]}


# ─── Mocked HTTP: upload_and_track ───────────────────────────────────────────


@pytest.fixture
def tmp_file(tmp_path):
    p = tmp_path / "Article.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    return p


@pytest.mark.asyncio
@respx.mock
async def test_upload_and_track_completes(no_sleep, tmp_file, monkeypatch):
    monkeypatch.setattr(su, "POLL_INTERVAL", 0)
    base = su.OPENRAG_BASE_URL
    respx.post(f"{base}/indexer/partition/P/file/Article").mock(
        return_value=httpx.Response(201, json={"task_status_url": "/indexer/task/t1"})
    )
    respx.get(f"{base}/indexer/task/t1").mock(side_effect=[
        httpx.Response(200, json={"task_state": "CHUNKING"}),
        httpx.Response(200, json={"task_state": "COMPLETED"}),
    ])
    async with httpx.AsyncClient() as client:
        res = await su.upload_and_track(client, "P", "Article", tmp_file, "application/pdf", asyncio.Semaphore(1))
    assert res == {"file_id": "Article", "status": "COMPLETED"}


@pytest.mark.asyncio
@respx.mock
async def test_upload_and_track_409_skips(tmp_file):
    base = su.OPENRAG_BASE_URL
    respx.post(f"{base}/indexer/partition/P/file/Article").mock(return_value=httpx.Response(409))
    async with httpx.AsyncClient() as client:
        res = await su.upload_and_track(client, "P", "Article", tmp_file, "application/pdf", asyncio.Semaphore(1))
    assert res["status"] == "skipped"


@pytest.mark.asyncio
@respx.mock
async def test_upload_and_track_cancelled_is_terminal(no_sleep, tmp_file, monkeypatch):
    # CANCELLED is terminal: it must end the poll loop.
    monkeypatch.setattr(su, "POLL_INTERVAL", 0)
    base = su.OPENRAG_BASE_URL
    respx.post(f"{base}/indexer/partition/P/file/Article").mock(
        return_value=httpx.Response(201, json={"task_status_url": "/indexer/task/t1"})
    )
    respx.get(f"{base}/indexer/task/t1").mock(return_value=httpx.Response(200, json={"task_state": "CANCELLED"}))
    async with httpx.AsyncClient() as client:
        res = await su.upload_and_track(client, "P", "Article", tmp_file, "application/pdf", asyncio.Semaphore(1))
    assert res == {"file_id": "Article", "status": "CANCELLED"}


@pytest.mark.asyncio
@respx.mock
async def test_upload_and_track_poll_timeout(no_sleep, tmp_file, monkeypatch):
    # A never-terminal task must time out rather than poll forever.
    monkeypatch.setattr(su, "POLL_INTERVAL", 0)
    monkeypatch.setattr(su, "MAX_POLL_SECONDS", 0)
    base = su.OPENRAG_BASE_URL
    respx.post(f"{base}/indexer/partition/P/file/Article").mock(
        return_value=httpx.Response(201, json={"task_status_url": "/indexer/task/t1"})
    )
    respx.get(f"{base}/indexer/task/t1").mock(return_value=httpx.Response(200, json={"task_state": "CHUNKING"}))
    async with httpx.AsyncClient() as client:
        res = await su.upload_and_track(client, "P", "Article", tmp_file, "application/pdf", asyncio.Semaphore(1))
    assert res["status"] == "ERROR" and "timed out" in res["error"]


@pytest.mark.asyncio
@respx.mock
async def test_upload_and_track_missing_task_url(tmp_file):
    # A 201 without task_status_url must return an error, not crash.
    base = su.OPENRAG_BASE_URL
    respx.post(f"{base}/indexer/partition/P/file/Article").mock(return_value=httpx.Response(201, json={}))
    async with httpx.AsyncClient() as client:
        res = await su.upload_and_track(client, "P", "Article", tmp_file, "application/pdf", asyncio.Semaphore(1))
    assert res["status"] == "ERROR" and "task_status_url" in res["error"]


# ─── Env validation ──────────────────────────────────────────────────────────


def test_require_llm_config_raises_when_missing(monkeypatch):
    for name in ("MODEL", "BASE_URL", "API_KEY", "JUDGE_MODEL", "JUDGE_BASE_URL", "JUDGE_API_KEY"):
        monkeypatch.setattr(ev, name, None)
    with pytest.raises(SystemExit) as exc:
        ev._require_llm_config()
    assert "MODEL" in str(exc.value)


def test_require_llm_config_passes_when_present(monkeypatch):
    for name in ("MODEL", "BASE_URL", "API_KEY", "JUDGE_MODEL", "JUDGE_BASE_URL", "JUDGE_API_KEY"):
        monkeypatch.setattr(ev, name, "set")
    ev._require_llm_config()  # no raise


# ─── Live contract smoke test (read-only, opt-in) ────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_openrag_contract():
    url = os.environ.get("OPENRAG_SMOKE_URL")
    token = os.environ.get("OPENRAG_SMOKE_TOKEN")
    if not url or not token:
        pytest.skip("set OPENRAG_SMOKE_URL + OPENRAG_SMOKE_TOKEN to run the live contract test")

    async with httpx.AsyncClient(timeout=30, base_url=url) as client:
        health = await client.get("/health_check")
        assert health.status_code == 200

        models = await client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
        assert models.status_code == 200
        ids = [m["id"] for m in models.json().get("data", [])]
        assert any(i.startswith("openrag-") for i in ids), ids
