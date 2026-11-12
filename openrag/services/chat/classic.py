"""What each node of the ``classic`` graph does.

The graph itself (its edges and the compile step) is
``services.chat.graphs.classic``, the only module that imports LangGraph. The
steps here are plain async methods: the request's state and context in, the
keys they change out, so they run and are tested without LangGraph.

Grading only happens in map-reduce (``summarize``), when the request sets
``use_map_reduce``; without it, the graph runs exactly the pipeline
``ChatService._prepare_chat`` ran before it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from core.chat.budget import fit_prompt
from core.chat.history import sanitize_messages
from core.chat.smalltalk import CasualMessagePolicy, acknowledgement_language, classify_latest_message
from core.models.query import Query, SearchQueries
from core.models.retrieval_trace import ContextualizationTrace
from core.prompts import (
    SOURCE_SEPARATOR,
    build_casual_response_prompt,
    documents_context,
    format_web_context,
    message_tokens,
    prepend_system_prompt,
    tool_definition_tokens,
)
from core.retrieval.date_order import order_chunks_by_date
from core.retrieval.trace import canonical_fingerprint
from core.utils.exceptions import ContextWindowExceededError
from core.utils.text import get_num_tokens
from services.capabilities.contextualize import mark_fallback, record_bypass

if TYPE_CHECKING:
    from core.chat.routing import Route
    from core.chat.scope import Scope
    from core.chat.state import ClassicState
    from core.config.root import Settings
    from core.llm.llm import LLM
    from core.models.chat import ChatOptions
    from core.retrieval.trace import RetrievalTraceBuilder
    from services.capabilities.contextualize import QueryContextualizer
    from services.capabilities.retrieve import SourceRetriever
    from services.capabilities.summarize import Summarizer
    from services.capabilities.web_search import WebSearch
    from services.orchestrators.prompt_service import PromptService
    from services.orchestrators.retrieval_service import RetrievalService

_DATE_FORMAT = "%A, %B %d, %Y, %H:%M:%S"


@dataclass(frozen=True)
class ChatContext:
    """What a chat request runs with besides its state, settled before the graph starts.

    *payload* is the request as the client sent it; the steps only read its
    tool definitions. *max_prompt_tokens* is what the answering model's window
    leaves for the prompt, ``None`` when the window is unknown.
    """

    options: ChatOptions
    route: Route
    scope: Scope
    llm: LLM
    payload: dict
    trace: RetrievalTraceBuilder | None = None
    max_prompt_tokens: int | None = None


def messages_tokens(messages: list[dict]) -> int:
    """Tokens a chat message list takes, counted as the router's preflight does."""
    length_function = get_num_tokens()
    return sum(message_tokens(message, length_function) for message in messages)


