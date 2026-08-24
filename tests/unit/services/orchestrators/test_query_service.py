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
from core.models.workspace import WorkspaceScope
from core.utils.exceptions import WorkspaceNotFoundError
from services.orchestrators.query_service import QueryService

# Real prompt-template config (dir + key->filename mapping) so QueryService
# can load its system / contextualizer / spoken-style templates from disk.
_PROMPT_CFG = load_config()


class _EmptyPromptRepo:
    """No DB rows → PromptService.resolve_prompt falls back to the disk seed,
    preserving the pre-DB behaviour these tests assert."""

    async def get_by_name(self, prompt_type, name):
        return None

    async def get_default(self, prompt_type):
        return None


def _disk_prompt_service():
    from services.orchestrators.prompt_service import PromptService

    return PromptService(prompt_repo=_EmptyPromptRepo(), config=_PROMPT_CFG)


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
        self.generate_calls: list = []

    async def chat(self, messages, **kwargs):
        self.chat_calls.append((messages, kwargs))
        if self._chat_responses:
            content = self._chat_responses.pop(0)
        else:
            content = "final answer"
        return {"choices": [{"message": {"content": content}}]}

    async def generate(self, prompt, **kwargs):
        self.generate_calls.append((prompt, kwargs))
        return {"choices": [{"text": self._gen_text}]}

    async def stream_chat(self, messages, **kwargs):
        for line in self._stream_lines:
            yield line


class FakeRetrieval:
    def __init__(self, chunks=None):
        self._chunks = chunks if chunks is not None else [Chunk(id="c1", text="ctx", metadata={"_id": "c1"})]
        self.retrieve_multi_calls: list[dict] = []
        self.retrieve_per_query_calls: list[dict] = []

    async def retrieve_multi(self, **kwargs):
        self.retrieve_multi_calls.append(kwargs)
        return list(self._chunks)

    async def retrieve_per_query(self, *, queries, **kwargs):
        self.retrieve_per_query_calls.append({"queries": queries, **kwargs})
        return [list(self._chunks) for _ in queries]

    @staticmethod
    def fuse(doc_lists, top_k=None):
        return doc_lists[0] if doc_lists else []


class FakeWeb:
    max_tokens = 2000

    def __init__(self, results=None):
        self._results = results or []
        self.calls: list[str] = []

    async def search(self, query):
        self.calls.append(query)
        return list(self._results)


class FakeWorkspace:
    def __init__(self, scope=None):
        self._scope = scope

    async def get_workspace(self, wid):
        return None

    async def resolve_scope(self, workspace_id, allowed_partitions):
        return self._scope


def _config(mode="SimpleRag"):
    return SimpleNamespace(
        rag=SimpleNamespace(mode=mode, chat_history_depth=4, max_contextualized_query_len=512),
        reranker=SimpleNamespace(top_k=5),
        chunker=SimpleNamespace(chunk_size=512),
        map_reduce=SimpleNamespace(initial_batch_size=2, expansion_batch_size=2, max_total_documents=4),
        paths=_PROMPT_CFG.paths,
        prompts=_PROMPT_CFG.prompts,
        partitions={},
        models=SimpleNamespace(llm={}),
    )


def _svc(*, llm=None, retrieval=None, web=None, mode="SimpleRag", llm_factory=None, workspace=None) -> QueryService:
    return QueryService(
        retrieval_service=retrieval or FakeRetrieval(),
        llm=llm or FakeLLM(),
        config=_config(mode),
        web_search_service=web or FakeWeb(),
        workspace_service=workspace or FakeWorkspace(),
        prompt_service=_disk_prompt_service(),
        llm_factory=llm_factory,
    )


# --------------------------------------------------------------------------- #
# chat history depth resolution (per-partition override of the global default)
# --------------------------------------------------------------------------- #


def test_resolve_chat_history_depth_uses_partition_value():
    svc = _svc()  # global default = 4
    svc._config.partitions = {"p": SimpleNamespace(chat_history_depth=10)}
    assert svc._resolve_chat_history_depth(["p"]) == 10


def test_resolve_chat_history_depth_zero_inherits_global_default():
    # 0 means "inherit" — never reaches the messages[-depth:] slice (where 0
    # would select the entire history).
    svc = _svc()
    svc._config.partitions = {"p": SimpleNamespace(chat_history_depth=0)}
    assert svc._resolve_chat_history_depth(["p"]) == 4


def test_resolve_chat_history_depth_none_and_unknown_use_default():
    svc = _svc()
    svc._config.partitions = {"p": SimpleNamespace(chat_history_depth=9)}
    assert svc._resolve_chat_history_depth(None) == 4
    assert svc._resolve_chat_history_depth(["missing"]) == 4


def test_resolve_chat_history_depth_all_sentinel_uses_default():
    # "all" (openrag-all) reaches this layer un-expanded and is not a real
    # partition name → cross-partition query uses the global default, never a
    # per-partition value.
    svc = _svc()  # global default = 4
    svc._config.partitions = {"a": SimpleNamespace(chat_history_depth=10)}
    assert svc._resolve_chat_history_depth(["all"]) == 4


def test_resolve_chat_history_depth_multi_partition_takes_max_explicit():
    svc = _svc()
    svc._config.partitions = {
        "a": SimpleNamespace(chat_history_depth=6),
        "b": SimpleNamespace(chat_history_depth=2),
        "c": SimpleNamespace(chat_history_depth=0),  # inherits → ignored
    }
    assert svc._resolve_chat_history_depth(["a", "b", "c"]) == 6


