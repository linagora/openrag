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
import copy
import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from core.models.preset import resolve_partition_chat_llm
from core.models.query import Query, SearchQueries
from core.prompts import (
    SOURCE_SEPARATOR,
    format_context,
    format_web_context,
    load_template_by_key,
    prepend_system_prompt,
)
from core.utils.exceptions import WorkspaceNotFoundError
from core.utils.logging import get_logger
from core.utils.source_filtering import (
    extract_and_strip_sources_block,
    filter_sources_by_citations,
    stream_with_source_filtering,
)
from core.utils.text import get_num_tokens
from core.utils.web_url import normalize_web_url
from services.inference.runtime import detect_language, get_llm_semaphore

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.llm.llm import LLM
    from services.orchestrators.retrieval_service import RetrievalService
    from services.orchestrators.workspace_service import WorkspaceService

logger = get_logger()

PrepareSources = Callable[[list, list], list]

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
    "\n\nRespond ONLY with one of these JSON forms: "
    '{"requires_retrieval": false, "query_list": []} when retrieval is not needed, or '
    '{"requires_retrieval": true, '
    '"query_list": [{"query": "<search query>", "temporal_filters": null}]} when it is.'
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
        llm_factory: Callable[[str], LLM] | None = None,
    ) -> None:
        self._retrieval = retrieval_service
        self._llm = llm
        self._llm_factory = llm_factory
        self._web = web_search_service
        self._workspace = workspace_service

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
        self._max_context_tokens = config.reranker.top_k * config.chunker.chunk_size

        mr = config.map_reduce
        self._mr_initial = mr.initial_batch_size
        self._mr_expansion = mr.expansion_batch_size
        self._mr_max = mr.max_total_documents

        prompts_dir, mapping = config.paths.prompts_dir, config.prompts
        self._query_contextualizer_prompt = load_template_by_key(prompts_dir, mapping, "query_contextualizer")
        self._spoken_style_answer_prompt = load_template_by_key(prompts_dir, mapping, "spoken_style_answer")
        self._sys_prompt_tmplt = load_template_by_key(prompts_dir, mapping, "sys_prompt")

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

    async def generate_query(self, messages: list[dict], llm: LLM | None = None) -> SearchQueries:
        llm = llm or self._llm
        last_user = messages[-1]["content"]
        if RAGMODE(self._rag_mode) is RAGMODE.SIMPLERAG:
            return SearchQueries(query_list=[Query(query=last_user)])

        chat_history = "".join(f"{m['role']}: {m['content']}\n" for m in messages)
        prompt = self._query_contextualizer_prompt.format(
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
        for attempt in (1, 2):
            try:
                resp = await llm.chat(llm_messages, **params)
                content = resp["choices"][0]["message"]["content"]
                return SearchQueries.model_validate_json(_json_slice(content))
            except Exception as exc:
                if attempt == 1:
                    logger.warning("Query generation parse error — retrying", error=str(exc))
                else:
                    logger.warning(
                        "Query generation failed twice — falling back to raw user query",
                        error=str(exc),
                    )
        return SearchQueries(query_list=[Query(query=last_user)])

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

    async def _prepare_chat(self, partition: list[str] | None, payload: dict, llm: LLM | None = None):
        messages = payload["messages"][-self._resolve_chat_history_depth(partition) :]
        queries = await self.generate_query(messages, llm=llm)

        metadata = payload.get("metadata") or {}
        use_map_reduce = metadata.get("use_map_reduce", False)
        spoken_style = metadata.get("spoken_style_answer", False)
        use_websearch = metadata.get("websearch", False)
        workspace = metadata.get("workspace")

        top_k = self._mr_max if use_map_reduce else None

        filter_params = None
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

        force_retrieval = use_websearch or use_map_reduce
        if not queries.query_list:
            if not queries.requires_retrieval and not force_retrieval:
                tmpl = self._spoken_style_answer_prompt if spoken_style else self._sys_prompt_tmplt
                payload["messages"] = prepend_system_prompt(
                    messages,
                    tmpl,
                    context="",
                    current_date=datetime.now().strftime("%A, %B %d, %Y, %H:%M:%S"),
                )
                return payload, [], []
            queries = SearchQueries(query_list=[Query(query=messages[-1]["content"])])

        web_results: list = []
        if partition is not None and use_websearch:
            chunks, web_lists = await self._gather_rag_and_web(queries, partition, top_k, filter_params)
            web_results = _dedupe_web(web_lists)
        elif partition is not None:
            chunks = await self._retrieval.retrieve_multi(
                partitions=partition, search_queries=queries, top_k=top_k, filter_params=filter_params
            )
        else:
            web_results = _dedupe_web(await asyncio.gather(*[self._web.search(q.query) for q in queries.query_list]))
            chunks = []

        if not chunks and not web_results and partition is None:
            return payload, [], []

        docs = [c.to_langchain() for c in chunks]
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

        new_messages = copy.deepcopy(messages)
        tmpl = self._spoken_style_answer_prompt if spoken_style else self._sys_prompt_tmplt
        new_messages.insert(
            0,
            {
                "role": "system",
                "content": tmpl.format(
                    context=context, current_date=datetime.now().strftime("%A, %B %d, %Y, %H:%M:%S")
                ),
            },
        )
        payload["messages"] = new_messages
        return payload, docs, web_results

    async def _gather_rag_and_web(self, queries, partition, top_k, filter_params):
        # Fuse the doc branch through retrieve_multi so a partition's rrf_k drives
        # its sub-query fusion here too (#707). Previously this used
        # retrieve_per_query + fuse() at the hardcoded 60, so enabling websearch
        # silently ignored rrf_k and the same preset fused differently depending
        # on the websearch toggle.
        rag = self._retrieval.retrieve_multi(
            partitions=partition, search_queries=queries, top_k=top_k, filter_params=filter_params
        )
        web = asyncio.gather(*[self._web.search(q.query) for q in queries.query_list])
        chunks, web_lists = await asyncio.gather(rag, web)
        return chunks, web_lists

    async def _prepare_completions(self, partition: list[str], payload: dict, llm: LLM | None = None):
        prompt = payload["prompt"]
        queries = await self.generate_query([{"role": "user", "content": prompt}], llm=llm)
        if not queries.query_list:
            if not queries.requires_retrieval:
                return payload, []
            queries = SearchQueries(query_list=[Query(query=prompt)])
        chunks = await self._retrieval.retrieve_multi(partitions=partition, search_queries=queries)
        docs = [c.to_langchain() for c in chunks]
        context, included = format_context(
            [doc.page_content for doc in docs],
            max_context_tokens=self._max_context_tokens,
            length_function=get_num_tokens(),
        )
        docs = [docs[i] for i in included]
        if docs:
            payload["prompt"] = (
                f"Given the content\n{context}\nComplete the following prompt: {prompt}\n"
                "At the very end of your response, on a new line, list which source numbers "
                "you used: [Sources: 1, 3]"
            )
        return payload, docs

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
        metadata = payload.get("metadata") or {}
        llm = self._resolve_llm(partitions)
        if partitions is None and not metadata.get("websearch", False):
            docs, web_results = [], []
        else:
            payload, docs, web_results = await self._prepare_chat(partitions, payload, llm)
        sources = prepare_sources(docs, web_results)

        payload["messages"] = self._sanitize_messages(payload["messages"])
        chunk = await llm.chat(payload["messages"], **_sampling(payload))
        chunk["model"] = model_name
        content = chunk.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        clean, citations = extract_and_strip_sources_block(
            content,
            include_inline_markers=bool(sources),
        )
        chunk["choices"][0]["message"]["content"] = clean
        chunk["extra"] = json.dumps(
            {
                "sources": filter_sources_by_citations(
                    sources,
                    citations,
                    allow_uncited=_allows_uncited_sources(payload),
                )
            }
        )
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
        metadata = payload.get("metadata") or {}
        llm = self._resolve_llm(partitions)
        if partitions is None and not metadata.get("websearch", False):
            docs, web_results = [], []
        else:
            payload, docs, web_results = await self._prepare_chat(partitions, payload, llm)
        sources = prepare_sources(docs, web_results)

        payload["messages"] = self._sanitize_messages(payload["messages"])
        llm_stream = llm.stream_chat(payload["messages"], **_sampling(payload))
        async for sse_line in stream_with_source_filtering(
            llm_stream,
            sources,
            model_name,
            allow_uncited_sources=_allows_uncited_sources(payload),
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
        llm = self._resolve_llm(partitions)
        if partitions is None:
            docs = []
        else:
            payload, docs = await self._prepare_completions(partitions, payload, llm)
        sources = prepare_sources(docs, [])

        resp = await llm.generate(payload["prompt"], **_sampling(payload, key="prompt"))
        text = resp.get("choices", [{}])[0].get("text", "") or ""
        clean, citations = extract_and_strip_sources_block(
            text,
            include_inline_markers=bool(sources),
        )
        resp["choices"][0]["text"] = clean
        resp["extra"] = json.dumps(
            {
                "sources": filter_sources_by_citations(
                    sources,
                    citations,
                    allow_uncited=_allows_uncited_sources(payload),
                )
            }
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


def _summary_doc(chunk, summary: str):
    """A summarised copy of a LangChain Document (page_content replaced)."""
    return chunk.__class__(page_content=summary, metadata=chunk.metadata)


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


def _allows_uncited_sources(payload: dict) -> bool:
    """Structured output cannot carry the plain-text citation marker."""
    response_format = payload.get("response_format")
    return isinstance(response_format, dict) and response_format.get("type") in {"json_object", "json_schema"}


__all__ = ["QueryService", "RAGMODE"]
