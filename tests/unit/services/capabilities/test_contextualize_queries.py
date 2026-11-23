"""The query contextualizer: the conversation rewritten into search queries."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from core.config import load_config
from services.capabilities.contextualize import QueryContextualizer
from services.orchestrators.prompt_service import PromptService

_PROMPT_CFG = load_config()
_NOW = datetime(2026, 9, 16, 9, 30, tzinfo=UTC)


class _EmptyPromptRepo:
    """No DB rows: the prompts come from the disk seeds."""

    async def get_by_name(self, prompt_type, name):
        return None

    async def get_default(self, prompt_type):
        return None


class FakeLLM:
    def __init__(self, *, chat_responses=None):
        self._chat_responses = list(chat_responses or [])
        self.chat_calls: list = []

    async def chat(self, messages, **kwargs):
        self.chat_calls.append((messages, kwargs))
        content = self._chat_responses.pop(0) if self._chat_responses else ""
        return {"choices": [{"message": {"content": content}}]}


def _contextualizer(*, llm=None, mode="SimpleRag", prompt_service=None, partitions=None, clock=None):
    config = SimpleNamespace(
        rag=SimpleNamespace(mode=mode, max_contextualized_query_len=512),
        partitions=partitions or {},
        paths=_PROMPT_CFG.paths,
        prompts=_PROMPT_CFG.prompts,
    )
    return QueryContextualizer(
        config=config,
        prompt_service=prompt_service or PromptService(prompt_repo=_EmptyPromptRepo(), config=_PROMPT_CFG),
        llm=llm or FakeLLM(),
        detect_language=lambda _text, **_kwargs: "en",
        clock=clock or (lambda: _NOW),
    )


@pytest.mark.parametrize(("mode", "rewrites"), [("SimpleRag", False), ("ChatBotRag", True), ("other", False)])
def test_only_chatbotrag_rewrites_the_queries(mode, rewrites):
    assert _contextualizer(mode=mode).rewrites_queries is rewrites


@pytest.mark.asyncio
async def test_the_dates_come_from_the_clock_it_is_given():
    """``create_chat_service`` gives it the UTC clock (tested in test_chat_service); the anchors and the
    current date the model copies are that clock's, with the ``+00:00`` the
    Milvus filter requires."""
    payload = json.dumps({"requires_retrieval": True, "query_list": [{"query": "q", "temporal_filters": None}]})
    llm = FakeLLM(chat_responses=[payload])
    ctx = _contextualizer(llm=llm, mode="ChatBotRag", clock=lambda: datetime(2026, 9, 16, 23, 30, tzinfo=UTC))

    await ctx.contextualize([{"role": "user", "content": "what did I receive today?"}])

    system = llm.chat_calls[0][0][0]["content"]
    assert "Current date: Wednesday, September 16, 2026, 23:30:00" in system
    assert "- today 2026-09-16T00:00:00+00:00, tomorrow 2026-09-17T00:00:00+00:00" in system
    assert "- last week [2026-09-07, 2026-09-14)" in system


_TOOL_TURN = [
    {"role": "user", "content": "Reply to the last email of Claire"},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function"}]},
    {"role": "tool", "tool_call_id": "c1", "content": "2 emails: ..."},
]


@pytest.mark.asyncio
async def test_simplerag_uses_last_message():
    sq = await _contextualizer(mode="SimpleRag").contextualize([{"role": "user", "content": "what is X?"}])
    assert [q.query for q in sq.query_list] == ["what is X?"]


@pytest.mark.asyncio
async def test_chatbotrag_parses_json():
    payload = json.dumps({"query_list": [{"query": "rewritten", "temporal_filters": None}]})
    ctx = _contextualizer(llm=FakeLLM(chat_responses=[payload]), mode="ChatBotRag")
    sq = await ctx.contextualize([{"role": "user", "content": "hi"}])
    assert sq.query_list[0].query == "rewritten"
    assert sq.requires_retrieval is True


@pytest.mark.asyncio
async def test_chatbotrag_can_skip_retrieval():
    payload = json.dumps({"intent": "capability", "requires_retrieval": False, "query_list": []})
    ctx = _contextualizer(llm=FakeLLM(chat_responses=[payload]), mode="ChatBotRag")
    sq = await ctx.contextualize([{"role": "user", "content": "How can you help me?"}])
    assert sq.intent == "capability"
    assert sq.requires_retrieval is False
    assert sq.query_list == []


@pytest.mark.asyncio
async def test_contextualizer_prompt_distinguishes_social_acknowledgement_from_factual_acceptance():
    payload = json.dumps({"intent": "gratitude", "requires_retrieval": False, "query_list": []})
    llm = FakeLLM(chat_responses=[payload])
    ctx = _contextualizer(llm=llm, mode="ChatBotRag")

    await ctx.contextualize(
        [
            {"role": "assistant", "content": "Was that helpful?"},
            {"role": "user", "content": "ok"},
        ]
    )

    contextualizer_prompt = llm.chat_calls[0][0][0]["content"]
    assert "Replies to social, wellbeing, or feedback questions remain gratitude" in contextualizer_prompt
    assert (
        'assistant: Was that helpful?\nuser: ok\n{"intent":"gratitude","requires_retrieval":false,"query_list":[]}'
    ) in contextualizer_prompt
    assert (
        "assistant: Albendazole treats lymphatic filariasis and several worm infections. "
        "Would you like the recommended dosages?\nuser: ok\n"
        '{"intent":"other","requires_retrieval":true,'
    ) in contextualizer_prompt


@pytest.mark.asyncio
async def test_query_hint_preserves_acknowledgement_rules_with_a_stored_contextualizer():
    class StoredPromptService:
        async def resolve_prompt(self, prompt_type, names=None):
            assert prompt_type == "query_contextualizer"
            return "Stored contextualizer for {query_language} on {current_date}."

    payload = json.dumps({"intent": "gratitude", "requires_retrieval": False, "query_list": []})
    llm = FakeLLM(chat_responses=[payload])
    ctx = _contextualizer(llm=llm, mode="ChatBotRag", prompt_service=StoredPromptService())

    await ctx.contextualize(
        [
            {"role": "assistant", "content": "Was that helpful?"},
            {"role": "user", "content": "ok"},
        ]
    )

    contextualizer_prompt = llm.chat_calls[0][0][0]["content"]
    assert "social, wellbeing, or feedback question" in contextualizer_prompt
    assert "factual, informational, analytical, or document-backed continuation" in contextualizer_prompt


@pytest.mark.asyncio
async def test_chatbotrag_falls_back_on_garbage():
    ctx = _contextualizer(llm=FakeLLM(chat_responses=["not json", "still not json"]), mode="ChatBotRag")
    sq = await ctx.contextualize([{"role": "user", "content": "raw question"}])
    assert sq.query_list[0].query == "raw question"  # fallback to raw user query


@pytest.mark.asyncio
async def test_renders_history_with_a_content_free_turn():
    """The chat-history string is built over *every* message, so the assistant
    turn that carries ``tool_calls`` — no ``content`` at all once the router
    dumps with ``exclude_none=True`` — reaches it too. ChatBotRag is the default
    ``rag.mode``, so the SimpleRag early return is no shield: reading the key
    unguarded 500s on a plain tool-call replay.
    """
    llm = FakeLLM(chat_responses=['{"requires_retrieval": true, "query_list": [{"query": "q"}]}'])
    ctx = _contextualizer(llm=llm, mode="ChatBotRag")

    sq = await ctx.contextualize(
        [
            {"role": "user", "content": "weather in Paris?"},
            {"role": "assistant", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "content": "18C", "tool_call_id": "c1"},
            {"role": "user", "content": "and tomorrow?"},
        ]
    )

    assert sq.query_list[0].query == "q"
    # The turn is still rendered (role kept, empty body) rather than skipped, so
    # the history handed to the contextualizer keeps its shape.
    history = llm.chat_calls[0][0][1]["content"]
    assert "assistant: \n" in history


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
    rec = RecordingPromptService()
    ctx = _contextualizer(
        llm=FakeLLM(chat_responses=[payload, payload]),
        mode="ChatBotRag",
        prompt_service=rec,
        partitions={"p": SimpleNamespace(retrieval=SimpleNamespace(query_contextualizer_prompt_name="myctx"))},
    )

    await ctx.contextualize([{"role": "user", "content": "q"}], partitions=["p"])
    assert ("query_contextualizer", ("myctx",)) in rec.calls

    rec.calls.clear()
    await ctx.contextualize([{"role": "user", "content": "q"}], partitions=["p", "q"])
    assert ("query_contextualizer", (None,)) in rec.calls  # no single owning partition


@pytest.mark.asyncio
async def test_hands_the_contextualizer_precomputed_calendar_anchors():
    """ "Last week" must reach Milvus as Monday-to-Monday. Mistral Small resolved
    it to the past seven days when left to do the arithmetic, so the system
    prompt now carries the boundaries pre-computed through the template's
    ``{calendar_anchors}`` placeholder.
    """
    payload = json.dumps({"requires_retrieval": True, "query_list": [{"query": "q", "temporal_filters": None}]})
    llm = FakeLLM(chat_responses=[payload])
    ctx = _contextualizer(llm=llm, mode="ChatBotRag")

    await ctx.contextualize([{"role": "user", "content": "résume mes échanges de la semaine dernière"}])

    system = llm.chat_calls[0][0][0]
    assert system["role"] == "system"
    assert "Calendar anchors (use verbatim, do not recompute)" in system["content"]
    assert "- last week [" in system["content"]
    # The bundled template points its resolution rules at those anchors.
    assert "copy the matching anchor under Current date verbatim" in system["content"]


@pytest.mark.asyncio
async def test_searches_the_request_of_the_user_not_the_tool_result():
    ctx = _contextualizer(mode="SimpleRag")
    sq = await ctx.contextualize(_TOOL_TURN)
    assert sq.query_list[0].query == "Reply to the last email of Claire"


@pytest.mark.asyncio
async def test_leaves_the_tool_results_out_of_the_history():
    llm = FakeLLM(chat_responses=['{"requires_retrieval": true, "query_list": [{"query": "q"}]}'])
    ctx = _contextualizer(llm=llm, mode="ChatBotRag")
    await ctx.contextualize(_TOOL_TURN)
    history = llm.chat_calls[0][0][1]["content"]
    assert "2 emails" not in history, "the data the app gave do not shape the search"
    assert "user: Reply to the last email of Claire" in history


@pytest.mark.asyncio
async def test_keeps_the_contextualizers_date_order():
    payload = json.dumps(
        {
            "intent": "other",
            "requires_retrieval": True,
            "query_list": [{"query": "meeting notes", "temporal_filters": None, "date_order": "newest"}],
        }
    )
    ctx = _contextualizer(llm=FakeLLM(chat_responses=[payload]), mode="ChatBotRag")
    out = await ctx.contextualize([{"role": "user", "content": "résume le dernier meeting que j'ai eu"}])
    assert out.query_list[0].date_order == "newest"
    assert out.date_order == "newest"