@pytest.mark.parametrize("global_depth", [0, -1])
def test_default_chat_history_depth_clamps_invalid_global_config(global_depth):
    """RAGConfig.chat_history_depth carries no lower bound. Left unclamped, a
    misconfigured global depth of 0 would make messages[-0:] keep the *entire*
    history for chats with no partition (or the "all" sentinel) — the opposite
    of what this depth is meant to limit."""
    config = _config()
    config.rag.chat_history_depth = global_depth
    svc = QueryService(
        retrieval_service=FakeRetrieval(),
        llm=FakeLLM(),
        config=config,
        web_search_service=FakeWeb(),
        workspace_service=FakeWorkspace(),
        prompt_service=_disk_prompt_service(),
    )
    assert svc._default_chat_history_depth == 4
    assert svc._resolve_chat_history_depth(None) == 4


# --------------------------------------------------------------------------- #
# chat LLM resolution (per-partition chat_llm model-endpoint preset)
# --------------------------------------------------------------------------- #


def _partition(chat_llm=None, chat_history_depth=0):
    return SimpleNamespace(chat_llm=chat_llm, chat_history_depth=chat_history_depth)


class RecordingFactory:
    def __init__(self, llms=None):
        self._llms = llms or {}
        self.calls: list[str] = []

    def __call__(self, name: str):
        self.calls.append(name)
        if name not in self._llms:
            raise KeyError(name)
        return self._llms[name]


def test_resolve_llm_uses_partition_preset():
    preset_llm = FakeLLM()
    factory = RecordingFactory({"mistral": preset_llm})
    svc = _svc(llm_factory=factory)
    svc._config.partitions = {"p": _partition(chat_llm="mistral")}
    assert svc._resolve_llm(["p"]) is preset_llm
    assert factory.calls == ["mistral"]


def test_resolve_llm_default_paths_use_the_catalog_default_endpoint():
    # No partition preset applies → resolve the catalog default endpoint
    # (is_default=True, exposed by the factory's "default" alias), NOT the
    # static env-built self._llm. Promoting a new default endpoint in the
    # catalog must take effect on the default chat path.
    static_llm, catalog_default = FakeLLM(), FakeLLM()
    factory = RecordingFactory({"default": catalog_default, "mistral": FakeLLM()})
    svc = _svc(llm=static_llm, llm_factory=factory)
    svc._config.partitions = {"p": _partition(chat_llm=None), "q": _partition(chat_llm="mistral")}
    assert svc._resolve_llm(None) is catalog_default  # direct/web-only mode
    assert svc._resolve_llm(["all"]) is catalog_default  # cross-partition sentinel
    assert svc._resolve_llm(["p"]) is catalog_default  # partition without a preset
    assert svc._resolve_llm(["missing"]) is catalog_default  # unknown partition
    assert factory.calls == ["default", "default", "default", "default"]


def test_resolve_llm_falls_back_to_static_llm_without_factory_or_catalog_default():
    # Last-resort static self._llm: no factory wired (unit tests), or the
    # factory has no "default" alias yet (catalog not seeded).
    static_llm = FakeLLM()
    no_factory = _svc(llm=static_llm)
    no_factory._config.partitions = {"q": _partition(chat_llm="mistral")}
    assert no_factory._resolve_llm(["q"]) is static_llm
    assert no_factory._resolve_llm(None) is static_llm

    empty_catalog = RecordingFactory()  # raises KeyError for "default"
    svc = _svc(llm=static_llm, llm_factory=empty_catalog)
    svc._config.partitions = {"p": _partition(chat_llm=None)}
    assert svc._resolve_llm(["p"]) is static_llm
    assert empty_catalog.calls == ["default"]


def test_resolve_llm_multi_partition_uses_preset_only_when_unanimous():
    catalog_default, preset_llm = FakeLLM(), FakeLLM()
    factory = RecordingFactory({"default": catalog_default, "mistral": preset_llm})
    svc = _svc(llm=FakeLLM(), llm_factory=factory)
    svc._config.partitions = {
        "a": _partition(chat_llm="mistral"),
        "b": _partition(chat_llm="mistral"),
        "c": _partition(chat_llm=None),  # unset → doesn't veto
        "d": _partition(chat_llm="llama"),
    }
    assert svc._resolve_llm(["a", "b", "c"]) is preset_llm  # unanimous among setters
    assert svc._resolve_llm(["a", "d"]) is catalog_default  # conflicting presets → catalog default


def test_default_llm_name_recovers_the_is_default_endpoint_name():
    # load_all() stores the is_default row's config under both its own name
    # and the "default" alias (same object) — _default_llm_name recovers the
    # real name by identity, so the logs name the endpoint that answered.
    svc = _svc(llm_factory=RecordingFactory())
    toy_cfg = SimpleNamespace(endpoint="http://toy-llm")
    svc._config.models.llm = {"base-llm": SimpleNamespace(endpoint="http://base"), "toy-llm": toy_cfg}
    svc._config.models.llm["default"] = toy_cfg  # alias points at the same object
    assert svc._default_llm_name() == "toy-llm"


def test_default_llm_name_returns_default_when_alias_absent():
    svc = _svc(llm_factory=RecordingFactory())
    svc._config.models.llm = {"base-llm": SimpleNamespace(endpoint="http://base")}
    assert svc._default_llm_name() == "default"


def test_resolve_llm_unknown_preset_falls_through_to_catalog_default():
    # chat_llm is not validated on assignment (the endpoint may be deleted
    # afterwards) — an unknown name must not fail the request; it falls through
    # to the catalog default endpoint, not the static env llm.
    static_llm, catalog_default = FakeLLM(), FakeLLM()
    factory = RecordingFactory({"default": catalog_default})  # "deleted" raises KeyError
    svc = _svc(llm=static_llm, llm_factory=factory)
    svc._config.partitions = {"p": _partition(chat_llm="deleted")}
    assert svc._resolve_llm(["p"]) is catalog_default
    assert factory.calls == ["deleted", "default"]


