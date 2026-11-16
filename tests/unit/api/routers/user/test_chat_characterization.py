"""Characterization tests for the chat endpoints.

They pin what ``/v1/chat/completions`` and ``/v1/completions`` do today, for each
combination of entry point and ``metadata`` flags: every call made to the LLMs,
to retrieval and to web search, and the response returned, ``extra`` included.
A refactor of the chat path must leave these snapshots unchanged; a change to
one is a change of behaviour and has to be explained.

The requests go through the real router and the real chat service. Only the
edges are faked: the LLMs, retrieval, web search, workspaces and the partition
table. ``_build_chat_service`` is the one place that knows how the service is
assembled, so it is what follows the refactor.

The clock, the language detector and the token counter are fixed so the
snapshots are deterministic.

To rewrite the snapshots after an intended change::

    UPDATE_CHAT_SNAPSHOTS=1 uv run pytest tests/unit/api/routers/user/test_chat_characterization.py
"""

from __future__ import annotations

import copy
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import core.prompts.chat_prompt_builder as chat_prompt_builder
import core.utils.text as text_utils
import di.chat as chat_factory
import pytest
import services.inference.runtime as inference_runtime
import services.orchestrators.chat_service as qs
from api.dependencies import model_limits
from api.dependencies.auth import current_user, current_user_or_admin_partitions_list
from api.dependencies.retrieval_diagnostics import RetrievalDiagnosticsGuard, get_retrieval_diagnostics_guard
from api.error_handlers import register_error_handlers
from api.routers.user import chat
from core.config.indexation_pipeline import IndexationPipelineConfig
from core.config.model_endpoints import LLM_CONTEXT_SIZE_KEY, ModelEndpointConfig
from core.config.retrieval_pipeline import RetrievalPipelineConfig
from core.config.root import Settings
from core.models.chunk import Chunk
from core.models.preset import PartitionConfig
from core.models.retrieval_result import ScoredChunk
from core.models.workspace import WorkspaceScope
from core.websearch import WebResult
from di.chat import create_chat_service
from di.providers import get_chat_service, get_config, get_partition_service
from fastapi import FastAPI
from services.orchestrators.chat_service import ChatService
from services.orchestrators.prompt_service import PromptService

SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "chat_characterization"
UPDATE_SNAPSHOTS = os.environ.get("UPDATE_CHAT_SNAPSHOTS") == "1"

FROZEN_NOW = datetime(2026, 3, 4, 10, 20, 30, tzinfo=UTC)
WINDOW = 32768
PARTITIONS = ("legal", "hr")
USER = {"id": 2, "is_admin": False}
ADMIN = {"id": 1, "is_admin": True}


# --------------------------------------------------------------------------- #
# Deterministic environment
# --------------------------------------------------------------------------- #


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW.astimezone(tz) if tz is not None else FROZEN_NOW.replace(tzinfo=None)


@asynccontextmanager
async def _no_semaphore():
    yield


def _word_count(text: str) -> int:
    return len(text.split())


@pytest.fixture(autouse=True)
def _deterministic(monkeypatch):
    for module in (qs, chat_factory, chat_prompt_builder):
        monkeypatch.setattr(module, "datetime", _FrozenDatetime)
    monkeypatch.setattr(text_utils, "_cached_length_function", _word_count)
    for module in (chat_factory, inference_runtime):
        monkeypatch.setattr(module, "detect_language", lambda _text, **_kwargs: "en")
        monkeypatch.setattr(module, "get_llm_semaphore", _no_semaphore)
    monkeypatch.setattr(model_limits, "_max_model_tokens_by_name", {})


