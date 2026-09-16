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
* **Streaming + citations live here; the router is pure transport.**
  ``chat_stream`` drives the proven
  ``core.utils.source_filtering.stream_with_source_filtering`` (100-char buffer that
  strips the ``[Sources: N]`` tag before it reaches the client);
  ``chat`` / ``complete`` return the finalized OpenAI dict with the
  citation-filtered ``extra`` sources. The router only maps the
  partition, builds request-bound source links (``prepare_sources``
  callable — keeps ``request.url_for`` in transport), and wraps
  ``StreamingResponse`` / ``JSONResponse``.

Imports from ``components.*`` (prompt shims) are
allowed during the Phase-8 shim (legacy layer, unchecked by the guard;
no LangChain symbol is imported into this file → 8H clean). ``Chunk`` is
converted to LangChain ``Document`` via ``Chunk.to_langchain()`` at the
boundary so the existing ``format_context`` / source helpers are reused
verbatim (no langchain import in this module).
"""

from __future__ import annotations

import asyncio
import json
import time
import unicodedata
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, NamedTuple

from core.models.preset import resolve_partition_chat_llm
from core.models.query import Query, SearchQueries
from core.models.retrieval_trace import (
    ContextualizationTrace,
    ContextualizedSubqueryTrace,
    PromptTrace,
    TemporalFilterTrace,
    TraceError,
)
from core.prompts import (
    SOURCE_SEPARATOR,
    build_casual_response_prompt,
    format_context,
    format_web_context,
    prepend_system_prompt,
)
from core.retrieval.trace import RetrievalTraceBuilder
from core.utils.exceptions import ValidationError, WorkspaceNotFoundError
from core.utils.logging import get_logger
from core.utils.source_filtering import (
    extract_and_strip_sources_block,
    filter_sources_by_citations,
    stream_with_source_filtering,
)
from core.utils.text import get_num_tokens
from core.utils.web_url import normalize_web_url
from services.inference.runtime import detect_language, get_llm_semaphore
from services.orchestrators.prompt_service import ResolvedPrompt

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.llm.llm import LLM
    from services.orchestrators.prompt_service import PromptService
    from services.orchestrators.retrieval_service import RetrievalService
    from services.orchestrators.workspace_service import WorkspaceService

logger = get_logger()

PrepareSources = Callable[[list, list], list]


class CasualMessagePolicy(NamedTuple):
    intent: str
    language: str


CASUAL_MESSAGE_INTENTS: dict[str, CasualMessagePolicy] = {
    "bonjour": CasualMessagePolicy("greeting", "fr"),
    "salut": CasualMessagePolicy("greeting", "fr"),
    "hello": CasualMessagePolicy("greeting", "en"),
    "hey": CasualMessagePolicy("greeting", "en"),
    "comment allez vous": CasualMessagePolicy("greeting", "fr"),
    "how are you": CasualMessagePolicy("greeting", "en"),
    "hey how are you": CasualMessagePolicy("greeting", "en"),
    "merci": CasualMessagePolicy("gratitude", "fr"),
    "thank you": CasualMessagePolicy("gratitude", "en"),
    "thanks": CasualMessagePolicy("gratitude", "en"),
    "au revoir": CasualMessagePolicy("farewell", "fr"),
    "bye": CasualMessagePolicy("farewell", "en"),
}
CASUAL_MESSAGES = frozenset(CASUAL_MESSAGE_INTENTS)
_EMPTY_CASUAL_POLICY = CasualMessagePolicy("empty", "en")
_CASUAL_LANGUAGE_MIN_CONFIDENCE = 0.8


def normalize_casual_message(message: str) -> str:
    """Normalize a complete message for strict casual-message matching."""
    normalized = unicodedata.normalize("NFKC", message).casefold()
    characters: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        codepoint = ord(character)
        is_variation_selector = 0xFE00 <= codepoint <= 0xFE0F or 0xE0100 <= codepoint <= 0xE01EF
        if category.startswith("S") or category == "Cf" or is_variation_selector or character == "\u20e3":
            continue
        characters.append(" " if category.startswith("P") else character)
    return " ".join("".join(characters).split())


def casual_message_policy(message: str) -> CasualMessagePolicy | None:
    """Return the exact-match casual policy, including empty-input fallback."""
    normalized = normalize_casual_message(message)
    if not normalized:
        return _EMPTY_CASUAL_POLICY
    return CASUAL_MESSAGE_INTENTS.get(normalized)


class _PrepareChatResult(NamedTuple):
    """A named tuple stays positionally unpackable, so existing
    ``a, b, c, ... = await self._prepare_chat(...)`` call sites still work.
    """

    payload: dict
    docs: list
    web_results: list
    retrieved_docs: list
    retrieved_web_results: list
    citation_protocol_active: bool
    indexed_attachment_ids: list[str]


_MAP_SYSTEM_PROMPT = """You are an AI assistant specialized in extracting and synthesizing relevant information from text.

Your task:
1. Analyze the provided text in relation to the user's question
2. Extract only the essential information that directly addresses the query
3. Preserve necessary context (key words, project names, dates) so the summary is self-understandable