@pytest.mark.asyncio
async def test_chat_answers_with_partition_chat_llm():
    default_llm = FakeLLM()
    preset_llm = FakeLLM(chat_responses=["preset answer [Sources: none]"])
    svc = _svc(llm=default_llm, llm_factory=RecordingFactory({"mistral": preset_llm}))
    svc._config.partitions = {"p": _partition(chat_llm="mistral")}
    out = await svc.chat(
        partitions=["p"],
        payload={"messages": [{"role": "user", "content": "q"}], "metadata": {}},
        prepare_sources=lambda d, w: [],
        model_name="openrag-p",
    )
    assert out["choices"][0]["message"]["content"] == "preset answer"
    assert len(preset_llm.chat_calls) == 1
    assert default_llm.chat_calls == []  # SimpleRag: no query-gen call either


@pytest.mark.asyncio
async def test_chat_query_generation_uses_partition_chat_llm():
    # ChatBotRag contextualizes the query with an LLM call — that call must
    # go through the partition preset too, not just the final answer.
    default_llm = FakeLLM()
    query_json = json.dumps({"query_list": [{"query": "rewritten", "temporal_filters": None}]})
    preset_llm = FakeLLM(chat_responses=[query_json, "preset answer [Sources: none]"])
    svc = _svc(mode="ChatBotRag", llm=default_llm, llm_factory=RecordingFactory({"mistral": preset_llm}))
    svc._config.partitions = {"p": _partition(chat_llm="mistral")}
    out = await svc.chat(
        partitions=["p"],
        payload={"messages": [{"role": "user", "content": "q"}], "metadata": {}},
        prepare_sources=lambda d, w: [],
        model_name="openrag-p",
    )
    assert out["choices"][0]["message"]["content"] == "preset answer"
    assert len(preset_llm.chat_calls) == 2  # query generation + answer
    assert default_llm.chat_calls == []


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
    assert sq.requires_retrieval is True


@pytest.mark.asyncio
async def test_generate_query_chatbotrag_can_skip_retrieval():
    payload = json.dumps({"requires_retrieval": False, "query_list": []})
    svc = _svc(llm=FakeLLM(chat_responses=[payload]), mode="ChatBotRag")
    sq = await svc.generate_query([{"role": "user", "content": "How can you help me?"}])
    assert sq.requires_retrieval is False
    assert sq.query_list == []


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
        prepare_sources=lambda d, w: [{"source_type": "document"}] if d or w else [],
        model_name="m1",
    )
    assert called["n"] == 0  # no retrieval in direct mode
    assert out["model"] == "m1"
    assert out["choices"][0]["message"]["content"] == "hello [Sources: none]"
    assert json.loads(out["extra"])["sources"] == []


@pytest.mark.asyncio
async def test_chat_direct_mode_preserves_literal_source_marker():
    answer = "The literal notation [Source 1] identifies the first source."
    svc = _svc(llm=FakeLLM(chat_responses=[answer]))

    out = await svc.chat(
        partitions=None,
        payload={"messages": [{"role": "user", "content": "Explain [Source 1]"}], "metadata": {}},
        prepare_sources=lambda d, w: [],
        model_name="m1",
    )

    assert out["choices"][0]["message"]["content"] == answer
    assert json.loads(out["extra"])["sources"] == []


@pytest.mark.asyncio
async def test_chat_direct_mode_preserves_literal_terminal_sources_marker():
    answer = "The requested literal notation is:\n[Sources: 1]"
    svc = _svc(llm=FakeLLM(chat_responses=[answer]))

    out = await svc.chat(
        partitions=None,
        payload={"messages": [{"role": "user", "content": "Repeat [Sources: 1]"}], "metadata": {}},
        prepare_sources=lambda d, w: [],
        model_name="m1",
    )

    assert out["choices"][0]["message"]["content"] == answer
    assert json.loads(out["extra"])["sources"] == []


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
    extra = json.loads(out["extra"])
    assert extra["sources"] == [{"source_type": "document", "n": 1}]  # only cited source 1
    assert extra["all_retrieved_sources"] == sources  # unfiltered, everything retrieved


@pytest.mark.asyncio
async def test_chat_all_retrieved_sources_survives_context_budget_truncation():
    """#847 review: all_retrieved_sources must reflect the complete retrieval
    set, not just the docs that fit the prompt's context-token budget — a
    doc dropped only for lack of room must still show up there."""
    chunks = [
        Chunk(id="c1", text="short", metadata={"_id": "c1"}),
        Chunk(id="c2", text="this one does not fit the token budget", metadata={"_id": "c2"}),
    ]
    svc = _svc(retrieval=FakeRetrieval(chunks=chunks), llm=FakeLLM(chat_responses=["answer [Sources: 1]"]))
    svc._max_context_tokens = qs.get_num_tokens()("[Source 1]\nshort")

    out = await svc.chat(
        partitions=["p"],
        payload={"messages": [{"role": "user", "content": "q"}], "metadata": {}},
        prepare_sources=lambda d, w: [{"id": doc.metadata.get("_id")} for doc in d],
        model_name="m",
    )

    extra = json.loads(out["extra"])
    assert extra["sources"] == [{"id": "c1"}]  # only the doc that fit the prompt and was cited
    assert extra["all_retrieved_sources"] == [{"id": "c1"}, {"id": "c2"}]  # both, unfiltered


@pytest.mark.asyncio
async def test_chat_stream_all_retrieved_sources_survives_context_budget_truncation():
    chunks = [
        Chunk(id="c1", text="short", metadata={"_id": "c1"}),
        Chunk(id="c2", text="this one does not fit the token budget", metadata={"_id": "c2"}),
    ]
    stream_lines = [
        'data: {"choices":[{"delta":{"content":"answer [Sources: 1]"},"finish_reason":null}]}\n\n',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
        "data: [DONE]\n\n",
    ]
    svc = _svc(retrieval=FakeRetrieval(chunks=chunks), llm=FakeLLM(stream_lines=stream_lines))
    svc._max_context_tokens = qs.get_num_tokens()("[Source 1]\nshort")

    lines = [
        line
        async for line in svc.chat_stream(
            partitions=["p"],
            payload={"messages": [{"role": "user", "content": "q"}], "metadata": {}},
            prepare_sources=lambda d, w: [{"id": doc.metadata.get("_id")} for doc in d],
            model_name="m",
        )
    ]
    chunks_out = [
        json.loads(line[len("data: ") :])
        for line in lines
        if line.startswith("data: ") and line.strip() != "data: [DONE]"
    ]
    extra = next(json.loads(c["extra"]) for c in reversed(chunks_out) if c.get("extra") not in (None, "{}"))

    assert extra["sources"] == [{"id": "c1"}]
    assert extra["all_retrieved_sources"] == [{"id": "c1"}, {"id": "c2"}]


