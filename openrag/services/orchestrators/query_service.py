"""QueryService — RAG orchestration (Phase 8C.2).

Rebuilt from ``components/pipeline.py:RagPipeline`` + ``map_reduce.py``.
The hardest single extraction in Phase 8: query generation, retrieval,
web search, map-reduce, context formatting, system-prompt assembly, and
streaming all lived tangled in ``RagPipeline``.

Two logged decisions (REFACTORING_DECISION_LOG Phase 8):

* **Structured output** — the legacy used a LangChain structured-output
  chain for ``SearchQueries`` (query generation) and ``SummarizedChunk``
  (map-reduce). 8H bans LangChain in orchestrators, so QueryService uses
  the injected core ``LLM`` with a
  JSON-instructed prompt + ``response_format=json_object`` and
  ``json.loads`` into the Pydantic model, keeping the legacy fallbacks
  (retry → raw user query; relevancy=False on parse failure).
* **The router is pure transport.** It maps the partition, builds
  request-bound source links (the ``prepare_sources`` callable keeps
  ``request.url_for`` in transport), and wraps ``StreamingResponse`` /
  ``JSONResponse``.

This module finds the sources and renders them into the prompt;
``AnswerService`` (``answer_service.py``) then calls the model, strips the
``[Sources: N]`` tag, retries an empty answer and builds ``extra``, the same
way for ``chat``, ``chat_stream`` and ``complete``. Finding the sources is a
sequence of steps in ``services.capabilities``: the scope, the contextualized
queries, the documents and web results, the map-reduce summaries; this module
decides which of them run. The rules with no I/O live
in ``core``: small talk (``core.chat.smalltalk``), the history window
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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple

from core.chat.budget import fit_prompt, fit_sources
from core.chat.history import history_window, latest_user_query, sanitize_messages, split_leading_system_prompt
from core.chat.routing import resolve_route
from core.chat.smalltalk import (
    CasualMessagePolicy,
    acknowledgement_language,
    classify_latest_message,
)
from core.models.chat import ChatOptions
from core.models.preset import resolve_partition_chat_llm
from core.models.query import Query, SearchQueries
from core.models.retrieval_trace import ContextualizationTrace, PromptTrace
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
from core.retrieval.trace import RetrievalDiagnosticsContext, RetrievalTraceBuilder, canonical_fingerprint
from core.utils.exceptions import ContextWindowExceededError, ValidationError
from core.utils.logging import get_logger
from core.utils.text import get_num_tokens
from services.capabilities.contextualize import (
    QueryContextualizer,
    mark_fallback,
    record_bypass,
    record_contextualization,
    record_not_run,
)
from services.capabilities.retrieve import SourceRetriever
from services.capabilities.scope import ScopeResolver
from services.capabilities.summarize import Summarizer
from services.capabilities.web_search import WebSearch
from services.inference.runtime import detect_language, get_llm_semaphore
from services.orchestrators.answer_service import AnswerService, PreparedAnswer

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.llm.llm import LLM
    from services.orchestrators.prompt_service import PromptService
    from services.orchestrators.retrieval_service import RetrievalService
    from services.orchestrators.workspace_service import WorkspaceService

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


class QueryService:
    """End-to-end RAG: query-gen → retrieve (+web) → map-reduce → answer."""

    #: Used when the global ``rag.chat_history_depth`` config is < 1 — see
    #: PartitionService._CHAT_HISTORY_DEPTH_DEFAULT for the matching partition-side clamp.
    _CHAT_HISTORY_DEPTH_DEFAULT = 4

    def __init__(
        self,
        *,
        retrieval_service: RetrievalService,
        llm: LLM,
        config: Settings,
        web_search_service: Any | None,
        workspace_service: WorkspaceService,
        prompt_service: PromptService,
        llm_factory: Callable[[str], LLM] | None = None,
        answer_service: AnswerService | None = None,
    ) -> None:
        self._retrieval = retrieval_service
        self._answer = answer_service or AnswerService(detect_language=detect_language)
        self._llm = llm
        self._llm_factory = llm_factory
        # Prompts resolve request-time (override → default → disk seed) so an
        # admin's edit takes effect on the next chat without a restart.
        self._prompt_service = prompt_service
        self._contextualizer = QueryContextualizer(
            config=config,
            prompt_service=prompt_service,
            llm=llm,
            detect_language=detect_language,
            clock=lambda: datetime.now(UTC),
        )
        self._scope = ScopeResolver(workspace_service)
        self._web = WebSearch(web_search_service)
        self._retriever = SourceRetriever(retrieval_service=retrieval_service, web_search=self._web)
        self._summarizer = Summarizer(llm=llm, config=config, semaphore=get_llm_semaphore)

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

    def _resolve_top_n(self, partition: list[str] | None) -> int:
        """How many sources this request's prompt takes: the retrieval preset's ``top_n``.

        ``reranker.top_k`` when the preset leaves it unset. Retrieval already
        cuts each group of partitions it retrieves together (one embedder, the
        same retrieval settings) to the group's ``top_n``; this matters for a
        request whose partitions, or ``"all"``, span several groups, whose fused
        results can hold up to the *sum* of them. It takes the largest ``top_n``
        among the partitions, so about one group's worth (in RRF order) reaches
        the prompt.
        """
        default = self._config.reranker.top_k
        configs = self._config.partitions
        if partition and configs:
            names = list(configs) if "all" in partition else partition
            top_ns = [
                cfg.retrieval.effective_top_n(default) for name in names if (cfg := configs.get(name)) is not None
            ]
            if top_ns:
                return max(top_ns)
        return default

    def _fit_sources(self, docs: list, partition: list[str] | None, headroom: int | None) -> list[int]:
        """Positions of up to ``top_n`` of *docs*, within the *headroom* the answering model's window leaves.

        See ``core.chat.budget.fit_sources``. The window comes from the LLM
        endpoint's context size (admin-set, else probed from vLLM, else
        ``MAX_LLM_CONTEXT_SIZE``), so a too-small value there shows up here as
        fewer sources than ``top_n``: logged, because nothing else surfaces it.
        """
        top_n = self._resolve_top_n(partition)
        included = fit_sources(docs, top_n=top_n, headroom=headroom, length_function=get_num_tokens())
        if len(included) < min(len(docs), top_n):
            logger.bind(partition=partition, top_n=top_n, presented=len(included), headroom=headroom).info(
                "Sources left out: they don't fit in the answering model's context window"
            )
        return included

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

    def _generation_prompt_name(self, prompt_type: str, partition: list[str] | None) -> str | None:
        """The library prompt this request's partition names for a generation type.

        Honoured only for a single owning partition (same rule as chat_llm /
        chat_history_depth); multi-partition and the ``"all"`` sentinel resolve
        the global default. Returned as the sole candidate name for
        ``PromptService.resolve_prompt`` — a future per-user tier prepends ahead
        of it.
        """
        if not partition or "all" in partition or len(partition) != 1:
            return None
        cfg = self._config.partitions.get(partition[0])
        if cfg is None:
            return None
        return getattr(cfg, "generation_prompt_names", {}).get(prompt_type)

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
        messages = history_window(payload["messages"], self._resolve_chat_history_depth(partition))
        custom_prompt, messages = split_leading_system_prompt(payload["messages"], messages)
        if not messages:
            raise ValidationError("Request must contain at least one non-system message")

        options = ChatOptions.from_metadata(payload.get("metadata"))
        # Taken before a workspace narrows the partitions: narrowing never empties them.
        route = resolve_route(partition, options)

        effective_retrieval_options = options.retrieval.as_options()
        retrieval_top_k = effective_retrieval_options.get("top_k")
        top_k = self._summarizer.max_total_documents if options.use_map_reduce else None
        retrieval_overrides = {key: value for key, value in effective_retrieval_options.items() if key != "top_k"}

        scope = await self._scope.resolve(
            partition, workspace=options.workspace, attachment_ids=list(options.attachment_ids)
        )
        partition = scope.partitions
        indexed_attachment_ids = scope.attachment_ids

        last_user_message = latest_user_query(messages)
        casual_policy, acknowledgement_candidate = classify_latest_message(
            messages, last_user_message, query_rewrite=self._contextualizer.rewrites_queries
        )
        retrieval_forced = (
            options.require_retrieval or options.websearch or options.use_map_reduce or bool(indexed_attachment_ids)
        )
        queries: SearchQueries | None = None
        configuration_fingerprint = "unavailable"
        resolved_retrieval_plan = None

        if options.bypass_query_contextualization:
            queries = SearchQueries(query_list=[Query(query=last_user_message)])
            record_bypass(trace, original_query=last_user_message)
        elif casual_policy is None or retrieval_forced:
            queries = await self._contextualizer.contextualize(messages, llm=llm, partitions=partition, trace=trace)
            usable_queries = [query for query in queries.query_list if query.query.strip()]
            if (
                acknowledgement_candidate
                and not retrieval_forced
                and queries.intent == "gratitude"
                and not queries.requires_retrieval
                and not usable_queries
            ):
                casual_policy = CasualMessagePolicy(
                    "acknowledgement", acknowledgement_language(messages, last_user_message, detect_language)
                )
            elif not usable_queries:
                queries = SearchQueries(query_list=[Query(query=last_user_message)])
                mark_fallback(trace, queries)
            elif len(usable_queries) != len(queries.query_list):
                queries = queries.model_copy(update={"query_list": usable_queries})

        if trace is not None:
            contextualizer_prompt = trace.contextualization.prompt if trace.contextualization is not None else None
            resolve_plan = getattr(self._retrieval, "resolve_retrieval_plan", None)
            if partition is not None and resolve_plan is not None:
                try:
                    resolved_retrieval_plan = await resolve_plan(
                        partition,
                        top_k=retrieval_top_k,
                        contextualizer_prompt=contextualizer_prompt,
                        **retrieval_overrides,
                    )
                except Exception as error:  # noqa: BLE001 - legacy resolution below keeps telemetry optional
                    trace.record_error("configuration_resolution", error)
            configuration_fingerprint = await self._configuration_fingerprint(
                partition,
                effective_options=effective_retrieval_options,
                contextualizer_prompt=contextualizer_prompt,
                resolved_plan=resolved_retrieval_plan,
            )

        # The history as the model gets it, so the window is checked against what is sent.
        messages = sanitize_messages(messages)

        if casual_policy is not None and not retrieval_forced:
            if trace is not None and trace.contextualization is None:
                trace.contextualization = ContextualizationTrace(
                    original_query=last_user_message,
                    subqueries=[],
                    intent=casual_policy.intent,
                    requires_retrieval=False,
                )
                trace.record_stage("original_query", status="complete", candidates=[])
                trace.record_stage("contextualized_query", status="not_run", candidates=[])
            casual_prompt = build_casual_response_prompt(
                casual_policy.intent, casual_policy.language, assistant_name=self._config.server.assistant_name
            )
            payload["messages"] = prepend_system_prompt(
                messages,
                casual_prompt,
                context="",
                current_date=datetime.now().strftime("%A, %B %d, %Y, %H:%M:%S"),
                custom_prompt=custom_prompt,
            )
            if max_prompt_tokens is not None:
                sent = _messages_tokens(payload["messages"]) + tool_definition_tokens(payload, get_num_tokens())
                if sent > max_prompt_tokens:
                    raise ContextWindowExceededError(sent, max_prompt_tokens)
            return _PrepareChatResult(
                payload,
                [],
                [],
                [],
                [],
                True,
                indexed_attachment_ids,
                configuration_fingerprint,
                True,
            )

        if queries is None:  # pragma: no cover - guarded by the casual return above
            queries = SearchQueries(query_list=[Query(query=last_user_message)])

        chunks, web_results = await self._retriever.retrieve(
            queries,
            route=route,
            scope=scope,
            top_k=top_k,
            retrieval_top_k=retrieval_top_k,
            overrides=retrieval_overrides,
            trace=trace,
            resolved_plan=resolved_retrieval_plan,
        )

        if trace is not None and route.has_documents and options.compare_original_query:
            await self._retriever.compare_original_query(
                trace,
                original_query=last_user_message,
                scope=scope,
                top_k=top_k,
                retrieval_top_k=retrieval_top_k,
                overrides=retrieval_overrides,
                configuration_fingerprint=configuration_fingerprint,
                resolved_plan=resolved_retrieval_plan,
            )

        if not chunks and not web_results and not route.has_documents:
            return _PrepareChatResult(
                payload,
                [],
                [],
                [],
                [],
                False,
                indexed_attachment_ids,
                configuration_fingerprint,
            )

        docs = [c.to_langchain() for c in chunks]

        # Full retrieval set, captured right after retrieval — before map-reduce
        # replaces `docs` with LLM-generated summaries, and before the
        # token-budget selection below drops anything that didn't fit in the
        # prompt. Kept separately for `all_retrieved_sources` (debugging/eval),
        # while `docs`/`web_results` stay map-reduced and budget-truncated to
        # match what the LLM actually saw and the citation indices it cites
        # (#847 review — the map-reduce gap was called out in a follow-up pass).
        retrieved_docs = docs
        retrieved_web_results = web_results

        prompt_type = "spoken_style_answer" if options.spoken_style_answer else "sys_prompt"
        tmpl = await self._prompt_service.resolve_prompt(
            prompt_type, names=[self._generation_prompt_name(prompt_type, partition)]
        )
        current_date = datetime.now().strftime("%A, %B %d, %Y, %H:%M:%S")
        # What the window leaves for sources once the instructions, the
        # conversation and the client's tool definitions are in. A conversation
        # it can't hold fails here, before map-reduce, rather than at the provider.
        room = None
        tools_tokens = tool_definition_tokens(payload, get_num_tokens())
        if max_prompt_tokens is not None:
            without_sources = prepend_system_prompt(
                messages, tmpl, context="", current_date=current_date, custom_prompt=custom_prompt
            )
            room = max_prompt_tokens - _messages_tokens(without_sources) - tools_tokens
            if room < 0:
                raise ContextWindowExceededError(max_prompt_tokens - room, max_prompt_tokens)

        if options.use_map_reduce and docs:
            docs = await self._summarizer.summarize(" ".join(q.query for q in queries.query_list), docs)

        # Web results come out of the same room, so they can't take more than it either.
        web_max_tokens = self._web.max_tokens if room is None else min(self._web.max_tokens, room)
        web_formatted, web_source_numbers, web_tokens = "", [], 0
        web_start_index = 1
        if web_results:
            web_formatted, web_source_numbers, web_tokens = format_web_context(
                web_results,
                length_function=get_num_tokens(),
                start_index=web_start_index,
                max_tokens=web_max_tokens,
            )
        headroom = None if room is None else room - web_tokens
        candidates = docs
        docs = [docs[i] for i in self._fit_sources(docs, partition, headroom)]

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
                messages, tmpl, context=context, current_date=current_date, custom_prompt=custom_prompt
            )
            return answer_messages, web_shown

        # The sources were sized part by part, without the separators between
        # documents and web results or the web results' final numbering: measure
        # what is sent, and drop sources (documents first) until it fits.
        new_messages, docs, web_results = fit_prompt(
            assemble,
            docs,
            web_results,
            measure=lambda answer_messages: _messages_tokens(answer_messages) + tools_tokens,
            max_tokens=max_prompt_tokens,
        )
        payload["messages"] = new_messages
        return _PrepareChatResult(
            payload,
            # The order they were rendered in: citation numbers index into it.
            order_chunks_by_date(docs, queries.date_order),
            web_results,
            retrieved_docs,
            retrieved_web_results,
            True,
            indexed_attachment_ids,
            configuration_fingerprint,
        )

    @staticmethod
    def _retrieval_diagnostics_context(options: ChatOptions) -> RetrievalDiagnosticsContext:
        return RetrievalDiagnosticsContext(
            query_mode=options.query_mode,
            compare_original_query=options.compare_original_query,
            effective_options=options.retrieval.as_options(),
        )

    async def _configuration_fingerprint(
        self,
        partitions: list[str] | None,
        *,
        effective_options: dict[str, object] | None = None,
        contextualizer_prompt: PromptTrace | None = None,
        resolved_plan=None,
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
            prompt_type, names=[self._generation_prompt_name(prompt_type, partition)]
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
            chunks = await self._retrieval.retrieve_multi(partitions=partition, search_queries=queries)
            docs = [c.to_langchain() for c in chunks]
            # Full retrieval set before the token-budget selection below, kept
            # separately for `all_retrieved_sources` (#847).
            retrieved_docs = docs
            docs = [docs[i] for i in self._fit_sources(docs, partition, room)]

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
    ) -> PreparedAnswer:
        """Find the sources of a chat request and render them into its prompt.

        A request with no partition and no web search goes to the model as it
        is. *max_prompt_tokens*: see ``chat``.
        """
        options = ChatOptions.from_metadata(payload.get("metadata"))
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


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _messages_tokens(messages: list[dict]) -> int:
    """Tokens a chat message list takes, counted as the router's preflight does."""
    length_function = get_num_tokens()
    return sum(message_tokens(message, length_function) for message in messages)


__all__ = ["QueryService"]