Respond with a JSON object exactly matching this schema:
{"relevancy": <true|false>, "summary": "<summary text, empty string if not relevant>"}
Set relevancy=false (and summary="") if the text has no relevant content for the query."""

_MAP_USER_PROMPT = """Here is a text:
{content}

From this document, identify and comprehensively summarize the information useful for answering the following question:
{query}"""

_QUERY_JSON_HINT = (
    "\n\nClassify the complete latest user message conservatively. Retrieval is the default. "
    "Only a message consisting exclusively of a greeting or salutation, gratitude, farewell, or a question about "
    "the assistant's capabilities may skip retrieval. A factual, informational, analytical, ambiguous, or mixed "
    "message must require retrieval, even when it has no question mark or also contains a social phrase. "
    "Respond ONLY with one of these JSON forms: "
    '{"intent": "greeting", "requires_retrieval": false, "query_list": []} for an exclusively casual message '
    "(using gratitude, farewell, or capability instead of greeting when appropriate), or "
    '{"intent": "other", "requires_retrieval": true, '
    '"query_list": [{"query": "<search query>", "temporal_filters": null}]} for everything else. '
    "If uncertain, use intent other and require retrieval."
)


class RAGMODE(Enum):
    SIMPLERAG = "SimpleRag"
    CHATBOTRAG = "ChatBotRag"


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
    ) -> None:
        self._retrieval = retrieval_service
        self._llm = llm
        self._llm_factory = llm_factory
        self._web = web_search_service
        self._workspace = workspace_service
        # Prompts resolve request-time (override → default → disk seed) so an
        # admin's edit takes effect on the next chat without a restart.
        self._prompt_service = prompt_service

        # Keep a live reference so per-partition config (resolved into
        # ``config.partitions`` and refreshed on every preset change) can be
        # read at request time, not snapshotted once at startup.
        self._config = config
        self._rag_mode = config.rag.mode
        # RAGConfig.chat_history_depth carries no lower bound, so a deployment may
        # configure it to 0 (or negative). Left unclamped, messages[-0:] would keep
        # the *entire* history instead of none — the opposite of what this depth
        # is meant to limit. Clamp here rather than reject at config-load time so a
        # misconfigured global default degrades safely instead of crashing startup.
        self._default_chat_history_depth = (
            config.rag.chat_history_depth if config.rag.chat_history_depth >= 1 else self._CHAT_HISTORY_DEPTH_DEFAULT
        )
        self._max_contextualized_query_len = config.rag.max_contextualized_query_len
        # Sized on the assumption that retrieval returns ~reranker.top_k chunks,
        # but reranker_top_k is never actually applied as a cutoff in
        # RetrieverPipeline.retrieve_docs() on the no-map-reduce path — retrieval
        # can return up to retriever.top_k candidates, so this budget (not
        # reranker.top_k) is what actually determines how many reach the prompt.
        # Tracked separately: https://github.com/linagora/openrag/issues/851
        self._max_context_tokens = config.reranker.top_k * config.chunker.chunk_size

        mr = config.map_reduce
        self._mr_initial = mr.initial_batch_size
        self._mr_expansion = mr.expansion_batch_size
        self._mr_max = mr.max_total_documents

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

        1. A partition's configured ``chat_llm`` model-endpoint preset (set
           via the admin API) wins when the request scopes to one or more
           named partitions that agree on a single preset. Resolved once per
           request (``chat`` / ``chat_stream`` / ``complete``) and used for
           both the query-contextualization call and the final answer.
           Map-reduce is the exception: its relevancy/summarisation passes
           stay pinned to the static ``self._llm`` (see ``_infer_relevancy``),
           so with a non-env default endpoint a map-reduce request's sub-calls
           run on a different model than its answer, until that post-release
           refactor lands.
        2. Otherwise the **catalog default** endpoint — the ``is_default=True``
           row, exposed by ``llm_factory`` under the ``"default"`` alias and
           resolved fresh per request so promoting a new default endpoint at
           runtime takes effect immediately. This covers a direct/web-only
           request (no partition), the cross-partition ``"all"`` sentinel,
           partitions that set no preset, and partitions whose presets
           conflict (no single owning partition). Same partition semantics as
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

        The resolved preset name is always logged (at debug), including for
        the default, so "which model answered?" is answerable from the logs.
        """
        chat_llm = self._agreed_partition_chat_llm(partition)
        if chat_llm is not None:
            try:
                llm = self._llm_factory(chat_llm)  # factory is not None when chat_llm is set
            except KeyError:
                logger.warning(
                    "Partition chat_llm preset not found in the model-endpoint catalog — "
                    "falling back to the default LLM",
                    chat_llm=chat_llm,
                    partitions=partition,
                )
            else:
                logger.bind(chat_llm=chat_llm, partitions=partition).debug(
                    "Answering with the partition's chat_llm preset"
                )
                return llm
        return self._default_llm(partition)

    def _agreed_partition_chat_llm(self, partition: list[str] | None) -> str | None:
        """The single ``chat_llm`` preset the request's partitions agree on, else None.

        Returns None — meaning "use the catalog default" — when no endpoint
        factory is wired, the request has no partition or uses the ``"all"``
        sentinel, no named partition sets a preset, or the named partitions
        name more than one preset (a conflict with no single owning partition).

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
                    "Answering with the default chat_llm preset"
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
    # Query generation (was RagPipeline.generate_query — no LangChain)
    # ------------------------------------------------------------------

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

    def _retrieval_prompt_name(self, field: str, partition: list[str] | None) -> str | None:
        """The library prompt this request's partition names on its retrieval
        preset (query-side prompts: query_contextualizer / hyde / multi_query).

        Same single-owning-partition rule as generation prompts; multi-partition
        and ``"all"`` resolve the global default.
        """
        if not partition or "all" in partition or len(partition) != 1:
            return None
        cfg = self._config.partitions.get(partition[0])
        if cfg is None:
            return None
        return getattr(getattr(cfg, "retrieval", None), field, None)

    async def generate_query(
        self,
        messages: list[dict],
        llm: LLM | None = None,
        partition: list[str] | None = None,
        trace: RetrievalTraceBuilder | None = None,
    ) -> SearchQueries:
        llm = llm or self._llm
        last_user = messages[-1]["content"]
        if RAGMODE(self._rag_mode) is RAGMODE.SIMPLERAG:
            queries = SearchQueries(query_list=[Query(query=last_user)])
            self._record_contextualization(trace, queries, original_query=last_user)
            return queries

        chat_history = "".join(f"{m['role']}: {m.get('content') or ''}\n" for m in messages)
        prompt_name = self._retrieval_prompt_name("query_contextualizer_prompt_name", partition)
        resolve_with_identity = getattr(self._prompt_service, "resolve_prompt_with_identity", None)
        if resolve_with_identity is not None:
            contextualizer = await resolve_with_identity("query_contextualizer", names=[prompt_name])
        else:
            content = await self._prompt_service.resolve_prompt("query_contextualizer", names=[prompt_name])
            contextualizer = ResolvedPrompt.create(content, name=prompt_name, source="named" if prompt_name else "default")
        prompt = contextualizer.content.format(
            query_language=detect_language(last_user),
            current_date=datetime.now().strftime("%A, %B %d, %Y, %H:%M:%S"),
        )
        llm_messages = [
            {"role": "system", "content": prompt + _QUERY_JSON_HINT},
            {"role": "user", "content": f"Here is the chat history: \n{chat_history}\n"},
        ]
        params = {
            "max_completion_tokens": self._max_contextualized_query_len,
            "response_format": {"type": "json_object"},
        }
        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt in (1, 2):
            try:
                resp = await llm.chat(llm_messages, **params)
                content = resp["choices"][0]["message"]["content"]
                queries = SearchQueries.model_validate_json(_json_slice(content))
                self._record_contextualization(
                    trace,
                    queries,
                    original_query=last_user,
                    duration_seconds=time.perf_counter() - started,
                    llm=llm,
                    prompt=contextualizer,
                )
                return queries
            except Exception as exc:
                last_error = exc
                if attempt == 1:
                    logger.warning("Query generation parse error — retrying", error=str(exc))
                else:
                    logger.warning(
                        "Query generation failed twice — falling back to raw user query",
                        error=str(exc),
                    )
        queries = SearchQueries(query_list=[Query(query=last_user)])
        self._record_contextualization(
            trace,
            queries,
            original_query=last_user,
            duration_seconds=time.perf_counter() - started,
            llm=llm,
            prompt=contextualizer,
            fallback_used=True,
            error=last_error,
        )
        return queries

    @staticmethod
    def _record_contextualization(
        trace: RetrievalTraceBuilder | None,
        queries: SearchQueries,
        *,
        original_query: str,
        duration_seconds: float = 0.0,
        llm: LLM | None = None,
        prompt: ResolvedPrompt | None = None,
        fallback_used: bool = False,
        error: Exception | None = None,
    ) -> None:
        if trace is None:
            return
        try:
            trace.contextualization = ContextualizationTrace(
                original_query=original_query,
                subqueries=[
                    ContextualizedSubqueryTrace(
                        query=query.query,
                        temporal_filters=[
                            TemporalFilterTrace(operator=item.operator, value=item.value)
                            for item in (query.temporal_filters or [])
                        ],
                    )
                    for query in queries.query_list
                ],
                intent=queries.intent,
                requires_retrieval=queries.requires_retrieval,
                fallback_used=fallback_used,
                error=(
                    TraceError(stage="contextualization", message="redacted", kind=type(error).__name__)
                    if error is not None
                    else None
                ),
                duration_seconds=duration_seconds,
                endpoint=getattr(llm, "_endpoint", None),
                model=getattr(llm, "_model", None),
                prompt=(
                    PromptTrace(content_hash=prompt.content_hash, name=prompt.name, source=prompt.source)
                    if prompt is not None
                    else None
                ),
            )
            trace.timings["contextualization"] = duration_seconds
            trace.record_stage("original_query", status="complete", candidates=[])
            trace.record_stage("contextualized_query", status="complete", candidates=[])
        except Exception as trace_error:
            trace.record_error("contextualization", trace_error)

    # ------------------------------------------------------------------
    # Map-reduce (was map_reduce.RAGMapReduce — no LangChain)
    # ------------------------------------------------------------------

    async def _infer_relevancy(self, query: str, doc) -> tuple[bool, str]:
        # Deliberately pinned to the static ``self._llm`` (the settings.llm env
        # client) — NOT the resolved catalog default the answer uses, so a
        # map-reduce request's sub-calls may run on a different model than its
        # answer. Map-reduce is slated for a full post-release refactor; routing
        # it through the resolved chat_llm is part of that work.
        async with get_llm_semaphore():
            try:
                resp = await self._llm.chat(
                    [
                        {"role": "system", "content": _MAP_SYSTEM_PROMPT},
                        {"role": "user", "content": _MAP_USER_PROMPT.format(query=query, content=doc.page_content)},
                    ],
                    max_tokens=512,
                    temperature=0.3,
                    response_format={"type": "json_object"},
                )
                data = json.loads(_json_slice(resp["choices"][0]["message"]["content"]))
                return bool(data.get("relevancy", False)), str(data.get("summary", "") or "")
            except Exception as e:
                logger.error("Error during chunk relevancy inference", error=str(e))
                return False, ""

    async def _map_reduce(self, query: str, docs: list) -> list:
        """LLM relevancy filter + summarisation, batched with early stop."""

        async def _batch(chunks: list, summaries: list) -> bool:
            outputs = await asyncio.gather(*[self._infer_relevancy(query, c) for c in chunks])
            terminate = all(not rel for rel, _ in outputs[-self._mr_expansion :])
            for (rel, summary), chunk in zip(outputs, chunks, strict=True):
                if rel:
                    summaries.append(_summary_doc(chunk, summary))
            return terminate

        summaries: list = []
        initial, remaining = docs[: self._mr_initial], docs[self._mr_initial :]
        terminate = await _batch(initial, summaries)
        if terminate or not remaining or len(summaries) >= self._mr_max:
            return summaries

        for i in range(0, len(remaining), self._mr_expansion):
            n = min(self._mr_expansion, self._mr_max - len(summaries))
            if n <= 0:
                break
            terminate = await _batch(remaining[i : i + n], summaries)
            if terminate or len(summaries) >= self._mr_max:
                break
        logger.debug("Map reduce completed", relevant_chunks_count=len(summaries))
        return summaries

    # ------------------------------------------------------------------
    # Preparation (was RagPipeline._prepare_for_chat_completion)
    # ------------------------------------------------------------------

    async def _prepare_chat(
        self,
        partition: list[str] | None,
        payload: dict,
        llm: LLM | None = None,
        trace: RetrievalTraceBuilder | None = None,
    ):
        messages = payload["messages"][-self._resolve_chat_history_depth(partition) :]
        custom_prompt, messages = _split_leading_system_prompt(payload["messages"], messages)
        if not messages:
            raise ValidationError("Request must contain at least one non-system message")

        metadata = payload.get("metadata") or {}
        use_map_reduce = metadata.get("use_map_reduce", False)
        spoken_style = metadata.get("spoken_style_answer", False)
        use_websearch = metadata.get("websearch", False)
        workspace = metadata.get("workspace")
        attachment_ids = _extract_attachment_ids(metadata)

        top_k = self._mr_max if use_map_reduce else None

        filter_params = None
        indexed_attachment_ids: list[str] = []
        if workspace and partition:
            scope = await self._workspace.resolve_scope(workspace, partition)
            if scope is None:
                raise WorkspaceNotFoundError(f"Workspace '{workspace}' not found.")
            # A workspace belongs to exactly one partition — narrow retrieval to
            # it even for an "openrag-all" / multi-partition request, otherwise
            # file_id-only filtering could match a same-named file in another
            # partition the caller also has access to (#706).
            partition = [scope.partition]
            filter_params = {"file_id": scope.file_ids}
        elif attachment_ids and partition:
            # No ownership check needed: file_id is ANDed with the server-fixed
            # partition (or, for the "all" wildcard, SUPER_ADMIN_MODE-only).
            indexed_attachment_ids = await self._existing_file_ids(attachment_ids, partition)
            filter_params = {"file_id": indexed_attachment_ids}

        last_user_message = messages[-1].get("content") or ""
        casual_policy = casual_message_policy(last_user_message)
        explicitly_required = metadata.get("require_retrieval") is True
        existing_force_retrieval = use_websearch or use_map_reduce or bool(indexed_attachment_ids)

        retrieval_forced = explicitly_required or existing_force_retrieval
        queries: SearchQueries | None = None

        if casual_policy is None or retrieval_forced:
            queries = await self.generate_query(messages, llm=llm, partition=partition, trace=trace)
            usable_queries = [query for query in queries.query_list if query.query.strip()]
            contextualizer_found_casual = (
                not retrieval_forced
                and not queries.requires_retrieval
                and queries.intent in {"greeting", "gratitude", "farewell", "capability"}
                and not queries.query_list
            )
            if contextualizer_found_casual:
                language = detect_language(
                    last_user_message,
                    min_confidence=_CASUAL_LANGUAGE_MIN_CONFIDENCE,
                )
                casual_policy = CasualMessagePolicy(queries.intent, language or "en")
            elif not usable_queries:
                queries = SearchQueries(query_list=[Query(query=last_user_message)])
                self._mark_contextualization_fallback(trace, queries)
            elif len(usable_queries) != len(queries.query_list):
                queries = queries.model_copy(update={"query_list": usable_queries})

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
            casual_prompt = build_casual_response_prompt(casual_policy.intent, casual_policy.language)
            payload["messages"] = prepend_system_prompt(
                messages,
                casual_prompt,
                context="",
                current_date=datetime.now().strftime("%A, %B %d, %Y, %H:%M:%S"),
                custom_prompt=custom_prompt,
            )
            return _PrepareChatResult(payload, [], [], [], [], True, indexed_attachment_ids)

        if queries is None:  # pragma: no cover - guarded by the casual return above
            queries = SearchQueries(query_list=[Query(query=last_user_message)])

        web_results: list = []
        if partition is not None and use_websearch:
            chunks, web_lists = await self._gather_rag_and_web(
                queries,
                partition,
                top_k,
                filter_params,
                trace=trace,
            )
            web_results = _dedupe_web(web_lists)
        elif partition is not None:
            trace_kwargs = {"trace": trace} if trace is not None else {}
            chunks = await self._retrieval.retrieve_multi(
                partitions=partition,
                search_queries=queries,
                top_k=top_k,
                filter_params=filter_params,
                **trace_kwargs,
            )
        else:
            web_results = _dedupe_web(await asyncio.gather(*[self._web.search(q.query) for q in queries.query_list]))
            chunks = []

        if (
            trace is not None
            and partition is not None
            and metadata.get("compare_original_query") is True
        ):
            await self._compare_original_query(
                trace,
                original_query=last_user_message,
                partition=partition,
                top_k=top_k,
                filter_params=filter_params,
            )

        if not chunks and not web_results and partition is None:
            return _PrepareChatResult(payload, [], [], [], [], False, indexed_attachment_ids)

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

        if use_map_reduce and docs:
            docs = await self._map_reduce(" ".join(q.query for q in queries.query_list), docs)

        web_formatted, web_source_numbers, web_tokens = "", [], 0
        web_start_index = 1
        if web_results:
            web_formatted, web_source_numbers, web_tokens = format_web_context(
                web_results,
                length_function=get_num_tokens(),
                start_index=web_start_index,
                max_tokens=self._web.max_tokens,
            )
        context, included = format_context(
            [doc.page_content for doc in docs],
            max_context_tokens=self._max_context_tokens - web_tokens,
            length_function=get_num_tokens(),
        )
        docs = [docs[i] for i in included]

        if web_results:
            if docs:
                web_start_index = len(docs) + 1
                web_formatted, web_source_numbers, _ = format_web_context(
                    web_results,
                    length_function=get_num_tokens(),
                    start_index=web_start_index,
                    max_tokens=self._web.max_tokens,
                )
            else:
                context = ""
            context = f"{context}{SOURCE_SEPARATOR}{web_formatted}" if context else web_formatted
            web_results = [web_results[number - web_start_index] for number in web_source_numbers]

        prompt_type = "spoken_style_answer" if spoken_style else "sys_prompt"
        tmpl = await self._prompt_service.resolve_prompt(
            prompt_type, names=[self._generation_prompt_name(prompt_type, partition)]
        )
        new_messages = prepend_system_prompt(
            messages,
            tmpl,
            context=context,
            current_date=datetime.now().strftime("%A, %B %d, %Y, %H:%M:%S"),
            custom_prompt=custom_prompt,
        )
        payload["messages"] = new_messages
        return _PrepareChatResult(
            payload, docs, web_results, retrieved_docs, retrieved_web_results, True, indexed_attachment_ids
        )

    async def _existing_file_ids(self, file_ids: list[str], partitions: list[str]) -> list[str]:
        """Order-preserving, deduplicated subset of ``file_ids`` indexed in ``partitions``.

        ``"all"`` (``SUPER_ADMIN_MODE`` wildcard) takes an unscoped lookup instead
        of a per-partition one.
        """
        if "all" in partitions:
            if len(partitions) > 1:
                raise ValueError("`partitions` cannot mix the wildcard with explicit values.")
            found = set(await self._workspace.get_existing_file_ids_any_partition(file_ids))
        else:
            results = await asyncio.gather(*(self._workspace.get_existing_file_ids(p, file_ids) for p in partitions))
            found = {fid for r in results for fid in r}
        return [fid for fid in dict.fromkeys(file_ids) if fid in found]

    async def _gather_rag_and_web(self, queries, partition, top_k, filter_params, trace=None):
        # Fuse the doc branch through retrieve_multi so a partition's rrf_k drives
        # its sub-query fusion here too (#707). Previously this used
        # retrieve_per_query + fuse() at the hardcoded 60, so enabling websearch
        # silently ignored rrf_k and the same preset fused differently depending
        # on the websearch toggle.
        trace_kwargs = {"trace": trace} if trace is not None else {}
        rag = self._retrieval.retrieve_multi(
            partitions=partition,
            search_queries=queries,
            top_k=top_k,
            filter_params=filter_params,
            **trace_kwargs,
        )
        web = asyncio.gather(*[self._web.search(q.query) for q in queries.query_list])
        chunks, web_lists = await asyncio.gather(rag, web)
        return chunks, web_lists

    @staticmethod
    def _mark_contextualization_fallback(
        trace: RetrievalTraceBuilder | None,
        queries: SearchQueries,
    ) -> None:
        if trace is None or trace.contextualization is None:
            return
        trace.contextualization = trace.contextualization.model_copy(
            update={
                "fallback_used": True,
                "subqueries": [
                    ContextualizedSubqueryTrace(query=query.query, temporal_filters=[])
                    for query in queries.query_list
                ],
            }
        )

    async def _compare_original_query(
        self,
        trace: RetrievalTraceBuilder,
        *,
        original_query: str,
        partition: list[str],
        top_k: int | None,
        filter_params: dict | None,
    ) -> None:
        """Run an isolated diagnostic retrieval that cannot alter the answer."""
        shadow = RetrievalTraceBuilder(request_id=f"{trace.request_id}:original", original_query=original_query)
        started = time.perf_counter()
        status = "complete"
        try:
            await self._retrieval.retrieve_multi(
                partitions=partition,
                search_queries=SearchQueries(query_list=[Query(query=original_query)]),
                top_k=top_k,
                filter_params=filter_params,
                trace=shadow,
            )
        except Exception as error:  # noqa: BLE001 - comparison is optional telemetry
            status = "error"
            shadow.record_error("original_query", error)
        shadow.timings["total"] = time.perf_counter() - started
        finished = shadow.finish(configuration_fingerprint=self._configuration_fingerprint(partition))
        trace.comparisons["original_query"] = {
            "status": status,
            "stages": finished["stages"],
            "timings": finished["timings"],
            "errors": finished["errors"],
            "configuration_fingerprint": finished["configuration_fingerprint"],
        }

    def _configuration_fingerprint(self, partitions: list[str] | None) -> str:
        if not partitions:
            return "unavailable"
        fingerprint = getattr(self._retrieval, "configuration_fingerprint", None)
        if fingerprint is None:
            return "unavailable"
        try:
            return str(fingerprint(partitions))
        except Exception:  # noqa: BLE001 - telemetry must never fail a request
            return "unavailable"

    async def _prepare_completions(self, partition: list[str], payload: dict, llm: LLM | None = None):
        prompt = payload["prompt"]
        metadata = payload.get("metadata") or {}
        # partition= is ours: the retrieval preset's query_contextualizer is
        # resolved per partition. The skip below is from #807.
        queries = await self.generate_query([{"role": "user", "content": prompt}], llm=llm, partition=partition)
        retrieved_docs: list = []
        if not queries.query_list:
            if not queries.requires_retrieval and metadata.get("require_retrieval") is not True:
                docs, context = [], ""
            else:
                queries = SearchQueries(query_list=[Query(query=prompt)])
        if queries.query_list:
            chunks = await self._retrieval.retrieve_multi(partitions=partition, search_queries=queries)
            docs = [c.to_langchain() for c in chunks]
            # Full retrieval set before the token-budget selection below, kept
            # separately for `all_retrieved_sources` (#847).
            retrieved_docs = docs
            context, included = format_context(
                [doc.page_content for doc in docs],
                max_context_tokens=self._max_context_tokens,
                length_function=get_num_tokens(),
            )
            docs = [docs[i] for i in included]

        prompt_type = "spoken_style_answer" if metadata.get("spoken_style_answer", False) else "sys_prompt"
        tmpl = await self._prompt_service.resolve_prompt(
            prompt_type, names=[self._generation_prompt_name(prompt_type, partition)]
        )
        instructions = tmpl.format(
            context=context,
            current_date=datetime.now().strftime("%A, %B %d, %Y, %H:%M:%S"),
            custom_prompt="",
        )
        payload["prompt"] = f"{instructions}\n\n# User request\n{prompt}"
        return payload, docs, retrieved_docs

    # ------------------------------------------------------------------
    # Message sanitization
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_messages(messages: list[dict]) -> list[dict]:
        """Replace empty-content ``role="assistant"`` messages with a placeholder.

        Some LLMs reject an assistant turn with no content. Substituting a
        placeholder preserves role alternation (no consecutive same-role
        messages are created). This should normally never happen, but we do it
        as a safeguard against a client sending a malformed history.

        An assistant message carrying ``tool_calls`` / ``function_call`` is
        legitimately content-free and is left untouched.
        """

        def _is_empty_content(content: object) -> bool:
            if content is None:
                return True
            if isinstance(content, str):
                return not content.strip()
            if isinstance(content, list):
                return not content
            return False

        result = []
        replaced_count = 0
        for msg in messages:
            should_replace_msg = (
                msg.get("role") == "assistant"
                and not msg.get("tool_calls")
                and not msg.get("function_call")
                and _is_empty_content(msg.get("content"))
            )
            if should_replace_msg:
                result.append({**msg, "content": "NO_CONTENT"})
                replaced_count += 1
            else:
                result.append(msg)
        if replaced_count:
            logger.warning(
                "Replaced empty assistant message(s) with NO_CONTENT placeholder before LLM call — "
                "this should normally never happen; likely a client-side history persistence "
                "or streaming response issue",
                replaced_count=replaced_count,
            )
        return result

    # ------------------------------------------------------------------
    # Public API (router = transport)
    # ------------------------------------------------------------------

    async def chat(
        self,
        *,
        partitions: list[str] | None,
        payload: dict,
        prepare_sources: PrepareSources,
        model_name: str,
    ) -> dict:
        """Non-streaming chat completion → finalized OpenAI dict."""
        request_started = time.perf_counter()
        metadata = payload.get("metadata") or {}
        include_all_retrieved = metadata.get("include_all_retrieved_sources") is True
        include_trace = metadata.get("include_retrieval_trace") is True
        original_query = _latest_user_query(payload.get("messages", []))
        trace = (
            RetrievalTraceBuilder(request_id=str(uuid.uuid4()), original_query=original_query)
            if include_trace
            else None
        )
        llm = self._resolve_llm(partitions)
        citation_protocol_active = False
        if partitions is None and not metadata.get("websearch", False):
            docs, web_results, retrieved_docs, retrieved_web_results = [], [], [], []
            attachments: list[str] = []
        else:
            result = await self._prepare_chat(partitions, payload, llm, trace=trace)
            payload = result.payload
            docs = result.docs
            web_results = result.web_results
            retrieved_docs = result.retrieved_docs
            retrieved_web_results = result.retrieved_web_results
            citation_protocol_active = result.citation_protocol_active
            attachments = result.indexed_attachment_ids
        sources = prepare_sources(docs, web_results)
        # `all_retrieved_sources` is debug/eval telemetry, not needed by most
        # callers — skip building it (and calling prepare_sources on the full,
        # uncapped retrieval set) unless the caller opted in (#847 review).
        all_sources = prepare_sources(retrieved_docs, retrieved_web_results) if include_all_retrieved else None
        structured_output = _is_structured_output(payload)

        payload["messages"] = self._sanitize_messages(payload["messages"])
        chunk = await llm.chat(payload["messages"], **_sampling(payload))
        chunk["model"] = model_name
        content = chunk.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        if citation_protocol_active and not structured_output:
            clean, citations = extract_and_strip_sources_block(
                content,
                include_inline_markers=bool(sources),
            )
        else:
            clean, citations = content, None
        chunk["choices"][0]["message"]["content"] = clean
        extra = _build_extra_payload(sources, citations, all_sources, include_all_retrieved=include_all_retrieved)
        if metadata.get("attachments"):
            # Indicate which attachments were actually searched to generate the answer.
            extra["attachments"] = attachments
        if trace is not None:
            if trace.contextualization is None:
                self._record_contextualization(
                    trace,
                    SearchQueries(query_list=[Query(query=original_query)]),
                    original_query=original_query,
                )
            trace.timings["total"] = time.perf_counter() - request_started
            extra["retrieval_trace"] = trace.finish(
                configuration_fingerprint=self._configuration_fingerprint(partitions)
            )
        chunk["extra"] = extra
        return chunk

    async def chat_stream(
        self,
        *,
        partitions: list[str] | None,
        payload: dict,
        prepare_sources: PrepareSources,
        model_name: str,
    ) -> AsyncIterator[str]:
        """Streaming chat completion → SSE strings with filtered sources."""
        request_started = time.perf_counter()
        metadata = payload.get("metadata") or {}
        include_all_retrieved = metadata.get("include_all_retrieved_sources") is True
        include_trace = metadata.get("include_retrieval_trace") is True
        original_query = _latest_user_query(payload.get("messages", []))
        trace = (
            RetrievalTraceBuilder(request_id=str(uuid.uuid4()), original_query=original_query)
            if include_trace
            else None
        )
        llm = self._resolve_llm(partitions)
        citation_protocol_active = False
        if partitions is None and not metadata.get("websearch", False):
            docs, web_results, retrieved_docs, retrieved_web_results = [], [], [], []
            attachments: list[str] = []
        else:
            result = await self._prepare_chat(partitions, payload, llm, trace=trace)
            payload = result.payload
            docs = result.docs
            web_results = result.web_results
            retrieved_docs = result.retrieved_docs
            retrieved_web_results = result.retrieved_web_results
            citation_protocol_active = result.citation_protocol_active
            attachments = result.indexed_attachment_ids
        sources = prepare_sources(docs, web_results)
        all_sources = prepare_sources(retrieved_docs, retrieved_web_results) if include_all_retrieved else None
        structured_output = _is_structured_output(payload)

        extra_fields = {"attachments": attachments} if metadata.get("attachments") else {}
        terminal_extra_fields = None
        if trace is not None:
            if trace.contextualization is None:
                self._record_contextualization(
                    trace,
                    SearchQueries(query_list=[Query(query=original_query)]),
                    original_query=original_query,
                )
            trace.timings["total"] = time.perf_counter() - request_started
            terminal_extra_fields = {
                "retrieval_trace": trace.finish(
                    configuration_fingerprint=self._configuration_fingerprint(partitions)
                )
            }

        payload["messages"] = self._sanitize_messages(payload["messages"])
        llm_stream = llm.stream_chat(payload["messages"], **_sampling(payload))
        async for sse_line in stream_with_source_filtering(
            llm_stream,
            sources,
            model_name,
            all_sources=all_sources,
            include_all_retrieved=include_all_retrieved,
            citation_protocol_active=citation_protocol_active and not structured_output,
            extra_fields=extra_fields or None,
            terminal_extra_fields=terminal_extra_fields,
        ):
            yield sse_line

    async def complete(
        self,
        *,
        partitions: list[str] | None,
        payload: dict,
        prepare_sources: PrepareSources,
    ) -> dict:
        """Non-streaming text completion → finalized OpenAI dict."""
        metadata = payload.get("metadata") or {}
        include_all_retrieved = metadata.get("include_all_retrieved_sources") is True
        llm = self._resolve_llm(partitions)
        citation_protocol_active = partitions is not None
        if partitions is None:
            docs, retrieved_docs = [], []
        else:
            payload, docs, retrieved_docs = await self._prepare_completions(partitions, payload, llm)
        sources = prepare_sources(docs, [])
        all_sources = prepare_sources(retrieved_docs, []) if include_all_retrieved else None
        structured_output = _is_structured_output(payload)

        resp = await llm.generate(payload["prompt"], **_sampling(payload, key="prompt"))
        text = resp.get("choices", [{}])[0].get("text", "") or ""
        if citation_protocol_active and not structured_output:
            clean, citations = extract_and_strip_sources_block(
                text,
                include_inline_markers=bool(sources),
            )
        else:
            clean, citations = text, None
        resp["choices"][0]["text"] = clean
        resp["extra"] = _build_extra_payload(
            sources, citations, all_sources, include_all_retrieved=include_all_retrieved
        )
        return resp


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _json_slice(text: str) -> str:
    """Best-effort extract the first JSON object from an LLM response."""
    start = text.find("{")
    end = text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def _latest_user_query(messages: list[dict]) -> str:
    """Return the latest user text without retaining any other message content."""
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else ""
    return ""


def _summary_doc(chunk, summary: str):
    """A summarised copy of a LangChain Document (page_content replaced)."""
    return chunk.__class__(page_content=summary, metadata=chunk.metadata)


def _split_leading_system_prompt(raw_messages: list[dict], truncated: list[dict]) -> tuple[str | None, list[dict]]:
    """Pull a client-pinned leading system prompt out of ``raw_messages``.

    A leading run of ``role="system"`` messages in ``raw_messages`` (the
    untruncated payload) is a pinned instruction, not a chat turn. ``truncated``
    is a tail slice of ``raw_messages`` (``raw_messages[-depth:]``), so only the
    portion of that leading run still inside the tail is stripped from it — a
    system message elsewhere in history that merely lands first after
    chat_history_depth truncation is never mistaken for the pin and dropped.

    ``content`` is read defensively: ``OpenAIMessage`` allows a null/absent
    content (the assistant turn carrying ``tool_calls``), and the router dumps
    with ``exclude_none=True``, so the key is genuinely optional. A content-free
    system message still counts toward the leading run — it is stripped from the
    history like its siblings — it just contributes nothing to the pin.
    """
    parts: list[str] = []
    i = 0
    while i < len(raw_messages) and raw_messages[i]["role"] == "system":
        content = raw_messages[i].get("content")
        if content:
            parts.append(content)
        i += 1

    offset = len(raw_messages) - len(truncated)
    strip = max(0, i - offset)

    return ("\n\n".join(parts) if parts else None), truncated[strip:]


def _extract_attachment_ids(metadata: dict) -> list[str]:
    """file_ids from ``metadata.attachments = [{"id": ...}, ...]``; malformed payloads dropped."""
    raw = metadata.get("attachments")
    if not isinstance(raw, list):
        return []
    return [a["id"] for a in raw if isinstance(a, dict) and isinstance(a.get("id"), str) and a["id"]]


def _dedupe_web(web_lists: list[list]) -> list:
    seen: set[str] = set()
    out: list = []
    for r in (r for lst in web_lists for r in lst):
        url = normalize_web_url(r.url)
        if url is not None and url not in seen:
            r.url = url
            seen.add(url)
            out.append(r)
    return out


def _sampling(payload: dict, key: str = "messages") -> dict:
    """Sampling kwargs handed to the core LLM (everything but the body).

    Mirrors the legacy ``_LLMShim``: strip the transport keys; the core
    ``VLLMClient`` consumes ``metadata`` (llm_override) and the rest as
    OpenAI sampling params.
    """
    drop = {key, "stream", "model"}
    return {k: v for k, v in payload.items() if k not in drop}


def _is_structured_output(payload: dict) -> bool:
    """Structured output cannot carry the plain-text citation marker."""
    response_format = payload.get("response_format")
    return isinstance(response_format, dict) and response_format.get("type") in {"json_object", "json_schema"}


def _build_extra_payload(
    sources: list,
    citations: set[int] | None,
    all_sources: list | None,
    *,
    include_all_retrieved: bool,
) -> dict:
    """Shared ``extra`` shape for ``chat``/``complete`` (mirrors
    ``stream_with_source_filtering``'s payload, minus the streaming-only
    ``truncated`` flag) so the three response paths can't drift apart.
    """
    filtered = filter_sources_by_citations(sources, citations)
    payload = {
        "sources": filtered,
        "presented_sources": sources,
        "cited_sources": filtered if citations is not None else [],
        "citations_reported": citations is not None,
    }
    if include_all_retrieved:
        payload["all_retrieved_sources"] = all_sources
    return payload


__all__ = ["QueryService", "RAGMODE"]