class ClassicSteps:
    """The bodies of the ``classic`` graph's nodes.

    *fit_sources* and *generation_prompt_name* are ``ChatService``'s, shared
    with the completions endpoint; *clock* gives the date the prompts show and
    *detect_language* the language of an acknowledgement.
    """

    def __init__(
        self,
        *,
        config: Settings,
        retrieval_service: RetrievalService,
        prompt_service: PromptService,
        contextualizer: QueryContextualizer,
        retriever: SourceRetriever,
        web_search: WebSearch,
        summarizer: Summarizer,
        fit_sources: Callable[[list, list[str] | None, int | None], list[int]],
        generation_prompt_name: Callable[[str, list[str] | None], str | None],
        clock: Callable[[], datetime],
        detect_language: Callable[..., str | None],
    ) -> None:
        # Live reference: the partition configs are refreshed on every preset change.
        self._config = config
        self._retrieval = retrieval_service
        self._prompt_service = prompt_service
        self._contextualizer = contextualizer
        self._retriever = retriever
        self._web = web_search
        self._summarizer = summarizer
        self._fit_sources = fit_sources
        self._generation_prompt_name = generation_prompt_name
        self._clock = clock
        self._detect_language = detect_language

    def _retrieval_settings(self, options: ChatOptions) -> tuple[dict[str, Any], Any, int | None, dict[str, Any]]:
        """The request's retrieval options, its ``retrieval_top_k``, the chunk cap and the other overrides."""
        effective = options.retrieval.as_options()
        top_k = self._summarizer.max_total_documents if options.use_map_reduce else None
        overrides = {key: value for key, value in effective.items() if key != "top_k"}
        return effective, effective.get("top_k"), top_k, overrides

    # ------------------------------------------------------------------
    # route → contextualize → trace_config
    # ------------------------------------------------------------------

    async def route(self, state: ClassicState, ctx: ChatContext) -> dict[str, Any]:
        """Whether the last message is small talk, and whether the request forces a search."""
        casual, acknowledgement_candidate = classify_latest_message(
            state["messages"], state["last_user_message"], query_rewrite=self._contextualizer.rewrites_queries
        )
        options = ctx.options
        retrieval_forced = (
            options.require_retrieval or options.websearch or options.use_map_reduce or bool(ctx.scope.attachment_ids)
        )
        return {
            "casual": casual,
            "acknowledgement_candidate": acknowledgement_candidate,
            "retrieval_forced": retrieval_forced,
        }

    @staticmethod
    def needs_queries(state: ClassicState, ctx: ChatContext) -> bool:
        """Whether ``contextualize`` runs: everything but plain small talk needs queries."""
        return (
            ctx.options.bypass_query_contextualization
            or state.get("casual") is None
            or bool(state.get("retrieval_forced"))
        )

    async def contextualize(self, state: ClassicState, ctx: ChatContext) -> dict[str, Any]:
        """The queries to search: the last message as it is on bypass, else the contextualizer's.

        A short acknowledgement ``route`` could not settle becomes small talk
        here when the contextualizer reads it as thanks that need no search.
        """
        last_user_message = state["last_user_message"]
        trace = ctx.trace
        if ctx.options.bypass_query_contextualization:
            record_bypass(trace, original_query=last_user_message)
            return {"queries": SearchQueries(query_list=[Query(query=last_user_message)])}

        messages = state["messages"]
        queries = await self._contextualizer.contextualize(
            messages, llm=ctx.llm, partitions=ctx.scope.partitions, trace=trace
        )
        casual = state.get("casual")
        usable_queries = [query for query in queries.query_list if query.query.strip()]
        if (
            state.get("acknowledgement_candidate")
            and not state.get("retrieval_forced")
            and queries.intent == "gratitude"
            and not queries.requires_retrieval
            and not usable_queries
        ):
            casual = CasualMessagePolicy(
                "acknowledgement", acknowledgement_language(messages, last_user_message, self._detect_language)
            )
        elif not usable_queries:
            queries = SearchQueries(query_list=[Query(query=last_user_message)])
            mark_fallback(trace, queries)
        elif len(usable_queries) != len(queries.query_list):
            queries = queries.model_copy(update={"query_list": usable_queries})
        return {"queries": queries, "casual": casual}

    async def trace_config(self, state: ClassicState, ctx: ChatContext) -> dict[str, Any]:
        """With a trace: the retrieval plan and the configuration fingerprint it records."""
        trace = ctx.trace
        if trace is None:
            return {"resolved_plan": None, "configuration_fingerprint": "unavailable"}
        partitions = ctx.scope.partitions
        effective, retrieval_top_k, _top_k, overrides = self._retrieval_settings(ctx.options)
        contextualizer_prompt = trace.contextualization.prompt if trace.contextualization is not None else None
        resolved_plan = None
        resolve_plan = getattr(self._retrieval, "resolve_retrieval_plan", None)
        if partitions is not None and resolve_plan is not None:
            try:
                resolved_plan = await resolve_plan(
                    partitions,
                    top_k=retrieval_top_k,
                    contextualizer_prompt=contextualizer_prompt,
                    **overrides,
                )
            except Exception as error:  # noqa: BLE001 - legacy resolution below keeps telemetry optional
                trace.record_error("configuration_resolution", error)
        fingerprint = await self._configuration_fingerprint(
            partitions,
            effective_options=effective,
            contextualizer_prompt=contextualizer_prompt,
            resolved_plan=resolved_plan,
        )
        return {"resolved_plan": resolved_plan, "configuration_fingerprint": fingerprint}

    async def _configuration_fingerprint(
        self,
        partitions: list[str] | None,
        *,
        effective_options: dict[str, object] | None = None,
        contextualizer_prompt: Any = None,
        resolved_plan: Any = None,
    ) -> str:
        if not partitions:
            return "unavailable"
        if resolved_plan is not None:
            return str(resolved_plan.configuration_fingerprint)
        resolved_fingerprint = getattr(self._retrieval, "resolved_configuration_fingerprint", None)
        fingerprint = getattr(self._retrieval, "configuration_fingerprint", None)
        if resolved_fingerprint is None and fingerprint is None:
            return "unavailable"
        try:
            if resolved_fingerprint is not None:
                stored_fingerprint = str(
                    await resolved_fingerprint(
                        partitions,
                        contextualizer_prompt=contextualizer_prompt,
                    )
                )
            else:
                stored_fingerprint = str(fingerprint(partitions))
            if not effective_options:
                return stored_fingerprint
            return canonical_fingerprint(
                {
                    "stored_configuration_fingerprint": stored_fingerprint,
                    "effective_request_overrides": effective_options,
                }
            )
        except Exception:  # noqa: BLE001 - telemetry must never fail a request
            return "unavailable"

    @staticmethod
    def is_small_talk(state: ClassicState) -> bool:
        """Whether the request is answered as small talk, without a search."""
        return state.get("casual") is not None and not state.get("retrieval_forced")

    # ------------------------------------------------------------------
    # small_talk
    # ------------------------------------------------------------------

    async def small_talk(self, state: ClassicState, ctx: ChatContext) -> dict[str, Any]:
        """The casual answer's prompt; 413 when the conversation doesn't fit the window."""
        casual = state["casual"]
        trace = ctx.trace
        # The history as the model gets it, so the window is checked against what is sent.
        messages = sanitize_messages(state["messages"])
        if trace is not None and trace.contextualization is None:
            trace.contextualization = ContextualizationTrace(
                original_query=state["last_user_message"],
                subqueries=[],
                intent=casual.intent,
                requires_retrieval=False,
            )
            trace.record_stage("original_query", status="complete", candidates=[])
            trace.record_stage("contextualized_query", status="not_run", candidates=[])
        casual_prompt = build_casual_response_prompt(
            casual.intent, casual.language, assistant_name=self._config.server.assistant_name
        )
        answer_messages = prepend_system_prompt(
            messages,
            casual_prompt,
            context="",
            current_date=self._clock().strftime(_DATE_FORMAT),
            custom_prompt=state.get("custom_prompt"),
        )
        if ctx.max_prompt_tokens is not None:
            sent = messages_tokens(answer_messages) + tool_definition_tokens(ctx.payload, get_num_tokens())
            if sent > ctx.max_prompt_tokens:
                raise ContextWindowExceededError(sent, ctx.max_prompt_tokens)
        return {"answer_messages": answer_messages, "citation_protocol_active": True, "retry_empty_response": True}

    # ------------------------------------------------------------------
    # retrieve → budget → (summarize) → prompt
    # ------------------------------------------------------------------

    async def retrieve(self, state: ClassicState, ctx: ChatContext) -> dict[str, Any]:
        """The documents and web results for the queries, and the original-query comparison when asked."""
        queries = state.get("queries")
        if queries is None:  # pragma: no cover - contextualize sets them whenever the graph reaches here
            queries = SearchQueries(query_list=[Query(query=state["last_user_message"])])
        _effective, retrieval_top_k, top_k, overrides = self._retrieval_settings(ctx.options)
        trace = ctx.trace
        resolved_plan = state.get("resolved_plan")
        chunks, web_results = await self._retriever.retrieve(
            queries,
            route=ctx.route,
            scope=ctx.scope,
            top_k=top_k,
            retrieval_top_k=retrieval_top_k,
            overrides=overrides,
            trace=trace,
            resolved_plan=resolved_plan,
        )
        if trace is not None and ctx.route.has_documents and ctx.options.compare_original_query:
            await self._retriever.compare_original_query(
                trace,
                original_query=state["last_user_message"],
                scope=ctx.scope,
                top_k=top_k,
                retrieval_top_k=retrieval_top_k,
                overrides=overrides,
                configuration_fingerprint=state.get("configuration_fingerprint", "unavailable"),
                resolved_plan=resolved_plan,
            )
        return {"queries": queries, "chunks": chunks, "web_results": web_results}

    @staticmethod
    def found_nothing_on_the_web(state: ClassicState, ctx: ChatContext) -> bool:
        """A web-only request whose search found nothing: the model answers without sources."""
        return not state.get("chunks") and not state.get("web_results") and not ctx.route.has_documents

    async def budget(self, state: ClassicState, ctx: ChatContext) -> dict[str, Any]:
        """The answer template and the room the window leaves for sources.

        A conversation the window can't hold fails here (413), before map-reduce
        spends any LLM call, rather than at the provider.
        """
        messages = sanitize_messages(state["messages"])
        partitions = ctx.scope.partitions
        docs = [chunk.to_langchain() for chunk in state["chunks"]]
        prompt_type = "spoken_style_answer" if ctx.options.spoken_style_answer else "sys_prompt"
        template = await self._prompt_service.resolve_prompt(
            prompt_type, names=[self._generation_prompt_name(prompt_type, partitions)]
        )
        current_date = self._clock().strftime(_DATE_FORMAT)
        custom_prompt = state.get("custom_prompt")
        # What the window leaves for sources once the instructions, the
        # conversation and the client's tool definitions are in.
        room = None
        tools_tokens = tool_definition_tokens(ctx.payload, get_num_tokens())
        if ctx.max_prompt_tokens is not None:
            without_sources = prepend_system_prompt(
                messages, template, context="", current_date=current_date, custom_prompt=custom_prompt
            )
            room = ctx.max_prompt_tokens - messages_tokens(without_sources) - tools_tokens
            if room < 0:
                raise ContextWindowExceededError(ctx.max_prompt_tokens - room, ctx.max_prompt_tokens)
        return {
            "history": messages,
            "template": template,
            "current_date": current_date,
            "room": room,
            "tools_tokens": tools_tokens,
            # The full retrieval set, captured before map-reduce replaces the
            # documents with summaries and before the fit drops what doesn't
            # fit: `all_retrieved_sources` reports it, while `docs` matches what
            # the model sees and the citation numbers it cites.
            "retrieved_docs": docs,
            "retrieved_web_results": state.get("web_results", []),
            "docs": docs,
        }

    @staticmethod
    def map_reduce_requested(state: ClassicState, ctx: ChatContext) -> bool:
        """Whether ``summarize`` runs: the request asks for map-reduce and there is something to summarize."""
        return bool(ctx.options.use_map_reduce and state.get("docs"))

    async def summarize(self, state: ClassicState, ctx: ChatContext) -> dict[str, Any]:
        """Map-reduce: the relevant documents, each replaced by its summary. The only grading."""
        queries = state["queries"]
        docs = await self._summarizer.summarize(" ".join(q.query for q in queries.query_list), state["docs"])
        return {"docs": docs}

    async def prompt(self, state: ClassicState, ctx: ChatContext) -> dict[str, Any]:
        """The answer messages, with as many sources as the window takes."""
        queries = state["queries"]
        messages = state["history"]
        template = state["template"]
        current_date = state["current_date"]
        custom_prompt = state.get("custom_prompt")
        room = state.get("room")
        tools_tokens = state["tools_tokens"]
        web_results = state.get("web_results", [])
        docs = state["docs"]

        # Web results come out of the same room, so they can't take more than it either.
        web_max_tokens = self._web.max_tokens if room is None else min(self._web.max_tokens, room)
        web_tokens = 0
        if web_results:
            _formatted, _numbers, web_tokens = format_web_context(
                web_results,
                length_function=get_num_tokens(),
                start_index=1,
                max_tokens=web_max_tokens,
            )
        headroom = None if room is None else room - web_tokens
        candidates = docs
        docs = [docs[i] for i in self._fit_sources(docs, ctx.scope.partitions, headroom)]

        def assemble(docs: list, web_candidates: list) -> tuple[list[dict], list]:
            """The answer messages with *docs*, then the *web_candidates* that fit after them."""
            context = documents_context(
                order_chunks_by_date(docs, queries.date_order),
                any_retrieved=bool(candidates),
                length_function=get_num_tokens(),
            )
            web_shown: list = []
            if web_candidates:
                start = len(docs) + 1
                web_formatted, numbers, _ = format_web_context(
                    web_candidates, length_function=get_num_tokens(), start_index=start, max_tokens=web_max_tokens
                )
                web_shown = [web_candidates[n - start] for n in numbers]
                context = f"{context}{SOURCE_SEPARATOR}{web_formatted}" if docs else web_formatted
            answer_messages = prepend_system_prompt(
                messages, template, context=context, current_date=current_date, custom_prompt=custom_prompt
            )
            return answer_messages, web_shown

        # The sources were sized part by part, without the separators between
        # documents and web results or the web results' final numbering: measure
        # what is sent, and drop sources (documents first) until it fits.
        answer_messages, docs, web_results = fit_prompt(
            assemble,
            docs,
            web_results,
            measure=lambda answer: messages_tokens(answer) + tools_tokens,
            max_tokens=ctx.max_prompt_tokens,
        )
        return {
            "answer_messages": answer_messages,
            # The order they were rendered in: citation numbers index into it.
            "docs": order_chunks_by_date(docs, queries.date_order),
            "web_results": web_results,
            "citation_protocol_active": True,
        }


__all__ = ["ChatContext", "ClassicSteps", "messages_tokens"]