@pytest.mark.asyncio
async def test_complete_all_retrieved_sources_survives_context_budget_truncation():
    chunks = [
        Chunk(id="c1", text="short", metadata={"_id": "c1"}),
        Chunk(id="c2", text="this one does not fit the token budget", metadata={"_id": "c2"}),
    ]
    svc = _svc(retrieval=FakeRetrieval(chunks=chunks), llm=FakeLLM(gen_text="answer\n[Sources: 1]"))
    svc._max_context_tokens = qs.get_num_tokens()("[Source 1]\nshort")

    out = await svc.complete(
        partitions=["p"],
        payload={"prompt": "q"},
        prepare_sources=lambda d, w: [{"id": doc.metadata.get("_id")} for doc in d],
    )

    extra = json.loads(out["extra"])
    assert extra["sources"] == [{"id": "c1"}]
    assert extra["all_retrieved_sources"] == [{"id": "c1"}, {"id": "c2"}]


@pytest.mark.asyncio
async def test_chat_recovers_context_markers_as_citations():
    svc = _svc(llm=FakeLLM(chat_responses=["First claim [Source 2]. Second claim [Source 1][Source 2]."]))
    sources = [{"source_type": "document", "n": 1}, {"source_type": "document", "n": 2}]
    out = await svc.chat(
        partitions=["p"],
        payload={"messages": [{"role": "user", "content": "q"}], "metadata": {}},
        prepare_sources=lambda d, w: sources,
        model_name="m",
    )

    assert out["choices"][0]["message"]["content"] == "First claim. Second claim."
    assert json.loads(out["extra"])["sources"] == sources


@pytest.mark.asyncio
async def test_chat_conversational_request_skips_partition_retrieval():
    query_json = json.dumps({"requires_retrieval": False, "query_list": []})
    llm = FakeLLM(chat_responses=[query_json, "I can help you search and summarize documents."])
    retrieval = FakeRetrieval()
    svc = _svc(mode="ChatBotRag", llm=llm, retrieval=retrieval)

    out = await svc.chat(
        partitions=["p"],
        payload={"messages": [{"role": "user", "content": "How can you help me?"}], "metadata": {}},
        prepare_sources=lambda d, w: [{"source_type": "document"}] if d or w else [],
        model_name="m",
    )

    assert retrieval.retrieve_multi_calls == []
    assert out["choices"][0]["message"]["content"] == "I can help you search and summarize documents."
    assert json.loads(out["extra"])["sources"] == []
    answer_messages = llm.chat_calls[1][0]
    assert answer_messages[0]["role"] == "system"
    assert "OpenRAG" in answer_messages[0]["content"]
    assert "LINAGORA" in answer_messages[0]["content"]
    assert "document-grounded RAG system" in answer_messages[0]["content"]


@pytest.mark.asyncio
async def test_chat_conversational_request_keeps_spoken_style_prompt():
    query_json = json.dumps({"requires_retrieval": False, "query_list": []})
    llm = FakeLLM(chat_responses=[query_json, "I'm OpenRAG, built by LINAGORA."])
    svc = _svc(mode="ChatBotRag", llm=llm)

    await svc.chat(
        partitions=["p"],
        payload={
            "messages": [{"role": "user", "content": "Who are you?"}],
            "metadata": {"spoken_style_answer": True},
        },
        prepare_sources=lambda d, w: [],
        model_name="m",
    )

    answer_system_prompt = llm.chat_calls[1][0][0]["content"]
    assert "OpenRAG" in answer_system_prompt
    assert "LINAGORA" in answer_system_prompt
    assert "short (1-2 sentences)" in answer_system_prompt


@pytest.mark.asyncio
async def test_chat_mixed_request_still_retrieves_documents():
    query_json = json.dumps(
        {
            "requires_retrieval": True,
            "query_list": [{"query": "Product A revenue in Q1", "temporal_filters": None}],
        }
    )
    llm = FakeLLM(chat_responses=[query_json, "Revenue was 10 million. [Sources: 1]"])
    retrieval = FakeRetrieval()
    svc = _svc(mode="ChatBotRag", llm=llm, retrieval=retrieval)

    out = await svc.chat(
        partitions=["p"],
        payload={"messages": [{"role": "user", "content": "Hello, what was Product A revenue in Q1?"}]},
        prepare_sources=lambda d, w: [{"source_type": "document", "filename": "report.pdf"}],
        model_name="m",
    )

    assert len(retrieval.retrieve_multi_calls) == 1
    assert json.loads(out["extra"])["sources"] == [{"source_type": "document", "filename": "report.pdf"}]


@pytest.mark.asyncio
async def test_chat_inconsistent_classifier_result_prefers_supplied_query():
    query_json = json.dumps(
        {
            "requires_retrieval": False,
            "query_list": [{"query": "Product A revenue", "temporal_filters": None}],
        }
    )
    llm = FakeLLM(chat_responses=[query_json, "Revenue was 10 million. [Sources: 1]"])
    retrieval = FakeRetrieval()
    svc = _svc(mode="ChatBotRag", llm=llm, retrieval=retrieval)

    await svc.chat(
        partitions=["p"],
        payload={"messages": [{"role": "user", "content": "What was Product A revenue?"}]},
        prepare_sources=lambda d, w: [{"source_type": "document"}],
        model_name="m",
    )

    assert len(retrieval.retrieve_multi_calls) == 1