# --------------------------------------------------------------------------- #
# Fakes at the edges
# --------------------------------------------------------------------------- #


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, set | frozenset):
        return sorted(_jsonable(v) for v in value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return f"<{type(value).__name__}>"


_MAP_SYSTEM_PREFIX = "You are an AI assistant specialized in extracting"
_CONTEXTUALIZER_USER_PREFIX = "Here is the chat history"


@dataclass
class Script:
    """What the fake LLMs answer, by kind of call."""

    contextualizer: list[Any] = field(default_factory=lambda: [_queries("notice period")])
    answers: list[dict] = field(
        default_factory=lambda: [{"content": "The notice period is three months. [Sources: 1]"}]
    )
    completion: str = "The notice period is three months. [Sources: 1]"

    def reply(self, messages: list[dict], kwargs: dict) -> dict:
        system = messages[0]["content"] if messages and messages[0].get("role") == "system" else ""
        last = messages[-1].get("content") or "" if messages else ""
        if isinstance(last, str) and last.startswith(_CONTEXTUALIZER_USER_PREFIX):
            answer = self.contextualizer.pop(0) if len(self.contextualizer) > 1 else self.contextualizer[0]
            return {"content": answer if isinstance(answer, str) else json.dumps(answer)}
        if isinstance(system, str) and system.startswith(_MAP_SYSTEM_PREFIX):
            relevant = "IRRELEVANT" not in last
            summary = f"Summary of: {last.splitlines()[1][:40]}" if relevant else ""
            return {"content": json.dumps({"relevancy": relevant, "summary": summary})}
        return dict(self.answers.pop(0) if len(self.answers) > 1 else self.answers[0])


def _queries(*queries: str, intent: str = "other", requires_retrieval: bool = True, **query_fields) -> dict:
    return {
        "intent": intent,
        "requires_retrieval": requires_retrieval,
        "query_list": [{"query": query, **query_fields} for query in queries],
    }


class FakeLLM:
    def __init__(self, name: str, log: list, script: Script) -> None:
        self.name = name
        self._log = log
        self._script = script

    def _record(self, method: str, **call) -> None:
        self._log.append({"llm": self.name, "method": method, **_jsonable(copy.deepcopy(call))})

    async def chat(self, messages, **kwargs):
        self._record("chat", messages=messages, kwargs=kwargs)
        reply = self._script.reply(messages, kwargs)
        message = {"role": "assistant", "content": reply.get("content", "")}
        if "tool_calls" in reply:
            message["tool_calls"] = reply["tool_calls"]
        finish_reason = "tool_calls" if "tool_calls" in reply else "stop"
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 1772619630,
            "model": f"upstream-{self.name}",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        }

    async def stream_chat(self, messages, **kwargs):
        self._record("stream_chat", messages=messages, kwargs=kwargs)
        reply = self._script.reply(messages, kwargs)

        def chunk(delta: dict, finish_reason: str | None = None) -> str:
            body = {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "created": 1772619630,
                "model": f"upstream-{self.name}",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
            return f"data: {json.dumps(body)}\n\n"

        content = reply.get("content", "")
        for start in range(0, len(content), 24):
            yield chunk({"content": content[start : start + 24]})
        if "tool_calls" in reply:
            yield chunk({"tool_calls": [{"index": 0, **call} for call in reply["tool_calls"]]})
        yield chunk({}, "tool_calls" if "tool_calls" in reply else "stop")
        yield "data: [DONE]\n\n"

    async def generate(self, prompt, **kwargs):
        self._record("generate", prompt=prompt, kwargs=kwargs)
        return {
            "id": "cmpl-fake",
            "object": "text_completion",
            "created": 1772619630,
            "model": f"upstream-{self.name}",
            "choices": [{"index": 0, "text": self._script.completion, "finish_reason": "stop"}],
        }


def _chunk(
    chunk_id: str,
    file_id: str,
    text: str,
    *,
    partition: str = "legal",
    filename: str | None = None,
    page: int | None = 1,
    created_at: str = "2026-01-15T09:30:00+00:00",
    rerank_score: float | None = None,
) -> Chunk:
    chunk = Chunk(
        id=chunk_id,
        document_id=file_id,
        text=text,
        partition=partition,
        page_number=page,
        metadata={
            "filename": filename or f"{file_id}.pdf",
            "source": f"/data/{partition}/{filename or f'{file_id}.pdf'}",
            "created_at": created_at,
        },
    )
    return ScoredChunk.from_chunk(chunk, rerank_score=rerank_score) if rerank_score is not None else chunk


DEFAULT_CHUNKS = (
    _chunk("101", "contract", "Employees give three months of notice before leaving.", rerank_score=0.91),
    _chunk("102", "handbook", "The notice period during probation is two weeks.", page=4, rerank_score=0.74),
    _chunk("103", "minutes", "The board approved the 2026 budget.", created_at="2026-02-20T16:00:00+00:00"),
)


class FakeRetrieval:
    def __init__(self, log: list, chunks: list[Chunk]) -> None:
        self._log = log
        self._chunks = chunks

    async def refresh_partition_configs(self) -> None:
        return None

    async def retrieve_multi(self, **kwargs):
        self._log.append({"method": "retrieve_multi", **_jsonable(kwargs)})
        file_ids = (kwargs.get("filter_params") or {}).get("file_id")
        chunks = [c for c in self._chunks if file_ids is None or c.document_id in file_ids]
        return chunks[: kwargs["top_k"]] if kwargs.get("top_k") else chunks

    @staticmethod
    def configuration_fingerprint(_partitions):
        return "fake-fingerprint"


class FakeWeb:
    max_tokens = 2000

    def __init__(self, log: list, results: list[WebResult]) -> None:
        self._log = log
        self._results = results

    async def search(self, query):
        self._log.append({"method": "web_search", "query": query})
        return list(self._results)


WEB_RESULTS = (
    WebResult(title="Labour code", url="https://example.org/labour-code", snippet="Notice periods by seniority."),
    WebResult(title="Guide", url="https://example.org/guide", snippet="How to compute a notice period."),
)


class FakeWorkspaces:
    """One workspace, ``ws-1``, in ``legal``, holding the contract and the handbook."""

    files = {"legal": {"contract", "handbook", "minutes"}, "hr": set()}

    async def resolve_scope(self, workspace_id, allowed_partitions):
        if workspace_id != "ws-1" or "legal" not in allowed_partitions:
            return None
        return WorkspaceScope(workspace_id="ws-1", partition="legal", file_ids=["contract", "handbook"])

    async def get_existing_file_ids(self, partition, file_ids):
        return [fid for fid in file_ids if fid in self.files.get(partition, set())]

    async def get_existing_file_ids_any_partition(self, file_ids):
        known = set().union(*self.files.values())
        return [fid for fid in file_ids if fid in known]


class FakePartitions:
    async def partition_exists(self, name: str) -> bool:
        return name in PARTITIONS


class _EmptyPromptRepo:
    """No stored prompts: every prompt resolves to its default template on disk."""

    async def get_by_name(self, prompt_type, name):
        return None

    async def get_default(self, prompt_type):
        return None


class _EmptyCatalog:
    """No catalog: the agent's file tools are built, but no scenario runs them."""

    async def list_catalog_files(self, partitions, *, file_ids=None, limit):
        return []

    async def get_file_metadata(self, file_id, partition):
        return None


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #


@dataclass
class Scenario:
    name: str
    body: dict
    path: str = "/v1/chat/completions"
    rag_mode: str = "ChatBotRag"
    script: Script = field(default_factory=Script)
    chunks: tuple = DEFAULT_CHUNKS
    web_results: tuple = ()
    user: dict = field(default_factory=lambda: USER)
    window: int = WINDOW
    top_n: int | None = None
    hr_chat_llm: str | None = None


def _chat(model: str | None, *messages: dict, metadata: dict | None = None, **params) -> dict:
    body: dict[str, Any] = {"messages": list(messages), **params}
    if model is not None:
        body["model"] = model
    if metadata is not None:
        body["metadata"] = metadata
    return body


def _user(content: str) -> dict:
    return {"role": "user", "content": content}


def _assistant(content: str) -> dict:
    return {"role": "assistant", "content": content}


QUESTION = _user("What is the notice period?")
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Weather for a city",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        },
    }
]
TOOL_CALL = {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}

