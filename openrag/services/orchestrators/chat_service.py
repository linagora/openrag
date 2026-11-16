"""ChatService — orchestrates the chat and completions endpoints (was ``QueryService``).

The router is pure transport: it maps the partition, builds request-bound
source links (the ``prepare_sources`` callable keeps ``request.url_for`` in
transport), and wraps ``StreamingResponse`` / ``JSONResponse``.

This module finds the sources and renders them into the prompt;
``AnswerService`` (``answer_service.py``) then calls the model, strips the
``[Sources: N]`` tag, retries an empty answer and builds ``extra``, the same
way for ``chat``, ``chat_stream`` and ``complete``. A chat request's sources
are found by the ``classic`` graph (``services.chat``), whose steps call the
capabilities in ``services.capabilities``: the scope, the contextualized
queries, the documents and web results, the map-reduce summaries. With
``metadata.rag_mode: agentic`` the ``agentic`` graph runs instead: the model
searches and reads with tools, then answers (``services.chat.agentic``). This
module settles the history, the route and the scope, runs the graph, and
prepares completions itself; ``di.chat`` builds it and compiles the graphs. The rules
with no I/O live in ``core``: small talk (``core.chat.smalltalk``), the history window
(``core.chat.history``), fitting the sources into the window
(``core.chat.budget``), the route (``core.chat.routing``), shaping the answer
(``core.chat.answer``), the request's metadata flags
(``core.models.chat.ChatOptions``) and the contextualizer's messages
(``core.prompts.query_contextualizer``). This module wires them to the LLM,
retrieval, web search and the prompt library.

No LangChain symbol is imported into this module. ``Chunk`` is converted to a
LangChain ``Document`` via ``Chunk.to_langchain()`` at the boundary so the
``format_context`` and source helpers are reused as they are.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import TYPE_CHECKING, NamedTuple

from core.chat.agent_trace import AgentTrace
from core.chat.budget import fit_prompt
from core.chat.history import history_window, latest_user_query, split_leading_system_prompt
from core.chat.routing import Route, resolve_route
from core.chat.scope import Scope
from core.models.chat import ChatOptions, RagMode
from core.models.preset import resolve_partition_chat_llm
from core.models.query import Query, SearchQueries
from core.prompts import documents_context
from core.retrieval.date_order import order_chunks_by_date
from core.retrieval.trace import RetrievalDiagnosticsContext, RetrievalTraceBuilder
from core.utils.exceptions import ContextWindowExceededError, ValidationError
from core.utils.logging import get_logger
from core.utils.text import get_num_tokens
from services.capabilities.contextualize import record_contextualization, record_not_run
from services.chat.agentic import AgentRequest
from services.chat.classic import ChatContext
from services.orchestrators.answer_service import PreparedAnswer

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.llm.llm import LLM
    from services.capabilities.contextualize import QueryContextualizer
    from services.capabilities.retrieve import SourceRetriever
    from services.capabilities.scope import ScopeResolver
    from services.chat.agentic import AgenticChat, AgentStream
    from services.chat.classic import ClassicGraph
    from services.chat.prompt_settings import PartitionPromptSettings
    from services.orchestrators.answer_service import AnswerService
    from services.orchestrators.prompt_service import PromptService
    from services.orchestrators.retrieval_service import RetrievalService

logger = get_logger()

PrepareSources = Callable[[list, list], list]


class _PrepareChatResult(NamedTuple):
    """Prepared chat data passed from orchestration to the response methods."""

    payload: dict
    docs: list
    web_results: list
    retrieved_docs: list
    retrieved_web_results: list
    citation_protocol_active: bool
    indexed_attachment_ids: list[str]
    configuration_fingerprint: str
    retry_empty_response: bool = True


class ChatService:
    """End-to-end RAG: contextualize → retrieve (+web) → map-reduce → answer."""

    #: Used when the global ``rag.chat_history_depth`` config is < 1 — see
    #: PartitionService._CHAT_HISTORY_DEPTH_DEFAULT for the matching partition-side clamp.
    _CHAT_HISTORY_DEPTH_DEFAULT = 4

    def __init__(
        self,
        *,
        config: Settings,
        llm: LLM,
        retrieval_service: RetrievalService,
        prompt_service: PromptService,
        contextualizer: QueryContextualizer,
        scope_resolver: ScopeResolver,
        retriever: SourceRetriever,
        prompt_settings: PartitionPromptSettings,
        classic: ClassicGraph,
        agentic: AgenticChat,
        answer_service: AnswerService,
        llm_factory: Callable[[str], LLM] | None = None,
    ) -> None:
        """Built by ``di.chat.create_chat_service``, which compiles the *classic* and *agentic* graphs.

        The graph's steps share *contextualizer*, *retriever* and
        *prompt_settings* with the completions endpoint, which runs outside it.
        """
        self._retrieval = retrieval_service
        self._answer = answer_service
        self._llm = llm
        self._llm_factory = llm_factory
        # Prompts resolve request-time (override → default → disk seed) so an
        # admin's edit takes effect on the next chat without a restart.
        self._prompt_service = prompt_service
        self._contextualizer = contextualizer
        self._scope = scope_resolver
        self._retriever = retriever
        self._prompt_settings = prompt_settings
        self._classic = classic
        self._agentic = agentic

        # Keep a live reference so per-partition config (resolved into
        # ``config.partitions`` and refreshed on every preset change) can be
        # read at request time, not snapshotted once at startup.
        self._config = config
        # RAGConfig.chat_history_depth carries no lower bound, so a deployment may
        # configure it to 0 (or negative). Left unclamped, messages[-0:] would keep
        # the *entire* history instead of none — the opposite of what this depth
        # is meant to limit. Clamp here rather than reject at config-load time so a
        # misconfigured global default degrades safely instead of crashing startup.
        self._default_chat_history_depth = (
            config.rag.chat_history_depth if config.rag.chat_history_depth >= 1 else self._CHAT_HISTORY_DEPTH_DEFAULT
        )

    async def refresh_partition_configs(self) -> None:
        await self._retrieval.refresh_partition_configs()

    def _resolve_chat_history_depth(self, partition: list[str] | None) -> int:
        """Effective chat-history depth for this request.

        Honors a partition's configured ``chat_history_depth`` (set via the
        admin API) over the global default. The admin API now requires an
        explicit ``chat_history_depth >= 1`` on create/update (``ge=1`` in
        ``CreatePartitionRequest``/``UpdatePartitionRequest``), and
        ``PartitionService.resolve_partition_row`` normalizes any pre-existing
        row still holding the legacy ``0`` sentinel to the global default
        value before it reaches ``Settings.partitions``. So in practice
        ``cfg.chat_history_depth`` is always >= 1 here; the ``> 0`` filter
        below is kept only as a defensive backstop — it must never reach the
        ``messages[-depth:]`` slice, where ``0`` would select the *entire*
        history rather than none.

        The ``"all"`` sentinel (``openrag-all``) reaches this layer un-expanded
        (retrieval resolves it to concrete partitions downstream) and is a
        cross-partition query with no single owning partition, so it uses the
        global default. A request scoped to one or more named partitions takes
        the largest explicit (>0) value among them.
        """
        if partition and "all" not in partition:
            explicit = [
                cfg.chat_history_depth
                for name in partition
                if (cfg := self._config.partitions.get(name)) is not None and cfg.chat_history_depth > 0
            ]
            if explicit:
                return max(explicit)
        return self._default_chat_history_depth

    def _resolve_llm(self, partition: list[str] | None) -> LLM:
        """Effective LLM for this request — query generation and answering.

        Resolution order:

        1. A partition's configured ``chat_llm`` model endpoint (set
           via the admin API) wins when the request scopes to one or more
           named partitions that agree on a single endpoint. Resolved once per
           request (``chat`` / ``chat_stream`` / ``complete``) and used for
           both the query-contextualization call and the final answer.
           Map-reduce is the exception: its relevancy/summarisation passes
           stay pinned to the static ``self._llm`` (see ``Summarizer``),
           so with a non-env default endpoint a map-reduce request's sub-calls
           run on a different model than its answer, until that post-release
           refactor lands.
        2. Otherwise the **catalog default** endpoint — the ``is_default=True``
           row, exposed by ``llm_factory`` under the ``"default"`` alias and
           resolved fresh per request so promoting a new default endpoint at
           runtime takes effect immediately. This covers a direct/web-only
           request (no partition), the cross-partition ``"all"`` sentinel,
           partitions that set no ``chat_llm``, and partitions whose ``chat_llm``
           endpoints conflict (no single owning partition). Same partition semantics as
           ``_resolve_chat_history_depth``.
        3. The static ``self._llm`` (built from ``settings.llm`` at startup)
           only as a last resort — no endpoint factory is wired (unit tests)
           or the catalog has no default endpoint yet.

        ``chat_llm`` is validated against the endpoint catalog when it is
        assigned (``PartitionService`` rejects an unknown name at create /
        PATCH time), but a stored name can still go stale afterwards — the
        endpoint may be renamed or deleted after assignment — so an
        unresolvable name here must not fail the chat request; it falls
        through to the catalog default with a warning.

        The resolved endpoint name is always logged (at debug), including for
        the default, so "which model answered?" is answerable from the logs.
        """
        chat_llm = self._agreed_partition_chat_llm(partition)
        if chat_llm is not None:
            try:
                llm = self._llm_factory(chat_llm)  # factory is not None when chat_llm is set
            except KeyError:
                logger.warning(
                    "Partition chat_llm endpoint not found in the model-endpoint catalog — "
                    "falling back to the default LLM",
                    chat_llm=chat_llm,
                    partitions=partition,
                )
            else:
                logger.bind(chat_llm=chat_llm, partitions=partition).debug(
                    "Answering with the partition's chat_llm endpoint"
                )
                return llm
        return self._default_llm(partition)

    def _agreed_partition_chat_llm(self, partition: list[str] | None) -> str | None:
        """The single ``chat_llm`` endpoint the request's partitions agree on, else None.

        Returns None — meaning "use the catalog default" — when no endpoint
        factory is wired, the request has no partition or uses the ``"all"``
        sentinel, no named partition sets one, or the named partitions
        name more than one endpoint (a conflict with no single owning partition).

        The partition-consensus decision itself is delegated to the shared
        ``resolve_partition_chat_llm`` — the same rule the chat-completions
        token preflight uses — so the LLM that answers and the budget it was
        checked against can't fall out of sync.
        """
        if self._llm_factory is None:
            return None
        return resolve_partition_chat_llm(partition, self._config.partitions)

    def _default_llm(self, partition: list[str] | None) -> LLM:
        """The catalog default LLM endpoint (``is_default=True``), resolved fresh.

        Bypassing this and returning the static ``self._llm`` was the bug
        behind "the default chat model is still the one in .env" reports:
        promoting a new default endpoint in the catalog had no effect on the
        default chat path, which stayed pinned to the ``settings.llm`` (env)
        client built at startup. Going through the factory's ``"default"``
        alias — kept in sync with the ``is_default`` row and cache-invalidated
        on every default change — makes the promotion take effect.

        Falls back to the static ``self._llm`` only when no factory is wired
        (unit tests) or the catalog has no default endpoint yet (KeyError).
        """
        if self._llm_factory is not None:
            try:
                llm = self._llm_factory("default")
            except KeyError:
                pass
            else:
                logger.bind(chat_llm=self._default_llm_name(), partitions=partition).debug(
                    "Answering with the default chat_llm endpoint"
                )
                return llm
        logger.bind(partitions=partition).debug("Answering with the static default LLM (no catalog default endpoint)")
        return self._llm

    def _default_llm_name(self) -> str:
        """Real endpoint name behind the catalog ``"default"`` alias, for logging.

        ``ModelEndpointService.load_all`` stores the ``is_default`` row's
        config under both its own name and the ``"default"`` alias (the *same*
        object), so the name is recovered by identity. Returns ``"default"``
        when it can't be resolved (e.g. the alias isn't populated yet)."""
        llms = self._config.models.llm
        default_cfg = llms.get("default")
        if default_cfg is not None:
            for name, cfg in llms.items():
                if name != "default" and cfg is default_cfg:
                    return name
        return "default"

    # ------------------------------------------------------------------
    # Preparation (was RagPipeline._prepare_for_chat_completion)
    # ------------------------------------------------------------------

    async def _prepare_chat(
        self,
        partition: list[str] | None,
        payload: dict,
        llm: LLM | None = None,
        trace: RetrievalTraceBuilder | None = None,
        max_prompt_tokens: int | None = None,
    ):
        """Find a chat request's sources and render its prompt, through the ``classic`` graph.

        The history check, the route and the scope are settled first: a request
        with no non-system message is a 422 and an unknown workspace a 404
        before any step runs. *payload*'s messages are replaced by the prompt
        the graph built, unless a web-only search found nothing.
        """
        messages = history_window(payload["messages"], self._resolve_chat_history_depth(partition))
        custom_prompt, messages = split_leading_system_prompt(payload["messages"], messages)
        if not messages:
            raise ValidationError("Request must contain at least one non-system message")

        options = ChatOptions.from_metadata(payload.get("metadata"))
        # Taken before a workspace narrows the partitions: narrowing never empties them.
        route = resolve_route(partition, options)
        scope = await self._scope.resolve(
            partition, workspace=options.workspace, attachment_ids=list(options.attachment_ids)
        )
        context = ChatContext(
            options=options,
            route=route,
            scope=scope,
            llm=llm,
            payload=payload,
            trace=trace,
            max_prompt_tokens=max_prompt_tokens,
        )
        state = await self._classic.ainvoke(
            {
                "messages": messages,
                "custom_prompt": custom_prompt,
                "last_user_message": latest_user_query(messages),
            },
            context=context,
        )
        if "answer_messages" in state:
            payload["messages"] = state["answer_messages"]
        return _PrepareChatResult(
            payload,
            state.get("docs", []),
            state.get("web_results", []),
            state.get("retrieved_docs", []),
            state.get("retrieved_web_results", []),
            state.get("citation_protocol_active", False),
            scope.attachment_ids,
            state.get("configuration_fingerprint", "unavailable"),
            state.get("retry_empty_response", True),
        )

    @staticmethod
    def _retrieval_diagnostics_context(options: ChatOptions) -> RetrievalDiagnosticsContext:
        return RetrievalDiagnosticsContext(
            query_mode=options.query_mode,
            compare_original_query=options.compare_original_query,
            effective_options=options.retrieval.as_options(),
        )

    async def _prepare_completions(
        self,
        partition: list[str],
        payload: dict,
        llm: LLM | None = None,
        max_prompt_tokens: int | None = None,
    ):
        prompt = payload["prompt"]
        options = ChatOptions.from_metadata(payload.get("metadata"))
        # partition= is ours: the retrieval preset's query_contextualizer is
        # resolved per partition. The skip below is from #807.
        queries = await self._contextualizer.contextualize(
            [{"role": "user", "content": prompt}], llm=llm, partitions=partition
        )
        prompt_type = "spoken_style_answer" if options.spoken_style_answer else "sys_prompt"
        tmpl = await self._prompt_service.resolve_prompt(
            prompt_type, names=[self._prompt_settings.generation_prompt_name(prompt_type, partition)]
        )
        current_date = datetime.now().strftime("%A, %B %d, %Y, %H:%M:%S")

        def render(context: str) -> str:
            instructions = tmpl.format(context=context, current_date=current_date, custom_prompt="")
            return f"{instructions}\n\n# User request\n{prompt}"

        # What the window leaves for sources once the instructions and the
        # prompt are in. A prompt it can't hold fails here rather than at the
        # provider.
        room = None
        if max_prompt_tokens is not None:
            room = max_prompt_tokens - get_num_tokens()(render(""))
            if room < 0:
                raise ContextWindowExceededError(max_prompt_tokens - room, max_prompt_tokens)

        retrieved_docs: list = []
        docs: list = []
        if not queries.query_list:
            if not queries.requires_retrieval and not options.require_retrieval:
                payload["prompt"] = render("")
            else:
                queries = SearchQueries(query_list=[Query(query=prompt)])
        if queries.query_list:
            chunks, _web = await self._retriever.retrieve(
                queries,
                route=Route.DOCUMENTS,
                scope=Scope(partitions=partition),
                top_k=None,
                retrieval_top_k=None,
                overrides={},
            )
            docs = [c.to_langchain() for c in chunks]
            # Full retrieval set before the token-budget selection below, kept
            # separately for `all_retrieved_sources` (#847).
            retrieved_docs = docs
            docs = [docs[i] for i in self._prompt_settings.fit_sources(docs, partition, room)]

            def assemble(docs: list, _web: list) -> tuple[str, list]:
                context = documents_context(
                    order_chunks_by_date(docs, queries.date_order),
                    any_retrieved=bool(retrieved_docs),
                    length_function=get_num_tokens(),
                )
                return render(context), []

            # Sized part by part: measure what is sent and drop documents until
            # it fits. With none left, the no-document message can still overflow.
            payload["prompt"], docs, _ = fit_prompt(
                assemble, docs, [], measure=lambda text: get_num_tokens()(text), max_tokens=max_prompt_tokens
            )
            docs = order_chunks_by_date(docs, queries.date_order)

        return payload, docs, retrieved_docs

    # ------------------------------------------------------------------
    # Public API (router = transport)
    # ------------------------------------------------------------------

    async def diagnose_retrieval(
        self,
        *,
        partitions: list[str],
        messages: list[dict],
        query_mode: str = "contextualized",
        top_k: int | None = None,
        similarity_threshold: float | None = None,
        disable_reranker: bool = False,
        disable_expansion: bool = False,
        request_id: str | None = None,
    ) -> dict[str, object]:
        """Run contextualization and retrieval diagnostics without answer generation."""
        if query_mode not in {"contextualized", "original", "compare"}:
            raise ValueError(f"Unsupported diagnostic query mode: {query_mode}")
        metadata: dict[str, object] = {
            "include_retrieval_trace": True,
            "require_retrieval": True,
            "bypass_query_contextualization": query_mode == "original",
            "compare_original_query": query_mode == "compare",
        }
        if top_k is not None:
            metadata["retrieval_top_k"] = top_k
        if similarity_threshold is not None:
            metadata["retrieval_similarity_threshold"] = similarity_threshold
        if disable_reranker:
            metadata["retrieval_disable_reranker"] = True
        if disable_expansion:
            metadata["retrieval_disable_expansion"] = True

        original_query = latest_user_query(messages)
        trace = RetrievalTraceBuilder(
            request_id=request_id or str(uuid.uuid4()),
            original_query=original_query,
            diagnostics=self._retrieval_diagnostics_context(ChatOptions.from_metadata(metadata)),
        )
        started = time.perf_counter()
        result = await self._prepare_chat(
            partitions,
            {"messages": messages, "metadata": metadata},
            self._resolve_llm(partitions),
            trace=trace,
        )
        trace.timings["total"] = time.perf_counter() - started
        return trace.finish(configuration_fingerprint=result.configuration_fingerprint)

    async def _prepare_answer(
        self,
        *,
        partitions: list[str] | None,
        payload: dict,
        prepare_sources: PrepareSources,
        llm: LLM,
        request_id: str | None,
        max_prompt_tokens: int | None,
        request_started: float,
        stream: bool = False,
    ) -> PreparedAnswer:
        """Find the sources of a chat request and render them into its prompt.

        A request with no partition and no web search goes to the model as it
        is. *max_prompt_tokens*: see ``chat``. In ``agentic``, the agent finds
        the sources and answers; with *stream*, its answer is streamed.
        """
        options = ChatOptions.from_metadata(payload.get("metadata"))
        if options.rag_mode is RagMode.AGENTIC and resolve_route(partitions, options).has_documents:
            return await self._prepare_agentic_answer(
                partitions=partitions,
                payload=payload,
                options=options,
                prepare_sources=prepare_sources,
                llm=llm,
                max_prompt_tokens=max_prompt_tokens,
                stream=stream,
                request_id=request_id,
                request_started=request_started,
            )
        original_query = latest_user_query(payload.get("messages", []))
        trace = (
            RetrievalTraceBuilder(
                request_id=request_id or str(uuid.uuid4()),
                original_query=original_query,
                diagnostics=self._retrieval_diagnostics_context(options),
            )
            if options.include_retrieval_trace
            else None
        )
        configuration_fingerprint = "unavailable"
        citation_protocol_active = False
        retry_empty_response = True
        if not resolve_route(partitions, options).searches:
            docs, web_results, retrieved_docs, retrieved_web_results = [], [], [], []
            attachments: list[str] = []
            record_not_run(trace, original_query=original_query)
        else:
            result = await self._prepare_chat(
                partitions, payload, llm, trace=trace, max_prompt_tokens=max_prompt_tokens
            )
            payload = result.payload
            docs = result.docs
            web_results = result.web_results
            retrieved_docs = result.retrieved_docs
            retrieved_web_results = result.retrieved_web_results
            citation_protocol_active = result.citation_protocol_active
            attachments = result.indexed_attachment_ids
            configuration_fingerprint = result.configuration_fingerprint
            retry_empty_response = result.retry_empty_response
        sources = prepare_sources(docs, web_results)
        # `all_retrieved_sources` is debug/eval telemetry, not needed by most
        # callers — skip building it (and calling prepare_sources on the full,
        # uncapped retrieval set) unless the caller opted in (#847 review).
        include_all_retrieved = options.include_all_retrieved_sources
        all_sources = prepare_sources(retrieved_docs, retrieved_web_results) if include_all_retrieved else None

        finish_trace = None
        if trace is not None:
            if trace.contextualization is None:
                record_contextualization(
                    trace,
                    SearchQueries(query_list=[Query(query=original_query)]),
                    original_query=original_query,
                )

            def finish_trace() -> dict:
                trace.timings["total"] = time.perf_counter() - request_started
                return {"retrieval_trace": trace.finish(configuration_fingerprint=configuration_fingerprint)}

        return PreparedAnswer(
            payload=payload,
            sources=sources,
            all_sources=all_sources,
            include_all_retrieved=include_all_retrieved,
            citation_protocol_active=citation_protocol_active,
            retry_empty_response=retry_empty_response,
            # Which attachments were actually searched to generate the answer.
            extra_fields={"attachments": attachments} if options.attachments_sent else {},
            finish_trace=finish_trace,
        )

    async def _prepare_agentic_answer(
        self,
        *,
        partitions: list[str],
        payload: dict,
        options: ChatOptions,
        prepare_sources: PrepareSources,
        llm: LLM,
        max_prompt_tokens: int | None,
        stream: bool,
        request_id: str | None = None,
        request_started: float | None = None,
    ) -> PreparedAnswer:
        """Let the agent find the sources and answer; ``AnswerService`` then shapes its answer.

        The history and the scope are settled as for ``classic``. The sources
        are every passage the tools showed, numbered as the model saw them, so
        its ``[Sources: N]`` citations map back to them. With *stream*, the
        agent runs as the client reads: its answer's text goes out as the model
        writes it, and the sources are added as the tools find them. With
        ``include_retrieval_trace``, what the agent did goes in
        ``extra.agent_trace`` once the answer is complete.
        """
        profile = self._agentic.profile(options.agent_profile)
        messages = history_window(payload["messages"], self._resolve_chat_history_depth(partitions))
        custom_prompt, messages = split_leading_system_prompt(payload["messages"], messages)
        if not messages:
            raise ValidationError("Request must contain at least one non-system message")
        scope = await self._scope.resolve(
            partitions, workspace=options.workspace, attachment_ids=list(options.attachment_ids)
        )
        trace = None
        finish_trace = None
        if options.include_retrieval_trace:
            trace = AgentTrace(
                request_id=request_id or str(uuid.uuid4()),
                profile=profile.name,
                max_tool_rounds=profile.max_tool_rounds,
                max_prompt_tokens=max_prompt_tokens,
            )
            fingerprint = await self._agent_configuration_fingerprint(scope.partitions)
            started = request_started if request_started is not None else time.perf_counter()

            def finish_trace() -> dict:
                total = time.perf_counter() - started
                return {"agent_trace": trace.finish(configuration_fingerprint=fingerprint, total_seconds=total)}

        request = AgentRequest(
            profile=profile,
            messages=messages,
            custom_prompt=custom_prompt,
            scope=scope,
            llm=llm,
            payload=payload,
            max_prompt_tokens=max_prompt_tokens,
            trace=trace,
        )
        include_all_retrieved = options.include_all_retrieved_sources
        extra_fields = {"attachments": scope.attachment_ids} if options.attachments_sent else {}
        if stream:
            sources: list = []
            answer_payload = dict(payload)
            lines = _agent_lines(await self._agentic.stream(request), sources, answer_payload, prepare_sources)
            return PreparedAnswer(
                payload=answer_payload,
                sources=sources,
                all_sources=sources if include_all_retrieved else None,
                include_all_retrieved=include_all_retrieved,
                citation_protocol_active=True,
                extra_fields=extra_fields,
                finish_trace=finish_trace,
                stream=lines,
            )
        run = await self._agentic.run(request)
        sources = prepare_sources([chunk.to_langchain() for chunk in run.sources], [])
        return PreparedAnswer(
            # The conversation the model last saw: an empty answer is retried on it.
            payload={**payload, "messages": run.messages},
            sources=sources,
            all_sources=sources if include_all_retrieved else None,
            include_all_retrieved=include_all_retrieved,
            citation_protocol_active=True,
            extra_fields=extra_fields,
            finish_trace=finish_trace,
            response=run.response,
        )

    async def _agent_configuration_fingerprint(self, partitions: list[str] | None) -> str:
        """The fingerprint of the retrieval configuration the agent's searches run with, as ``classic``'s trace has it.

        ``unavailable`` when it can't be resolved: a trace never fails a request.
        """
        if not partitions:
            return "unavailable"
        try:
            return str(await self._retrieval.resolved_configuration_fingerprint(partitions))
        except Exception:  # noqa: BLE001 - telemetry must never fail a request
            logger.opt(exception=True).debug("Agent trace: the retrieval configuration fingerprint is unavailable")
            return "unavailable"

    async def chat(
        self,
        *,
        partitions: list[str] | None,
        payload: dict,
        prepare_sources: PrepareSources,
        model_name: str,
        request_id: str | None = None,
        max_prompt_tokens: int | None = None,
    ) -> dict:
        """Non-streaming chat completion → finalized OpenAI dict.

        *max_prompt_tokens* is what the answering model's window leaves for the
        prompt (window minus output); the retrieved sources are capped to fit
        it. ``None`` means the window is unknown: only ``top_n`` caps them.
        Raises ``ContextWindowExceededError`` (413) when the instructions and
        the conversation alone don't fit in it.
        """
        request_started = time.perf_counter()
        llm = self._resolve_llm(partitions)
        prepared = await self._prepare_answer(
            partitions=partitions,
            payload=payload,
            prepare_sources=prepare_sources,
            llm=llm,
            request_id=request_id,
            max_prompt_tokens=max_prompt_tokens,
            request_started=request_started,
        )
        return await self._answer.chat(prepared, llm=llm, model_name=model_name)

    async def chat_stream(
        self,
        *,
        partitions: list[str] | None,
        payload: dict,
        prepare_sources: PrepareSources,
        model_name: str,
        request_id: str | None = None,
        max_prompt_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """Streaming chat completion → SSE strings with filtered sources (*max_prompt_tokens*: see ``chat``).

        The sources are prepared once the caller starts iterating, so a failure
        there reaches it inside the stream.
        """
        request_started = time.perf_counter()
        llm = self._resolve_llm(partitions)
        prepared = await self._prepare_answer(
            partitions=partitions,
            payload=payload,
            prepare_sources=prepare_sources,
            llm=llm,
            request_id=request_id,
            max_prompt_tokens=max_prompt_tokens,
            request_started=request_started,
            stream=True,
        )
        async for sse_line in self._answer.chat_stream(prepared, llm=llm, model_name=model_name):
            yield sse_line

    async def complete(
        self,
        *,
        partitions: list[str] | None,
        payload: dict,
        prepare_sources: PrepareSources,
        max_prompt_tokens: int | None = None,
    ) -> dict:
        """Non-streaming text completion → finalized OpenAI dict (*max_prompt_tokens*: see ``chat``)."""
        include_all_retrieved = ChatOptions.from_metadata(payload.get("metadata")).include_all_retrieved_sources
        llm = self._resolve_llm(partitions)
        if partitions is None:
            docs, retrieved_docs = [], []
        else:
            payload, docs, retrieved_docs = await self._prepare_completions(partitions, payload, llm, max_prompt_tokens)
        prepared = PreparedAnswer(
            payload=payload,
            sources=prepare_sources(docs, []),
            all_sources=prepare_sources(retrieved_docs, []) if include_all_retrieved else None,
            include_all_retrieved=include_all_retrieved,
            citation_protocol_active=partitions is not None,
        )
        return await self._answer.complete(prepared, llm=llm)


async def _agent_lines(
    agent: AgentStream, sources: list, payload: dict, prepare_sources: PrepareSources
) -> AsyncIterator[str]:
    """The agent's answer lines, keeping *sources* and *payload* up to date as they go out.

    *sources* gets the source link of each passage the tools show, before the
    line that follows it: the answer's citations are read against it. Once
    the agent is done, *payload* holds the conversation the model last saw,
    which an empty answer is retried on.
    """
    async for line in agent:
        shown = agent.sources
        if len(shown) > len(sources):
            sources.extend(prepare_sources([chunk.to_langchain() for chunk in shown[len(sources) :]], []))
        if agent.run is not None:
            payload["messages"] = agent.run.messages
        yield line


__all__ = ["ChatService"]
