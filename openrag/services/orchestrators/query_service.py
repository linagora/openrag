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

from core.models.query import Query, SearchQueries
from core.prompts import (
    SOURCE_SEPARATOR,
    format_context,
    format_web_context,
    load_template_by_key,
)
from core.utils.logging import get_logger
from core.utils.source_filtering import (
    extract_and_strip_sources_block,
    filter_sources_by_citations,
    stream_with_source_filtering,
)
from core.utils.text import get_num_tokens
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
    "\n\nRespond ONLY with a JSON object of the form "
    '{"query_list": [{"query": "<search query>", "temporal_filters": null}]}.'
)


class RAGMODE(Enum):
    SIMPLERAG = "SimpleRag"
    CHATBOTRAG = "ChatBotRag"


class QueryService:
    """End-to-end RAG: query-gen → retrieve (+web) → map-reduce → answer."""

    def __init__(
        self,
        *,
        retrieval_service: RetrievalService,
        llm: LLM,
        config: Settings,
        web_search_service: Any | None,
        workspace_service: WorkspaceService,
    ) -> None:
        self._retrieval = retrieval_service
        self._llm = llm
        self._web = web_search_service
        self._workspace = workspace_service

        self._rag_mode = config.rag.mode
        self._chat_history_depth = config.rag.chat_history_depth
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

    # ------------------------------------------------------------------
    # Query generation (was RagPipeline.generate_query — no LangChain)
    # ------------------------------------------------------------------

    async def generate_query(self, messages: list[dict]) -> SearchQueries:
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
                resp = await self._llm.chat(llm_messages, **params)
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

    async def _prepare_chat(self, partition: list[str] | None, payload: dict):
        messages = payload["messages"][-self._chat_history_depth :]
        queries = await self.generate_query(messages)

        metadata = payload.get("metadata") or {}
        use_map_reduce = metadata.get("use_map_reduce", False)
        spoken_style = metadata.get("spoken_style_answer", False)
        use_websearch = metadata.get("websearch", False)
        workspace = metadata.get("workspace")

        top_k = self._mr_max if use_map_reduce else None

        if workspace:
            ws = await self._workspace.get_workspace(workspace)
            if not ws or ("all" not in partition and ws["partition_name"] not in partition):
                logger.warning("Workspace not found in partition(s) — ignoring", workspace=workspace)
                workspace = None
        filter_params = {"workspace_id": workspace} if workspace else None

        web_results: list = []
        if partition is not None and use_websearch:
            doc_lists, web_lists = await self._gather_rag_and_web(queries.query_list, partition, top_k, filter_params)
            chunks = self._retrieval.fuse(doc_lists, top_k=top_k)
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

        web_formatted, web_tokens = "", 0
        if web_results:
            web_formatted, _, web_tokens = format_web_context(
                web_results,
                length_function=get_num_tokens(),
                start_index=1,
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
                web_formatted, _, _ = format_web_context(
                    web_results,
                    length_function=get_num_tokens(),
                    start_index=len(docs) + 1,
                    max_tokens=self._web.max_tokens,
                )
            else:
                context = ""
            context = f"{context}{SOURCE_SEPARATOR}{web_formatted}" if context else web_formatted

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

    async def _gather_rag_and_web(self, query_list, partition, top_k, filter_params):
        rag = self._retrieval.retrieve_per_query(
            partitions=partition, queries=query_list, top_k=top_k, filter_params=filter_params
        )
        web = asyncio.gather(*[self._web.search(q.query) for q in query_list])
        doc_lists, web_lists = await asyncio.gather(rag, web)
        return doc_lists, web_lists

    async def _prepare_completions(self, partition: list[str], payload: dict):
        prompt = payload["prompt"]
        queries = await self.generate_query([{"role": "user", "content": prompt}])
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
        """Sanitize the message list before forwarding to the LLM.

        Replaces ``role="assistant"`` messages whose content is absent or
        whitespace-only with the placeholder ``"NO_CONTENT"``.  This preserves
        role alternation (no consecutive same-role messages are created) while
        acting as a defensive guard against upstream bugs (broken streaming
        response or client-side history persistence).  Logged at ERROR level so
        the root cause is traceable.

        Non-string content (e.g. multimodal list) is never replaced.
        """
        result = []
        patched_count = 0
        for msg in messages:
            content = msg.get("content")
            if msg.get("role") == "assistant" and (
                content is None or (isinstance(content, str) and not content.strip())
            ):
                result.append({**msg, "content": "NO_CONTENT"})
                patched_count += 1
            else:
                result.append(msg)
        if patched_count:
            logger.error(
                "Replaced empty assistant message(s) with NO_CONTENT placeholder before LLM call — "
                "this should never happen, investigate upstream "
                "(streaming response code or client-side history persistence)",
                patched_count=patched_count,
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
        if partitions is None and not metadata.get("websearch", False):
            docs, web_results = [], []
        else:
            payload, docs, web_results = await self._prepare_chat(partitions, payload)
        sources = prepare_sources(docs, web_results)

        payload["messages"] = self._sanitize_messages(payload["messages"])
        chunk = await self._llm.chat(payload["messages"], **_sampling(payload))
        chunk["model"] = model_name
        content = chunk.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        clean, citations = extract_and_strip_sources_block(content)
        chunk["choices"][0]["message"]["content"] = clean
        chunk["extra"] = json.dumps({"sources": filter_sources_by_citations(sources, citations)})
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
        if partitions is None and not metadata.get("websearch", False):
            docs, web_results = [], []
        else:
            payload, docs, web_results = await self._prepare_chat(partitions, payload)
        sources = prepare_sources(docs, web_results)

        payload["messages"] = self._sanitize_messages(payload["messages"])
        llm_stream = self._llm.stream_chat(payload["messages"], **_sampling(payload))
        async for sse_line in stream_with_source_filtering(llm_stream, sources, model_name):
            yield sse_line

    async def complete(
        self,
        *,
        partitions: list[str] | None,
        payload: dict,
        prepare_sources: PrepareSources,
    ) -> dict:
        """Non-streaming text completion → finalized OpenAI dict."""
        if partitions is None:
            docs = []
        else:
            payload, docs = await self._prepare_completions(partitions, payload)
        sources = prepare_sources(docs, [])

        resp = await self._llm.generate(payload["prompt"], **_sampling(payload, key="prompt"))
        text = resp.get("choices", [{}])[0].get("text", "") or ""
        clean, citations = extract_and_strip_sources_block(text)
        resp["choices"][0]["text"] = clean
        resp["extra"] = json.dumps({"sources": filter_sources_by_citations(sources, citations)})
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
        if r.url not in seen:
            seen.add(r.url)
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


__all__ = ["QueryService", "RAGMODE"]