SCENARIOS = [
    # ---- direct: no partition --------------------------------------------
    Scenario("direct", _chat("", QUESTION), script=Script(answers=[{"content": "Three months."}])),
    Scenario("direct_stream", _chat("", QUESTION, stream=True), script=Script(answers=[{"content": "Three months."}])),
    Scenario(
        "direct_client_system_prompt",
        _chat("", {"role": "system", "content": "Answer like a pirate."}, QUESTION),
        script=Script(answers=[{"content": "Three months, matey."}]),
    ),
    Scenario(
        "direct_client_tools",
        _chat("", _user("Weather in Paris?"), tools=TOOLS),
        script=Script(answers=[{"content": "", "tool_calls": [TOOL_CALL]}]),
    ),
    Scenario(
        "direct_ignores_rag_flags",
        _chat(
            "",
            QUESTION,
            metadata={"use_map_reduce": True, "workspace": "ws-1", "attachments": [{"id": "contract"}]},
        ),
        script=Script(answers=[{"content": "Three months."}]),
    ),
    Scenario("unknown_partition", _chat("openrag-sales", QUESTION)),
    Scenario("unknown_model", _chat("gpt-4o", QUESTION)),
    Scenario(
        "direct_tool_result_last",
        _chat(
            "",
            _user("Weather in Paris?"),
            {"role": "assistant", "content": None, "tool_calls": [TOOL_CALL]},
            {"role": "tool", "tool_call_id": "call_1", "content": "18 C, sunny"},
            tools=TOOLS,
        ),
        script=Script(answers=[{"content": "It is 18 C and sunny in Paris."}]),
    ),
    # ---- web search without a partition ------------------------------------
    Scenario(
        "web_only",
        _chat("", QUESTION, metadata={"websearch": True}),
        web_results=WEB_RESULTS,
        script=Script(answers=[{"content": "It depends on seniority. [Sources: 1]"}]),
    ),
    Scenario(
        "web_only_no_results",
        _chat("", QUESTION, metadata={"websearch": True}),
        script=Script(answers=[{"content": "Three months."}]),
    ),
    # ---- documents ---------------------------------------------------------
    Scenario("documents_simple_rag", _chat("openrag-legal", QUESTION), rag_mode="SimpleRag"),
    Scenario(
        "documents_chatbot_rag",
        _chat(
            "openrag-legal",
            _user("Who approved the budget?"),
            _assistant("The board did."),
            _user("And what is the notice period?"),
        ),
        script=Script(
            contextualizer=[_queries("notice period", "notice period during probation", date_order="newest")],
            answers=[{"content": "Three months, two weeks on probation. [Sources: 1, 2]"}],
        ),
    ),
    Scenario("documents_stream", _chat("openrag-legal", QUESTION, stream=True)),
    Scenario(
        "documents_and_web",
        _chat("openrag-legal", QUESTION, metadata={"websearch": True}),
        web_results=WEB_RESULTS,
        script=Script(answers=[{"content": "Three months; the law agrees. [Sources: 1, 4]"}]),
    ),
    Scenario(
        "documents_no_results",
        _chat("openrag-legal", QUESTION),
        chunks=(),
        script=Script(answers=[{"content": "I could not find it in the documents."}]),
    ),
    Scenario(
        "documents_all_partitions",
        _chat("openrag-all", QUESTION),
        chunks=(*DEFAULT_CHUNKS, _chunk("201", "policy", "HR policy: notice is given in writing.", partition="hr")),
    ),
    Scenario("workspace", _chat("openrag-legal", QUESTION, metadata={"workspace": "ws-1"})),
    Scenario("workspace_not_found", _chat("openrag-legal", QUESTION, metadata={"workspace": "ws-unknown"})),
    Scenario(
        "attachments",
        _chat("openrag-legal", QUESTION, metadata={"attachments": [{"id": "contract"}, {"id": "missing"}]}),
    ),
    Scenario(
        "workspace_and_attachments",
        _chat("openrag-legal", QUESTION, metadata={"workspace": "ws-1", "attachments": [{"id": "handbook"}]}),
        script=Script(answers=[{"content": "Two weeks on probation. [Sources: 1]"}]),
    ),
    Scenario(
        "map_reduce",
        _chat("openrag-legal", QUESTION, metadata={"use_map_reduce": True}),
        chunks=(
            *DEFAULT_CHUNKS[:2],
            _chunk("103", "minutes", "IRRELEVANT: the board approved the 2026 budget."),
        ),
    ),
    Scenario(
        "small_talk",
        _chat("openrag-legal", _user("hello")),
        script=Script(answers=[{"content": "Hello! How can I help?"}]),
    ),
    Scenario(
        "gratitude",
        _chat("openrag-legal", QUESTION, _assistant("Three months."), _user("thanks")),
        script=Script(answers=[{"content": "You're welcome!"}]),
    ),
    Scenario(
        "acknowledgement",
        _chat("openrag-legal", QUESTION, _assistant("Three months."), _user("ok")),
        script=Script(
            contextualizer=[_queries(intent="gratitude", requires_retrieval=False)],
            answers=[{"content": "Glad it helps."}],
        ),
    ),
    Scenario(
        "acknowledgement_accepting_an_offer",
        _chat(
            "openrag-legal",
            QUESTION,
            _assistant("Three months. Do you want the probation rules too?"),
            _user("yes"),
        ),
        script=Script(contextualizer=[_queries("notice period during probation")]),
    ),
    Scenario(
        "small_talk_require_retrieval",
        _chat("openrag-legal", _user("hello"), metadata={"require_retrieval": True}),
        script=Script(contextualizer=[_queries(intent="greeting", requires_retrieval=False)]),
    ),
    Scenario(
        "bypass_contextualization",
        _chat(
            "openrag-legal",
            _user("Who approved the budget?"),
            _assistant("The board did."),
            _user("And the notice period?"),
            metadata={
                "bypass_query_contextualization": True,
                "include_retrieval_trace": True,
                "require_retrieval": True,
            },
        ),
        user=ADMIN,
    ),
    Scenario(
        "retrieval_trace",
        _chat("openrag-legal", QUESTION, metadata={"include_retrieval_trace": True}),
        user=ADMIN,
    ),
    Scenario(
        "retrieval_trace_requires_admin",
        _chat("openrag-legal", QUESTION, metadata={"include_retrieval_trace": True}),
    ),
    Scenario(
        "contextualizer_unparsable",
        _chat("openrag-legal", QUESTION),
        script=Script(contextualizer=["not json"]),
    ),
    Scenario(
        "spoken_style_answer",
        _chat("openrag-legal", QUESTION, metadata={"spoken_style_answer": True}),
        script=Script(answers=[{"content": "The notice period is three months."}]),
    ),
    Scenario(
        "structured_output",
        _chat("openrag-legal", QUESTION, response_format={"type": "json_object"}),
        script=Script(answers=[{"content": '{"notice_period": "three months"}'}]),
    ),
    Scenario(
        "client_system_prompt_with_documents",
        _chat("openrag-legal", {"role": "system", "content": "Answer in one sentence."}, QUESTION),
    ),
    Scenario(
        "history_window",
        _chat(
            "openrag-legal",
            _user("First question?"),
            _assistant("First answer."),
            _user("Second question?"),
            _assistant("Second answer."),
            _user("Third question?"),
            _assistant(""),
            QUESTION,
        ),
    ),
    Scenario(
        "retrieval_options",
        _chat(
            "openrag-legal",
            QUESTION,
            metadata={
                "include_retrieval_trace": True,
                "require_retrieval": True,
                "retrieval_top_k": 7,
                "retrieval_similarity_threshold": 0.4,
                "retrieval_disable_reranker": True,
                "retrieval_disable_expansion": True,
            },
        ),
        user=ADMIN,
    ),
    Scenario(
        "retrieval_options_require_the_trace",
        _chat("openrag-legal", QUESTION, metadata={"retrieval_top_k": 7}),
    ),
    Scenario(
        "include_all_retrieved_sources",
        _chat("openrag-legal", QUESTION, metadata={"include_all_retrieved_sources": True}),
        top_n=2,
    ),
    Scenario(
        "answer_without_citation_tag",
        _chat("openrag-legal", QUESTION),
        script=Script(answers=[{"content": "The notice period is three months."}]),
    ),
    Scenario(
        "inline_source_markers",
        _chat("openrag-legal", QUESTION),
        script=Script(answers=[{"content": "Three months [Source 1], two weeks on probation [Source 2]."}]),
    ),
    Scenario(
        "empty_answer_retried",
        _chat("openrag-legal", QUESTION),
        script=Script(answers=[{"content": "[Sources: 1]"}, {"content": "Three months. [Sources: 1]"}]),
    ),
    Scenario(
        "sources_trimmed_to_window",
        _chat("openrag-legal", QUESTION, max_tokens=1000),
        window=1665,
    ),
    Scenario(
        "partition_chat_llm",
        _chat("openrag-hr", QUESTION),
        chunks=(_chunk("201", "policy", "HR policy: notice is given in writing.", partition="hr"),),
        hr_chat_llm="hr-llm",
    ),
    # ---- text completions -------------------------------------------------
    Scenario("completion_documents", {"model": "openrag-legal", "prompt": "Notice period?"}, path="/v1/completions"),
    Scenario(
        "completion_direct",
        {"prompt": "Notice period?"},
        path="/v1/completions",
        script=Script(completion="Three months."),
    ),
]


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def _settings(scenario: Scenario) -> Settings:
    base = Settings()
    settings = base.model_copy(
        update={
            "llm": base.llm.model_copy(update={"model": "direct-llm"}),
            "rag": base.rag.model_copy(update={"mode": scenario.rag_mode, "chat_history_depth": 4}),
            "reranker": base.reranker.model_copy(update={"top_k": 5}),
            "map_reduce": base.map_reduce.model_copy(
                update={"initial_batch_size": 2, "expansion_batch_size": 1, "max_total_documents": 4}
            ),
        }
    )
    settings.models.llm["default"] = ModelEndpointConfig(
        endpoint="http://llm:8000/v1", extra={LLM_CONTEXT_SIZE_KEY: scenario.window}
    )
    if scenario.hr_chat_llm:
        settings.models.llm[scenario.hr_chat_llm] = ModelEndpointConfig(
            endpoint="http://hr-llm:8000/v1", extra={LLM_CONTEXT_SIZE_KEY: scenario.window}
        )
    settings.partitions.update(
        {
            name: PartitionConfig(
                name=name,
                indexation=IndexationPipelineConfig(),
                retrieval=RetrievalPipelineConfig(top_n=scenario.top_n),
                chat_llm=scenario.hr_chat_llm if name == "hr" else None,
            )
            for name in PARTITIONS
        }
    )
    return settings