@pytest.mark.asyncio
async def test_chat_without_citation_keeps_retrieved_sources():
    """No tag at all means the model didn't report citations, not that the answer is unsourced."""
    svc = _svc(llm=FakeLLM(chat_responses=["A general answer with no citation marker."]))
    sources = [{"source_type": "document", "filename": "unrelated.pdf"}]

    out = await svc.chat(
        partitions=["p"],
        payload={"messages": [{"role": "user", "content": "How can you help me?"}], "metadata": {}},
        prepare_sources=lambda d, w: sources,
        model_name="m",
    )

    assert json.loads(out["extra"])["sources"] == sources


@pytest.mark.asyncio
async def test_chat_invalid_citation_does_not_fallback_to_unrelated_sources():
    svc = _svc(llm=FakeLLM(chat_responses=["Answer. [Sources: 99]"]))
    sources = [{"source_type": "document", "filename": "unrelated.pdf"}]

    out = await svc.chat(
        partitions=["p"],
        payload={"messages": [{"role": "user", "content": "Question"}], "metadata": {}},
        prepare_sources=lambda d, w: sources,
        model_name="m",
    )

    assert json.loads(out["extra"])["sources"] == []


@pytest.mark.asyncio
async def test_chat_structured_output_keeps_retrieved_sources_without_citation_marker():
    structured_answer = '{"answer": "Use [Source 1]", "literal_format": "[Sources: 1]"}'
    svc = _svc(llm=FakeLLM(chat_responses=[structured_answer]))
    sources = [{"source_type": "document", "filename": "report.pdf"}]

    out = await svc.chat(
        partitions=["p"],
        payload={
            "messages": [{"role": "user", "content": "Question"}],
            "metadata": {},
            "response_format": {"type": "json_object"},
        },
        prepare_sources=lambda d, w: sources,
        model_name="m",
    )

    assert out["choices"][0]["message"]["content"] == structured_answer
    assert json.loads(out["extra"])["sources"] == sources


@pytest.mark.asyncio
async def test_chat_stream_structured_output_preserves_source_like_json_values():
    structured_answer = '{"answer":"Use [Source 1]","literal_format":"[Sources: 1]"}'
    stream_lines = [
        "data: "
        + json.dumps(
            {
                "choices": [
                    {
                        "delta": {"content": structured_answer},
                        "finish_reason": None,
                    }
                ]
            }
        )
        + "\n\n",
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
        "data: [DONE]\n\n",
    ]
    svc = _svc(llm=FakeLLM(stream_lines=stream_lines))
    sources = [{"source_type": "document", "filename": "report.pdf"}]

    lines = [
        line
        async for line in svc.chat_stream(
            partitions=["p"],
            payload={
                "messages": [{"role": "user", "content": "Question"}],
                "metadata": {},
                "response_format": {"type": "json_object"},
            },
            prepare_sources=lambda d, w: sources,
            model_name="m",
        )
    ]
    chunks = [
        json.loads(line[len("data: ") :])
        for line in lines
        if line.startswith("data: ") and line.strip() != "data: [DONE]"
    ]
    content = "".join(
        choice.get("delta", {}).get("content", "") for chunk in chunks for choice in chunk.get("choices", [])
    )
    extra = next(json.loads(chunk["extra"]) for chunk in reversed(chunks) if chunk.get("extra") not in (None, "{}"))

    assert content == structured_answer
    assert extra["sources"] == sources


@pytest.mark.asyncio
async def test_structured_websearch_returns_only_sources_included_in_context():
    first = SimpleNamespace(
        url="https://example.test/included",
        title="Included",
        content="short evidence",
        snippet="",
    )
    excluded = SimpleNamespace(
        url="https://example.test/excluded",
        title="Excluded",
        content="long evidence that does not fit",
        snippet="",
    )
    web = FakeWeb(results=[first, excluded])
    web.max_tokens = qs.get_num_tokens()("[Source 1]\nIncluded\nshort evidence")
    svc = _svc(
        llm=FakeLLM(chat_responses=['{"answer": "structured"}']),
        retrieval=FakeRetrieval(chunks=[]),
        web=web,
    )

    out = await svc.chat(
        partitions=None,
        payload={
            "messages": [{"role": "user", "content": "Question"}],
            "metadata": {"websearch": True},
            "response_format": {"type": "json_object"},
        },
        prepare_sources=lambda _docs, results: [{"url": result.url} for result in results],
        model_name="m",
    )

    extra = json.loads(out["extra"])
    assert extra["sources"] == [{"url": "https://example.test/included"}]
    # #847 review: excluded (didn't fit the web token budget) still shows up
    # in all_retrieved_sources.
    assert extra["all_retrieved_sources"] == [
        {"url": "https://example.test/included"},
        {"url": "https://example.test/excluded"},
    ]


@pytest.mark.asyncio
async def test_explicit_websearch_forces_retrieval_for_conversational_classifier_result():
    query_json = json.dumps({"requires_retrieval": False, "query_list": []})
    llm = FakeLLM(chat_responses=[query_json])
    retrieval = FakeRetrieval(chunks=[])
    web = FakeWeb()
    svc = _svc(mode="ChatBotRag", llm=llm, retrieval=retrieval, web=web)

    await svc._prepare_chat(
        ["p"],
        {
            "messages": [{"role": "user", "content": "What is happening today?"}],
            "metadata": {"websearch": True},
        },
    )

    assert len(retrieval.retrieve_multi_calls) == 1
    assert web.calls == ["What is happening today?"]


