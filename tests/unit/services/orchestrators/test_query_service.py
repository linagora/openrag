"""Unit tests for :class:`QueryService` (Phase 8C.2).

The Ray-backed LLM semaphore and the model-file-backed language detector
are monkeypatched (both are infra concerns exercised in integration, not
here). Retrieval is faked; the real ``format_context`` /
``stream_with_source_filtering`` helpers run against real ``Chunk`` →
``Document`` conversions.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import services.orchestrators.query_service as qs
from core.config import load_config
from core.models.chunk import Chunk
from services.orchestrators.query_service import QueryService

# Real prompt-template config (dir + key->filename mapping) so QueryService
# can load its system / contextualizer / spoken-style templates from disk.
_PROMPT_CFG = load_config()


@pytest.fixture(autouse=True)
def _patch_infra(monkeypatch):
    @asynccontextmanager
    async def _noop_sem():
        yield

    monkeypatch.setattr(qs, "get_llm_semaphore", _noop_sem)
    monkeypatch.setattr(qs, "detect_language", lambda _t: "en")


class FakeLLM:
    def __init__(self, *, chat_responses=None, gen_text="answer", stream_lines=None):
        self._chat_responses = list(chat_responses or [])
        self._gen_text = gen_text
        self._stream_lines = stream_lines or ['data: {"choices":[{"delta":{"content":"hi"}}]}\n\n', "data: [DONE]\n\n"]
        self.chat_calls: list = []

    async def chat(self, messages, **kwargs):
        self.chat_calls.append((messages, kwargs))
        if self._chat_responses:
            content = self._chat_responses.pop(0)
        else:
            content = "final answer"
        return {"choices": [{"message": {"content": content}}]}

    async def generate(self, prompt, **kwargs):
        return {"choices": [{"text": self._gen_text}]}

    async def stream_chat(self, messages, **kwargs):
        for line in self._stream_lines:
            yield line


class FakeRetrieval:
    def __init__(self, chunks=None):
        self._chunks = chunks if chunks is not None else [Chunk(id="c1", text="ctx", metadata={"_id": "c1"})]

    async def retrieve_multi(self, **kwargs):
        return list(self._chunks)

    async def retrieve_per_query(self, *, queries, **kwargs):
        return [list(self._chunks) for _ in queries]

    @staticmethod
    def fuse(doc_lists, top_k=None):
        return doc_lists[0] if doc_lists else []


class FakeWeb:
    max_tokens = 2000

    def __init__(self, results=None):
        self._results = results or []

    async def search(self, query):
        return list(self._results)


class FakeWorkspace:
    async def get_workspace(self, wid):
        return None


def _config(mode="SimpleRag"):
    return SimpleNamespace(
        rag=SimpleNamespace(mode=mode, chat_history_depth=4, max_contextualized_query_len=512),
        reranker=SimpleNamespace(top_k=5),
        chunker=SimpleNamespace(chunk_size=512),
        map_reduce=SimpleNamespace(initial_batch_size=2, expansion_batch_size=2, max_total_documents=4),
        paths=_PROMPT_CFG.paths,
        prompts=_PROMPT_CFG.prompts,
    )


def _svc(*, llm=None, retrieval=None, web=None, mode="SimpleRag") -> QueryService:
    return QueryService(
        retrieval_service=retrieval or FakeRetrieval(),
        llm=llm or FakeLLM(),
        config=_config(mode),
        web_search_service=web or FakeWeb(),
        workspace_service=FakeWorkspace(),
    )


# --------------------------------------------------------------------------- #
# generate_query
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_generate_query_simplerag_uses_last_message():
    sq = await _svc(mode="SimpleRag").generate_query([{"role": "user", "content": "what is X?"}])
    assert [q.query for q in sq.query_list] == ["what is X?"]


@pytest.mark.asyncio
async def test_generate_query_chatbotrag_parses_json():
    payload = json.dumps({"query_list": [{"query": "rewritten", "temporal_filters": None}]})
    svc = _svc(llm=FakeLLM(chat_responses=[payload]), mode="ChatBotRag")
    sq = await svc.generate_query([{"role": "user", "content": "hi"}])
    assert sq.query_list[0].query == "rewritten"


@pytest.mark.asyncio
async def test_generate_query_chatbotrag_falls_back_on_garbage():
    svc = _svc(llm=FakeLLM(chat_responses=["not json", "still not json"]), mode="ChatBotRag")
    sq = await svc.generate_query([{"role": "user", "content": "raw question"}])
    assert sq.query_list[0].query == "raw question"  # fallback to raw user query


# --------------------------------------------------------------------------- #
# chat / complete
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_chat_direct_mode_skips_retrieval():
    retrieval = FakeRetrieval()
    called = {"n": 0}

    async def _spy(**kwargs):
        called["n"] += 1
        return []

    retrieval.retrieve_multi = _spy
    svc = _svc(retrieval=retrieval, llm=FakeLLM(chat_responses=["hello [Sources: none]"]))
    out = await svc.chat(
        partitions=None,
        payload={"messages": [{"role": "user", "content": "hi"}], "metadata": {}},
        prepare_sources=lambda d, w: [{"source_type": "document"}],
        model_name="m1",
    )
    assert called["n"] == 0  # no retrieval in direct mode
    assert out["model"] == "m1"
    assert out["choices"][0]["message"]["content"] == "hello"  # sources tag stripped
    assert json.loads(out["extra"])["sources"] == []  # [Sources: none] → no sources


@pytest.mark.asyncio
async def test_chat_with_partition_retrieves_and_filters_sources():
    svc = _svc(llm=FakeLLM(chat_responses=["answer [Sources: 1]"]))
    sources = [{"source_type": "document", "n": 1}, {"source_type": "document", "n": 2}]
    out = await svc.chat(
        partitions=["p"],
        payload={"messages": [{"role": "user", "content": "q"}], "metadata": {}},
        prepare_sources=lambda d, w: sources,
        model_name="m",
    )
    filtered = json.loads(out["extra"])["sources"]
    assert filtered == [{"source_type": "document", "n": 1}]  # only cited source 1


@pytest.mark.asyncio
async def test_complete_strips_and_filters():
    svc = _svc(llm=FakeLLM(gen_text="text body [Sources: none]"))
    out = await svc.complete(
        partitions=None,
        payload={"prompt": "do x"},
        prepare_sources=lambda d, w: [{"x": 1}],
    )
    assert out["choices"][0]["text"] == "text body"
    assert json.loads(out["extra"])["sources"] == []


@pytest.mark.asyncio
async def test_chat_stream_yields_sse_and_done():
    svc = _svc(llm=FakeLLM())
    lines = []
    async for line in svc.chat_stream(
        partitions=None,
        payload={"messages": [{"role": "user", "content": "hi"}], "metadata": {}},
        prepare_sources=lambda d, w: [],
        model_name="m",
    ):
        lines.append(line)
    assert any("[DONE]" in ln for ln in lines)


# --------------------------------------------------------------------------- #
# map-reduce
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_map_reduce_keeps_relevant_drops_irrelevant():
    rel = json.dumps({"relevancy": True, "summary": "kept"})
    irr = json.dumps({"relevancy": False, "summary": ""})
    svc = _svc(llm=FakeLLM(chat_responses=[rel, irr]))
    docs = [
        Chunk(id="a", text="A", metadata={"_id": "a"}).to_langchain(),
        Chunk(id="b", text="B", metadata={"_id": "b"}).to_langchain(),
    ]
    out = await svc._map_reduce("q", docs)
    assert len(out) == 1
    assert out[0].page_content == "kept"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def test_json_slice_extracts_object():
    assert qs._json_slice('noise {"a": 1} trailing') == '{"a": 1}'


def test_dedupe_web_preserves_first_seen():
    a = SimpleNamespace(url="u1")
    b = SimpleNamespace(url="u1")
    c = SimpleNamespace(url="u2")
    assert qs._dedupe_web([[a, b], [c]]) == [a, c]


def test_sampling_strips_transport_keys():
    out = qs._sampling({"messages": [], "stream": True, "model": "m", "temperature": 0.5})
    assert out == {"temperature": 0.5}


# --------------------------------------------------------------------------- #
# _sanitize_messages
# --------------------------------------------------------------------------- #


class _RecordingLogger:
    """Minimal logger stub that records error/warning calls for assertions."""

    def __init__(self):
        self.errors: list[dict] = []
        self.warnings: list[dict] = []

    def error(self, msg: str, **kwargs) -> None:
        self.errors.append({"msg": msg, **kwargs})

    def warning(self, msg: str, **kwargs) -> None:
        self.warnings.append({"msg": msg, **kwargs})

    def info(self, *a, **kw) -> None:
        pass

    def debug(self, *a, **kw) -> None:
        pass


@pytest.fixture()
def rec_logger(monkeypatch):
    rec = _RecordingLogger()
    monkeypatch.setattr(qs, "logger", rec)
    return rec


def test_sanitize_valid_alternating_unchanged(rec_logger):
    """Valid alternating history → identical output, no logs."""
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "again"},
    ]
    out = QueryService._sanitize_messages(msgs)
    assert out == msgs
    assert rec_logger.errors == []


def test_sanitize_empty_content_replaced_with_placeholder(rec_logger):
    """Empty assistant content → replaced with NO_CONTENT, alternation preserved."""
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "second"},
    ]
    out = QueryService._sanitize_messages(msgs)
    assert len(out) == 3
    assert out[1]["role"] == "assistant"
    assert out[1]["content"] == "NO_CONTENT"
    assert len(rec_logger.errors) == 1
    assert rec_logger.errors[0]["patched_count"] == 1


def test_sanitize_whitespace_only_replaced(rec_logger):
    """Whitespace-only assistant content → replaced with NO_CONTENT."""
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "   "},
    ]
    out = QueryService._sanitize_messages(msgs)
    assert len(out) == 2
    assert out[1]["content"] == "NO_CONTENT"
    assert rec_logger.errors[0]["patched_count"] == 1


def test_sanitize_no_content_key_replaced(rec_logger):
    """Assistant with no 'content' key → replaced with NO_CONTENT, other keys kept."""
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "name": "bot"},
    ]
    out = QueryService._sanitize_messages(msgs)
    assert len(out) == 2
    assert out[1]["content"] == "NO_CONTENT"
    assert out[1]["name"] == "bot"
    assert rec_logger.errors[0]["patched_count"] == 1


def test_sanitize_empty_user_not_touched(rec_logger):
    """User message with empty content → kept as-is (only assistant is patched)."""
    msgs = [{"role": "user", "content": ""}]
    out = QueryService._sanitize_messages(msgs)
    assert out == msgs
    assert rec_logger.errors == []


def test_sanitize_multiple_empty_assistants_all_patched(rec_logger):
    """Multiple empty assistants → all replaced, patched_count accurate, alternation intact."""
    msgs = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "u3"},
    ]
    out = QueryService._sanitize_messages(msgs)
    assert len(out) == 5
    assert out[1]["content"] == "NO_CONTENT"
    assert out[3]["content"] == "NO_CONTENT"
    assert rec_logger.errors[0]["patched_count"] == 2


def test_sanitize_multimodal_content_untouched(rec_logger):
    """Non-string content (multimodal list) → never replaced."""
    msgs = [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://x"}}]},
        {"role": "assistant", "content": [{"type": "text", "text": "desc"}]},
    ]
    out = QueryService._sanitize_messages(msgs)
    assert out == msgs
    assert rec_logger.errors == []


def test_sanitize_empty_list(rec_logger):
    """Empty input → empty output, no logs."""
    out = QueryService._sanitize_messages([])
    assert out == []
    assert rec_logger.errors == []


def test_sanitize_system_message_preserved(rec_logger):
    """System message in head → preserved untouched."""
    msgs = [
        {"role": "system", "content": "You are an assistant."},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    out = QueryService._sanitize_messages(msgs)
    assert out == msgs
    assert rec_logger.errors == []