def _build_chat_service(settings: Settings, scenario: Scenario, calls: dict[str, list]) -> ChatService:
    """The chat service under test, wired to the fakes. Follows the refactor."""
    llms = {name: FakeLLM(name, calls["llm"], scenario.script) for name in ("static", "default", scenario.hr_chat_llm)}

    def llm_factory(name: str):
        if name not in llms:
            raise KeyError(name)
        return llms[name]

    return create_chat_service(
        retrieval_service=FakeRetrieval(calls["retrieval"], list(scenario.chunks)),
        llm=llms["static"],
        config=settings,
        web_search_service=FakeWeb(calls["web"], list(scenario.web_results)),
        workspace_service=FakeWorkspaces(),
        prompt_service=PromptService(prompt_repo=_EmptyPromptRepo(), config=settings),
        document_repo=_EmptyCatalog(),
        llm_factory=llm_factory,
    )


def _app(settings: Settings, service: ChatService, user: dict) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(chat.router, prefix="/v1")

    # Named routes the source links are built from.
    @app.get("/static/{extract_id}", name="download_source")
    async def _download(extract_id: str): ...

    @app.get("/extract/{extract_id}", name="get_extract")
    async def _extract(extract_id: str): ...

    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[current_user_or_admin_partitions_list] = lambda: list(PARTITIONS)
    app.dependency_overrides[get_chat_service] = lambda: service
    app.dependency_overrides[get_partition_service] = FakePartitions
    app.dependency_overrides[get_config] = lambda: settings
    app.dependency_overrides[get_retrieval_diagnostics_guard] = lambda: RetrievalDiagnosticsGuard("1000/minute")
    return app