@pytest.mark.asyncio
async def test_websearch_with_partition_fuses_docs_via_retrieve_multi():
    # #707/#740: with a partition AND websearch enabled, the document branch must
    # fuse through retrieve_multi (which honors the partition's rrf_k), NOT the
    # legacy retrieve_per_query + fuse()@60. Revert-proves _gather_rag_and_web:
    # reverting it to retrieve_per_query + fuse flips these call records and fails.
    retrieval = FakeRetrieval()
    web_result = SimpleNamespace(url="https://ex.com", title="T", content="web body", snippet="")
    svc = _svc(retrieval=retrieval, web=FakeWeb(results=[web_result]))
    _payload, _docs, web, _retrieved_docs, _retrieved_web, _citation_protocol_active = await svc._prepare_chat(
        ["p"], {"messages": [{"role": "user", "content": "q"}], "metadata": {"websearch": True}}
    )
    assert len(retrieval.retrieve_multi_calls) == 1  # doc branch fused via the rrf_k-aware retrieve_multi
    assert retrieval.retrieve_per_query_calls == []  # legacy per-query + fuse()@60 path NOT used
    assert web and web[0].url == "https://ex.com"  # websearch branch actually taken


@pytest.mark.asyncio
async def test_answer_system_prompt_comes_from_prompt_service():
    # Revert-proves the query seam: the payload's system message is built from
    # prompt_service.resolve_prompt("sys_prompt", ...), resolved request-time —
    # not a startup snapshot. Reverting query_service to load_template_by_key at
    # __init__ makes the marker disappear.
    class MarkerPromptService:
        def __init__(self):
            self.seen: list = []

        async def resolve_prompt(self, prompt_type, names=None):
            self.seen.append(prompt_type)
            return "MARKER-SYS::{context}"

    svc = _svc(retrieval=FakeRetrieval())  # SimpleRag → no contextualizer call
    marker = MarkerPromptService()
    svc._prompt_service = marker

    payload, _docs, _web, _retrieved_docs, _retrieved_web, _citation_protocol_active = await svc._prepare_chat(
        ["p"], {"messages": [{"role": "user", "content": "q"}], "metadata": {}}
    )

    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][0]["content"].startswith("MARKER-SYS::")
    assert "sys_prompt" in marker.seen


@pytest.mark.asyncio
async def test_generation_prompt_name_from_partition_reaches_resolver():
    # Revert-proves #12: a single owning partition's generation_prompt_names is
    # passed to resolve_prompt as the candidate name. Multi-partition / "all"
    # pass None (global default).
    class RecordingPromptService:
        def __init__(self):
            self.calls: list = []

        async def resolve_prompt(self, prompt_type, names=None):
            self.calls.append((prompt_type, tuple(names or ())))
            return "SYS::{context}"

    svc = _svc(retrieval=FakeRetrieval())
    rec = RecordingPromptService()
    svc._prompt_service = rec
    svc._config.partitions = {
        "p": SimpleNamespace(generation_prompt_names={"sys_prompt": "legal"}, chat_history_depth=4)
    }

    await svc._prepare_chat(["p"], {"messages": [{"role": "user", "content": "q"}], "metadata": {}})
    assert ("sys_prompt", ("legal",)) in rec.calls

    rec.calls.clear()
    await svc._prepare_chat(["p", "q"], {"messages": [{"role": "user", "content": "q"}], "metadata": {}})
    assert ("sys_prompt", (None,)) in rec.calls  # no single owning partition


@pytest.mark.asyncio
async def test_spoken_style_metadata_swaps_the_answer_prompt():
    """`metadata.spoken_style_answer` is a public API flag (and a Chainlit
    command) that swaps the answer prompt for a voice-friendly one. Nothing
    asserted this, so the whole feature could be — and briefly was — deleted
    with the suite still green.
    """

    class RecordingPromptService:
        def __init__(self):
            self.calls: list = []

        async def resolve_prompt(self, prompt_type, names=None):
            self.calls.append((prompt_type, tuple(names or ())))
            return "SPOKEN::{context}"

    svc = _svc(retrieval=FakeRetrieval())
    rec = RecordingPromptService()
    svc._prompt_service = rec
    svc._config.partitions = {
        "p": SimpleNamespace(generation_prompt_names={"spoken_style_answer": "voice"}, chat_history_depth=4)
    }

    await svc._prepare_chat(
        ["p"],
        {"messages": [{"role": "user", "content": "q"}], "metadata": {"spoken_style_answer": True}},
    )
    # The spoken-style type is resolved, and the partition may name its own.
    assert ("spoken_style_answer", ("voice",)) in rec.calls
    assert not any(call[0] == "sys_prompt" for call in rec.calls)

    # Without the flag the ordinary answer prompt is used.
    rec.calls.clear()
    await svc._prepare_chat(["p"], {"messages": [{"role": "user", "content": "q"}], "metadata": {}})
    assert any(call[0] == "sys_prompt" for call in rec.calls)
    assert not any(call[0] == "spoken_style_answer" for call in rec.calls)


@pytest.mark.asyncio
async def test_query_contextualizer_name_from_retrieval_preset_reaches_resolver():
    # query_contextualizer is selected on the partition's RETRIEVAL preset (not
    # generation prompts). A single owning partition's preset name is passed to
    # resolve_prompt; multi-partition passes None (global default).
    class RecordingPromptService:
        def __init__(self):
            self.calls: list = []

        async def resolve_prompt(self, prompt_type, names=None):
            self.calls.append((prompt_type, tuple(names or ())))
            return "CTX"

    payload = json.dumps({"query_list": [{"query": "rewritten", "temporal_filters": None}]})
    svc = _svc(llm=FakeLLM(chat_responses=[payload, payload]), mode="ChatBotRag")
    rec = RecordingPromptService()
    svc._prompt_service = rec
    svc._config.partitions = {"p": SimpleNamespace(retrieval=SimpleNamespace(query_contextualizer_prompt_name="myctx"))}

    await svc.generate_query([{"role": "user", "content": "q"}], partition=["p"])
    assert ("query_contextualizer", ("myctx",)) in rec.calls

    rec.calls.clear()
    await svc.generate_query([{"role": "user", "content": "q"}], partition=["p", "q"])
    assert ("query_contextualizer", (None,)) in rec.calls  # no single owning partition


