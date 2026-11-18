"""ChatService — orchestrates the chat and completions endpoints (was ``QueryService``).

The router is pure transport: it maps the partition, builds request-bound
source links (the ``prepare_sources`` callable keeps ``request.url_for`` in
transport), and wraps ``StreamingResponse`` / ``JSONResponse``.

This module finds the sources and renders them into the prompt;
``AnswerService`` (``answer_service.py``) then calls the model, strips the
``[Sources: N]`` tag, retries an empty answer and builds ``extra``, the same
way for ``chat``, ``chat_stream`` and ``complete``. Every chat request runs a
workflow (``services.chat.workflow``): today the ``classic`` graph
(``services.chat.classic.ClassicWorkflow``), whose steps settle the history
window and the scope, then call the capabilities in ``services.capabilities``:
the contextualized queries, the documents and web results, the map-reduce
summaries. A request with no partition and no web search goes through it too,
and reaches the model as it was sent. This module resolves the answering LLM,
picks the workflow, and prepares completions itself; ``di.chat`` builds it and
compiles the graph. The rules with no I/O live in ``core``: small talk
(``core.chat.smalltalk``), the history window
(``core.chat.history``), fitting the sources into the window
(``core.chat.budget``), the route (``core.chat.routing``), shaping the answer
(``core.chat.answer``), the request's metadata flags
(``core.models.chat.ChatOptions``) and the contextualizer's messages
(``core.prompts.query_contextualizer``). This module wires them to the LLM,
retrieval, web search and the prompt library.

The contextualizer and map-reduce steps call the injected core ``LLM`` with a
JSON-instructed prompt and ``response_format=json_object``, parsed into
Pydantic models: a rewrite that can't be read falls back to the user's
message, a map-reduce verdict that can't be read counts as not relevant.

No LangChain symbol is imported into this module. ``Chunk`` is converted to a
LangChain ``Document`` via ``Chunk.to_langchain()`` at the boundary so the
``format_context`` and source helpers are reused as they are.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import TYPE_CHECKING

from core.chat.budget import fit_prompt
from core.chat.routing import Route
from core.chat.scope import Scope
from core.models.chat import ChatOptions
from core.models.preset import resolve_partition_chat_llm
from core.models.query import Query, SearchQueries
from core.prompts import documents_context
from core.retrieval.date_order import order_chunks_by_date
from core.utils.exceptions import ContextWindowExceededError
from core.utils.logging import get_logger
from core.utils.text import get_num_tokens
from services.chat.workflow import ChatRequest, ChatWorkflow, PrepareSources
from services.orchestrators.answer_service import PreparedAnswer

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.llm.llm import LLM
    from services.capabilities.contextualize import QueryContextualizer
    from services.capabilities.retrieve import SourceRetriever
    from services.chat.classic import ClassicWorkflow
    from services.chat.prompt_settings import PartitionPromptSettings
    from services.orchestrators.answer_service import AnswerService
    from services.orchestrators.prompt_service import PromptService
    from services.orchestrators.retrieval_service import RetrievalService

logger = get_logger()


class ChatService:
    """End-to-end RAG: query-gen → retrieve (+web) → map-reduce → answer."""

    def __init__(
        self,
        *,
        config: Settings,
        llm: LLM,
        retrieval_service: RetrievalService,
        prompt_service: PromptService,
        contextualizer: QueryContextualizer,
        retriever: SourceRetriever,
        prompt_settings: PartitionPromptSettings,
        classic: ClassicWorkflow,
        answer_service: AnswerService,
        llm_factory: Callable[[str], LLM] | None = None,
    ) -> None:
        """Built by ``di.chat.create_chat_service``, which compiles the *classic* graph.

        Every chat request runs the *classic* workflow. Its steps share
        *contextualizer*, *retriever* and *prompt_settings* with the
        completions endpoint, which runs outside it.
        """
        self._retrieval = retrieval_service
        self._answer = answer_service
        self._llm = llm
        self._llm_factory = llm_factory
        # Prompts resolve request-time (override → default → disk seed) so an
        # admin's edit takes effect on the next chat without a restart.
        self._prompt_service = prompt_service
        self._contextualizer = contextualizer
        self._retriever = retriever
        self._prompt_settings = prompt_settings
        self._classic = classic

        # Keep a live reference so per-partition config (resolved into
        # ``config.partitions`` and refreshed on every preset change) can be
        # read at request time, not snapshotted once at startup.
        self._config = config

    async def refresh_partition_configs(self) -> None:
        await self._retrieval.refresh_partition_configs()

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
    # Preparation
    # ------------------------------------------------------------------

    def _workflow(self, options: ChatOptions) -> ChatWorkflow:
        """The workflow that finds a chat request's sources: ``classic``, the one there is."""
        return self._classic

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

        started = time.perf_counter()
        request = ChatRequest(
            partitions=partitions,
            payload={"messages": messages, "metadata": metadata},
            options=ChatOptions.from_metadata(metadata),
            llm=self._resolve_llm(partitions),
            prepare_sources=lambda _docs, _web: [],
            started=started,
            request_id=request_id,
        )
        trace = self._classic.new_trace(request)
        run = await self._classic.run(request, trace)
        trace.timings["total"] = time.perf_counter() - started
        return trace.finish(configuration_fingerprint=run.configuration_fingerprint)

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
        """Find the sources of a chat request and render them into its prompt, with its workflow.

        *max_prompt_tokens*: see ``chat``.
        """
        options = ChatOptions.from_metadata(payload.get("metadata"))
        request = ChatRequest(
            partitions=partitions,
            payload=payload,
            options=options,
            llm=llm,
            prepare_sources=prepare_sources,
            started=request_started,
            request_id=request_id,
            max_prompt_tokens=max_prompt_tokens,
        )
        return await self._workflow(options).prepare(request)

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


__all__ = ["ChatService"]