def _response_body(response) -> Any:
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        events = []
        for line in response.text.splitlines():
            if not line.startswith("data: "):
                continue
            data = line[len("data: ") :]
            events.append(data if data == "[DONE]" else json.loads(data))
        return {"events": events}
    return response.json()


_VOLATILE_KEYS = {"request_id": "<request-id>", "duration_seconds": "<duration>"}


def _mask_volatile(value: Any) -> Any:
    """Mask what changes on every run: the retrieval trace's request id and timings."""
    if isinstance(value, dict):
        return {
            key: (
                _VOLATILE_KEYS[key]
                if key in _VOLATILE_KEYS and item is not None
                else dict.fromkeys(item, "<duration>")
                if key == "timings" and isinstance(item, dict)
                else _mask_volatile(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_volatile(item) for item in value]
    return value


def _check_snapshot(name: str, actual: dict) -> None:
    path = SNAPSHOT_DIR / f"{name}.json"
    rendered = json.dumps(actual, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if UPDATE_SNAPSHOTS or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        if not UPDATE_SNAPSHOTS:
            pytest.fail(f"Snapshot {path.name} was missing and has been written; check it and rerun.")
        return
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert json.loads(rendered) == expected, f"{path.name} changed; rerun with UPDATE_CHAT_SNAPSHOTS=1 if intended"


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
async def test_chat_characterization(scenario: Scenario, async_client_factory):
    calls: dict[str, list] = {"llm": [], "retrieval": [], "web": []}
    settings = _settings(scenario)
    service = _build_chat_service(settings, scenario, calls)
    async with async_client_factory(_app(settings, service, scenario.user)) as client:
        response = await client.post(scenario.path, json=copy.deepcopy(scenario.body))

    _check_snapshot(
        scenario.name,
        {
            "request": {"path": scenario.path, "body": scenario.body, "rag_mode": scenario.rag_mode},
            "status": response.status_code,
            "llm_calls": calls["llm"],
            "retrieval_calls": calls["retrieval"],
            "web_calls": calls["web"],
            "response": _mask_volatile(_response_body(response)),
        },
    )


def test_scenario_names_are_unique():
    names = [scenario.name for scenario in SCENARIOS]
    assert len(names) == len(set(names))


def test_no_orphan_snapshots():
    """A snapshot without a scenario would never be checked again."""
    names = {scenario.name for scenario in SCENARIOS}
    orphans = sorted(p.stem for p in SNAPSHOT_DIR.glob("*.json") if p.stem not in names)
    assert orphans == []