@pytest.mark.asyncio
async def test_chat_with_valid_workspace_scopes_search_to_file_ids():
    scope = WorkspaceScope(workspace_id="w1", partition="p1", file_ids=["fa", "fb"])
    retrieval = FakeRetrieval()
    svc = _svc(
        retrieval=retrieval, llm=FakeLLM(chat_responses=["answer [Sources: none]"]), workspace=FakeWorkspace(scope)
    )
    await svc.chat(
        partitions=["p1"],
        payload={"messages": [{"role": "user", "content": "q"}], "metadata": {"workspace": "w1"}},
        prepare_sources=lambda d, w: [],
        model_name="m",
    )
    assert len(retrieval.retrieve_multi_calls) == 1
    call = retrieval.retrieve_multi_calls[0]
    assert call["partitions"] == ["p1"]
    assert call["filter_params"] == {"file_id": ["fa", "fb"]}


@pytest.mark.asyncio
async def test_chat_workspace_restricts_openrag_all_to_owning_partition():
    # "openrag-all" reaches _prepare_chat as partitions=["all"] — a workspace
    # must narrow retrieval to its single owning partition, never search
    # every accessible partition with just a file_id filter (#706).
    scope = WorkspaceScope(workspace_id="w1", partition="only-this-one", file_ids=["fa"])
    retrieval = FakeRetrieval()
    svc = _svc(
        retrieval=retrieval, llm=FakeLLM(chat_responses=["answer [Sources: none]"]), workspace=FakeWorkspace(scope)
    )
    await svc.chat(
        partitions=["all"],
        payload={"messages": [{"role": "user", "content": "q"}], "metadata": {"workspace": "w1"}},
        prepare_sources=lambda d, w: [],
        model_name="m",
    )
    call = retrieval.retrieve_multi_calls[0]
    assert call["partitions"] == ["only-this-one"]


@pytest.mark.asyncio
async def test_chat_empty_workspace_scopes_to_zero_files_not_full_partition():
    scope = WorkspaceScope(workspace_id="w1", partition="p1", file_ids=[])
    retrieval = FakeRetrieval()
    svc = _svc(
        retrieval=retrieval, llm=FakeLLM(chat_responses=["answer [Sources: none]"]), workspace=FakeWorkspace(scope)
    )
    await svc.chat(
        partitions=["p1"],
        payload={"messages": [{"role": "user", "content": "q"}], "metadata": {"workspace": "w1"}},
        prepare_sources=lambda d, w: [],
        model_name="m",
    )
    call = retrieval.retrieve_multi_calls[0]
    assert call["filter_params"] == {"file_id": []}  # must stay explicit, never None/omitted


@pytest.mark.asyncio
async def test_chat_invalid_workspace_raises_instead_of_falling_back():
    # Fail closed: an unknown/inaccessible workspace must error, never
    # silently widen the search to the full partition (#706).
    svc = _svc(workspace=FakeWorkspace(None))
    with pytest.raises(WorkspaceNotFoundError):
        await svc.chat(
            partitions=["p1"],
            payload={"messages": [{"role": "user", "content": "q"}], "metadata": {"workspace": "ghost"}},
            prepare_sources=lambda d, w: [],
            model_name="m",
        )


@pytest.mark.asyncio
async def test_chat_stream_invalid_workspace_raises_instead_of_falling_back():
    svc = _svc(workspace=FakeWorkspace(None))
    with pytest.raises(WorkspaceNotFoundError):
        async for _ in svc.chat_stream(
            partitions=["p1"],
            payload={"messages": [{"role": "user", "content": "q"}], "metadata": {"workspace": "ghost"}},
            prepare_sources=lambda d, w: [],
            model_name="m",
        ):
            pass


@pytest.mark.asyncio
async def test_chat_without_workspace_unaffected():
    retrieval = FakeRetrieval()
    svc = _svc(retrieval=retrieval, llm=FakeLLM(chat_responses=["answer [Sources: none]"]))
    await svc.chat(
        partitions=["p1"],
        payload={"messages": [{"role": "user", "content": "q"}], "metadata": {}},
        prepare_sources=lambda d, w: [],
        model_name="m",
    )
    call = retrieval.retrieve_multi_calls[0]
    assert call["partitions"] == ["p1"]
    assert call["filter_params"] is None


@pytest.mark.asyncio
async def test_complete_direct_mode_preserves_literal_source_marker():
    answer = "text body\n[Sources: none]"
    svc = _svc(llm=FakeLLM(gen_text=answer))
    out = await svc.complete(
        partitions=None,
        payload={"prompt": "do x"},
        prepare_sources=lambda d, w: [{"x": 1}] if d or w else [],
    )
    assert out["choices"][0]["text"] == answer
    assert json.loads(out["extra"])["sources"] == []


@pytest.mark.asyncio
async def test_complete_conversational_request_uses_openrag_prompt_without_retrieval():
    query_json = json.dumps({"requires_retrieval": False, "query_list": []})
    llm = FakeLLM(chat_responses=[query_json], gen_text="I am OpenRAG.\n[Sources: none]")
    retrieval = FakeRetrieval()
    svc = _svc(mode="ChatBotRag", llm=llm, retrieval=retrieval)

    out = await svc.complete(
        partitions=["p"],
        payload={"prompt": "Who are you?"},
        prepare_sources=lambda d, w: [],
    )

    assert retrieval.retrieve_multi_calls == []
    assert out["choices"][0]["text"] == "I am OpenRAG."
    answer_prompt = llm.generate_calls[0][0]
    assert "OpenRAG" in answer_prompt
    assert "LINAGORA" in answer_prompt
    assert "document-grounded RAG system" in answer_prompt
    assert "Who are you?" in answer_prompt


@pytest.mark.asyncio
async def test_complete_partition_request_keeps_context_and_filters_citations():
    llm = FakeLLM(gen_text="The answer is grounded.\n[Sources: 1]")
    svc = _svc(llm=llm)
    sources = [{"source_type": "document", "filename": "report.pdf"}]

    out = await svc.complete(
        partitions=["p"],
        payload={"prompt": "What does the report say?"},
        prepare_sources=lambda d, w: sources,
    )

    assert out["choices"][0]["text"] == "The answer is grounded."
    assert json.loads(out["extra"])["sources"] == sources
    answer_prompt = llm.generate_calls[0][0]
    assert "ctx" in answer_prompt
    assert "What does the report say?" in answer_prompt


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
    a = SimpleNamespace(url="https://example.test/one")
    b = SimpleNamespace(url="https://example.test/one")
    c = SimpleNamespace(url="https://example.test/two")
    assert qs._dedupe_web([[a, b], [c]]) == [a, c]


def test_dedupe_web_drops_invalid_urls_before_source_numbering():
    invalid = SimpleNamespace(url="javascript:alert(1)")
    valid = SimpleNamespace(url="https://example.test/evidence")

    assert qs._dedupe_web([[invalid, valid]]) == [valid]


def test_sampling_strips_transport_keys():
    out = qs._sampling({"messages": [], "stream": True, "model": "m", "temperature": 0.5})
    assert out == {"temperature": 0.5}


# --------------------------------------------------------------------------- #
# _sanitize_messages
# --------------------------------------------------------------------------- #


def test_sanitize_valid_alternating_unchanged():
    """Valid alternating history → identical output."""
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "again"},
    ]
    assert QueryService._sanitize_messages(msgs) == msgs


def test_sanitize_empty_content_replaced_with_placeholder():
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


def test_sanitize_whitespace_only_replaced():
    """Whitespace-only assistant content → replaced with NO_CONTENT."""
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "   "},
    ]
    out = QueryService._sanitize_messages(msgs)
    assert out[1]["content"] == "NO_CONTENT"


def test_sanitize_no_content_key_replaced():
    """Assistant with no 'content' key → replaced with NO_CONTENT, other keys kept."""
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "name": "bot"},
    ]
    out = QueryService._sanitize_messages(msgs)
    assert out[1]["content"] == "NO_CONTENT"
    assert out[1]["name"] == "bot"


def test_sanitize_empty_list_content_replaced():
    """Empty multimodal content list → treated as empty, replaced with NO_CONTENT."""
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": []},
    ]
    out = QueryService._sanitize_messages(msgs)
    assert out[1]["content"] == "NO_CONTENT"


def test_sanitize_assistant_with_tool_calls_untouched():
    """Empty assistant content is legitimate when carrying tool_calls → untouched."""
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "type": "function"}]},
    ]
    out = QueryService._sanitize_messages(msgs)
    assert out == msgs


def test_sanitize_assistant_with_function_call_untouched():
    """Empty assistant content is legitimate when carrying function_call → untouched."""
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "function_call": {"name": "f"}},
    ]
    out = QueryService._sanitize_messages(msgs)
    assert out == msgs


def test_sanitize_empty_user_not_touched():
    """User message with empty content → kept as-is (only assistant is patched)."""
    msgs = [{"role": "user", "content": ""}]
    assert QueryService._sanitize_messages(msgs) == msgs


def test_sanitize_multiple_empty_assistants_all_replaced():
    """Multiple empty assistants → all replaced, alternation intact."""
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


def test_sanitize_multimodal_content_untouched():
    """Non-empty multimodal content list → never replaced."""
    msgs = [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://x"}}]},
        {"role": "assistant", "content": [{"type": "text", "text": "desc"}]},
    ]
    assert QueryService._sanitize_messages(msgs) == msgs


def test_sanitize_empty_list():
    """Empty input → empty output."""
    assert QueryService._sanitize_messages([]) == []


def test_sanitize_system_message_preserved():
    """System message in head → preserved untouched."""
    msgs = [
        {"role": "system", "content": "You are an assistant."},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    assert QueryService._sanitize_messages(msgs) == msgs


@pytest.mark.asyncio
async def test_conversational_reply_resolves_its_prompt_from_the_library():
    """The no-retrieval path (a greeting / capability question) came from #807
    and read an __init__-time snapshot this branch removes. Git auto-merged that
    reference without flagging a conflict, so nothing but this test proves the
    conversational reply resolves through PromptService at all.
    """

    class RecordingPromptService:
        def __init__(self):
            self.calls: list = []

        async def resolve_prompt(self, prompt_type, names=None):
            self.calls.append((prompt_type, tuple(names or ())))
            # Each type is rendered with its own placeholders, so the stub has
            # to answer in kind rather than with one shared string.
            if prompt_type == "query_contextualizer":
                return "CTX {query_language} {current_date}"
            return "CONVERSATIONAL {context} {current_date}"

    payload = json.dumps({"requires_retrieval": False, "query_list": []})
    svc = _svc(llm=FakeLLM(chat_responses=[payload]), mode="ChatBotRag")
    rec = RecordingPromptService()
    svc._prompt_service = rec
    svc._config.partitions = {
        "p": SimpleNamespace(generation_prompt_names={"sys_prompt": "chatty"}, chat_history_depth=4)
    }

    out, docs, web, _retrieved_docs, _retrieved_web, _ = await svc._prepare_chat(
        ["p"], {"messages": [{"role": "user", "content": "hello!"}], "metadata": {}}
    )

    # Resolved from the library, honouring the partition's selection, and no
    # retrieval happened.
    assert ("sys_prompt", ("chatty",)) in rec.calls
    assert docs == [] and web == []
    assert out["messages"][0]["role"] == "system"
    assert "CONVERSATIONAL" in out["messages"][0]["content"]
