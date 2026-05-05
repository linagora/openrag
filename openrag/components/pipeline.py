import asyncio
import copy
from datetime import datetime
from enum import Enum

import openai
import ray
from components.prompts import (
    QUERY_CONTEXTUALIZER_PROMPT,
    SPOKEN_STYLE_ANSWER_PROMPT,
    SYS_PROMPT_TMPLT,
)
from components.ray_utils import call_ray_actor_with_timeout
from components.utils import detect_language, format_context, format_web_context
from components.websearch import WebSearchFactory
from config import load_config
from langchain_core.documents.base import Document
from langchain_core.exceptions import OutputParserException
from langchain_openai import ChatOpenAI
from pydantic import ValidationError
from utils.logger import get_logger

# Phase 5/5.15: domain query model + RetrieverPipeline live in core/. This file
# re-exports them and shims the legacy RetrieverPipeline as an adapter.
from openrag.core.models.chunk import Chunk
from openrag.core.models.query import Query, SearchQueries, TemporalPredicate
from openrag.core.rerankers.reranker import Reranker as _CoreReranker
from openrag.core.retrieval.pipeline import RetrieverPipeline as _CoreRetrieverPipeline

from .llm import LLM
from .map_reduce import RAGMapReduce
from .reranker import BaseReranker, RerankerFactory
from .retriever import BaseRetriever, RetrieverFactory
from .utils import SOURCE_SEPARATOR

logger = get_logger()
config = load_config()
VECTORDB_TIMEOUT = config.ray.indexer.vectordb_timeout

__all__ = [
    "Query",
    "RAGMODE",
    "RagPipeline",
    "RetrieverPipeline",
    "SearchQueries",
    "TemporalPredicate",
]


class RAGMODE(Enum):
    SIMPLERAG = "SimpleRag"
    CHATBOTRAG = "ChatBotRag"


class _LegacyRerankerAdapter(_CoreReranker):
    """Wraps a legacy ``BaseReranker`` (Document-in / Document-out) so it
    satisfies the core ``Reranker`` ABC (str-in / (idx, score)-out).

    The legacy reranker only returns reordered documents; we tag each
    incoming text with its original index via the wrapping Document's
    metadata, then read it back to produce ``(idx, rank-score)`` tuples.
    Score values are synthetic (``1 / (rank+1)``) — only the order is
    consumed by the core pipeline.
    """

    def __init__(self, legacy: BaseReranker) -> None:
        self._legacy = legacy

    async def rerank(self, query: str, documents: list[str], top_k: int | None = None) -> list[tuple[int, float]]:
        tagged = [Document(page_content=t, metadata={"_legacy_rerank_idx": i}) for i, t in enumerate(documents)]
        reordered = await self._legacy.rerank(query=query, documents=tagged, top_k=top_k)
        return [(d.metadata["_legacy_rerank_idx"], 1.0 / (rank + 1)) for rank, d in enumerate(reordered)]


def _to_documents(chunks: list[Chunk]) -> list[Document]:
    return [c.to_langchain() for c in chunks]


class RetrieverPipeline:
    """Backward-compat adapter — delegates to ``core.retrieval.pipeline.RetrieverPipeline``.

    The legacy retriever shim already provides a core ``Retriever``; we
    wrap the legacy reranker in ``_LegacyRerankerAdapter`` and hand both
    to the core pipeline. Outputs are converted ``Chunk → Document`` so
    legacy callers (``RagPipeline``) keep working unchanged.
    """

    def __init__(self) -> None:
        self.retriever: BaseRetriever = RetrieverFactory.create_retriever(config=config)
        self.allow_filterless_fallback = config.retriever.allow_filterless_fallback

        self.reranker_enabled = config.reranker.enabled
        self.reranker: BaseReranker = RerankerFactory.get_reranker(config)
        logger.debug("Reranker", enabled=self.reranker_enabled, provider=config.reranker.provider)
        self.reranker_top_k = config.reranker.top_k

        self._core_pipeline: _CoreRetrieverPipeline | None = None

    def _ensure_core_pipeline(self) -> _CoreRetrieverPipeline:
        # Built lazily so the underlying Ray actor only needs to exist at
        # first request time, not at module import / pipeline construction.
        if self._core_pipeline is None:
            self._core_pipeline = _CoreRetrieverPipeline(
                retriever=self.retriever._build_core_retriever(),
                reranker=_LegacyRerankerAdapter(self.reranker) if self.reranker_enabled else None,
                reranker_top_k=self.reranker_top_k,
                allow_filterless_fallback=self.allow_filterless_fallback,
            )
        return self._core_pipeline

    async def retrieve_docs(
        self,
        partition: list[str],
        query: Query,
        top_k: int | None = None,
        filter_params: dict | None = None,
    ) -> list[Document]:
        chunks = await self._ensure_core_pipeline().retrieve_docs(
            partition=partition, query=query, top_k=top_k, filter_params=filter_params
        )
        logger.debug("Documents retrieved", document_count=len(chunks))
        return _to_documents(chunks)

    async def get_relevant_docs(
        self,
        partition: list[str],
        search_queries: SearchQueries,
        top_k: int | None = None,
        filter_params: dict | None = None,
    ) -> list[Document]:
        chunks = await self._ensure_core_pipeline().get_relevant_docs(
            partition=partition, search_queries=search_queries, top_k=top_k, filter_params=filter_params
        )
        logger.debug("Final relevant documents after RRF reranking", document_count=len(chunks))
        return _to_documents(chunks)


class RagPipeline:
    def __init__(self) -> None:
        # retriever pipeline
        self.retriever_pipeline = RetrieverPipeline()

        # RAG
        self.rag_mode = config.rag.mode
        self.chat_history_depth = config.rag.chat_history_depth
        self.max_context_tokens = config.reranker.top_k * config.chunker.chunk_size

        self.llm_client = LLM(config.llm, logger)

        llm = ChatOpenAI(
            base_url=config.llm.base_url,
            api_key=config.llm.api_key,
            model=config.llm.model,
            temperature=config.llm.temperature,
        )

        primary = llm.with_structured_output(SearchQueries, method="json_schema", strict=True)
        fallback = llm.with_structured_output(SearchQueries, method="function_calling", strict=False)
        self.query_generator = primary.with_fallbacks(
            [fallback],
            exceptions_to_handle=(openai.BadRequestError,),
        )

        self.max_contextualized_query_len = config.rag.max_contextualized_query_len

        # map reduce
        self.map_reduce: RAGMapReduce = RAGMapReduce(config=config)

        # Web search
        self.web_search_service = WebSearchFactory.create_service(config)
        if self.web_search_service.provider:
            logger.info("Web search enabled", provider=config.websearch.provider)
        else:
            logger.info("Web search disabled (WEBSEARCH_API_TOKEN not set)")

    async def generate_query(self, messages: list[dict]) -> SearchQueries:
        match RAGMODE(self.rag_mode):
            case RAGMODE.SIMPLERAG:
                # For SimpleRag, we don't need to contextualize the query as the chat history is not taken into account
                last_msg = messages[-1]
                return SearchQueries(query_list=[Query(query=last_msg["content"])])

            case RAGMODE.CHATBOTRAG:
                # Contextualize the query based on the chat history
                chat_history = ""
                for m in messages:
                    chat_history += f"{m['role']}: {m['content']}\n"

                last_user_query = messages[-1]["content"]
                query_language = detect_language(last_user_query)

                model_kwargs = {
                    "max_completion_tokens": self.max_contextualized_query_len,
                    # "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
                }
                prompt = QUERY_CONTEXTUALIZER_PROMPT.format(
                    query_language=query_language,
                    current_date=datetime.now().strftime("%A, %B %d, %Y, %H:%M:%S"),
                )

                llm_messages = [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"Here is the chat history: \n{chat_history}\n"},
                ]

                # Retry once on schema-validation failure; fall back to the raw user query on the second failure.
                generator = self.query_generator.bind(**model_kwargs)
                for attempt in (1, 2):
                    try:
                        return await generator.ainvoke(llm_messages)
                    except (ValidationError, OutputParserException) as exc:
                        if attempt == 1:
                            logger.warning("Query generation schema error — retrying", error=str(exc))
                        else:
                            logger.warning(
                                "Query generation failed twice — falling back to raw user query",
                                error=str(exc),
                            )
                return SearchQueries(query_list=[Query(query=last_user_query)])

    async def _prepare_for_chat_completion(self, partition: list[str] | None, payload: dict):
        messages = payload["messages"]
        messages = messages[-self.chat_history_depth :]  # limit history depth

        # 1. get the query
        queries: SearchQueries = await self.generate_query(messages)
        logger.debug("Prepared query for chat completion", queries=str(queries))

        metadata = payload.get("metadata") or {}

        use_map_reduce = metadata.get("use_map_reduce", False)
        spoken_style_answer = metadata.get("spoken_style_answer", False)
        use_websearch = metadata.get("websearch", False)
        workspace = metadata.get("workspace")

        logger.debug(
            "Metadata parameters",
            use_map_reduce=use_map_reduce,
            spoken_style_answer=spoken_style_answer,
            use_websearch=use_websearch,
            workspace=workspace,
        )

        # 2. get docs and/or web results concurrently
        top_k = config.map_reduce.max_total_documents if use_map_reduce else None
        if workspace:
            vectordb = ray.get_actor("Vectordb", namespace="openrag")
            ws = await call_ray_actor_with_timeout(
                vectordb.get_workspace.remote(workspace),
                timeout=VECTORDB_TIMEOUT,
                task_description=f"get_workspace({workspace})",
            )
            if not ws or ("all" not in partition and ws["partition_name"] not in partition):
                logger.warning(
                    "Workspace not found in partition(s) — ignoring workspace filter",
                    workspace=workspace,
                    partition=partition,
                )
                workspace = None

        filter_params = {"workspace_id": workspace} if workspace else None

        if partition is not None and use_websearch:
            # Run one retrieval and one web search per sub-query, all concurrently (Option C).
            # Web results from different sub-queries are deduplicated by URL, preserving order.
            rag_tasks = [
                self.retriever_pipeline.retrieve_docs(
                    partition=partition, query=q, top_k=top_k, filter_params=filter_params
                )
                for q in queries.query_list
            ]
            web_tasks = [self.web_search_service.search(q.query) for q in queries.query_list]
            all_results = await asyncio.gather(*rag_tasks, *web_tasks)
            n = len(queries.query_list)
            raw_doc_lists = list(all_results[:n])
            raw_web_lists = list(all_results[n:])
            docs = self.retriever_pipeline.reranker.rrf_reranking(doc_lists=raw_doc_lists)
            if top_k is not None:
                docs = docs[:top_k]
            # Deduplicate web results by URL, preserving first-seen order
            seen_urls: set[str] = set()
            web_results = []
            for result in (r for web_list in raw_web_lists for r in web_list):
                if result.url not in seen_urls:
                    seen_urls.add(result.url)
                    web_results.append(result)
        elif partition is not None:
            docs = await self.retriever_pipeline.get_relevant_docs(
                partition=partition, search_queries=queries, top_k=top_k, filter_params=filter_params
            )
            web_results = []
        else:
            # Web-only mode (partition is None): no RAG retrieval.
            # Run one web search per sub-query concurrently and deduplicate by URL.
            raw_web_lists = await asyncio.gather(*[self.web_search_service.search(q.query) for q in queries.query_list])
            seen_urls = set()
            web_results = []
            for result in (r for web_list in raw_web_lists for r in web_list):
                if result.url not in seen_urls:
                    seen_urls.add(result.url)
                    web_results.append(result)
            docs = []

        # Web-only with no results: fall back to plain direct LLM mode
        if not docs and not web_results and partition is None:
            return payload, [], []

        if use_map_reduce and docs:
            docs = await self.map_reduce.map(query=" ".join(q.query for q in queries.query_list), chunks=docs)

        # 3. Format web results first to know actual token usage, then allocate remaining budget to RAG
        web_formatted = ""
        web_tokens_used = 0
        if web_results:
            web_formatted, _, web_tokens_used = format_web_context(
                web_results, start_index=1, max_tokens=self.web_search_service.max_tokens
            )

        rag_max_tokens = self.max_context_tokens - web_tokens_used
        context, included_indices = format_context(docs, max_context_tokens=rag_max_tokens)
        docs = [docs[i] for i in included_indices]

        # Re-number web sources after RAG sources and rebuild if needed
        if web_results:
            n_rag_sources = len(docs)
            if n_rag_sources > 0:
                # Re-format with correct start_index now that we know RAG source count
                web_formatted, _, _ = format_web_context(
                    web_results, start_index=n_rag_sources + 1, max_tokens=self.web_search_service.max_tokens
                )

            # Avoid misleading "No document found" when web results provide context
            if not docs:
                context = ""

            context = f"{context}{SOURCE_SEPARATOR}{web_formatted}" if context else web_formatted

        # 4. prepare the output
        messages: list = copy.deepcopy(messages)

        # prepend the messages with the system prompt
        prompt = SPOKEN_STYLE_ANSWER_PROMPT if spoken_style_answer else SYS_PROMPT_TMPLT

        messages.insert(
            0,
            {
                "role": "system",
                "content": prompt.format(
                    context=context, current_date=datetime.now().strftime("%A, %B %d, %Y, %H:%M:%S")
                ),
            },
        )
        payload["messages"] = messages
        return payload, docs, web_results

    async def _prepare_for_completions(self, partition: list[str], payload: dict):
        prompt = payload["prompt"]

        # 1. get the query
        queries: SearchQueries = await self.generate_query(messages=[{"role": "user", "content": prompt}])
        # 2. get docs
        docs = await self.retriever_pipeline.get_relevant_docs(partition=partition, search_queries=queries)

        # 3. Format the retrieved docs
        context, included_indices = format_context(docs, max_context_tokens=self.max_context_tokens)
        docs = [docs[i] for i in included_indices]

        # 4. prepare the output
        if docs:
            prompt = f"""Given the content
            {context}
            Complete the following prompt: {prompt}
            At the very end of your response, on a new line, list which source numbers you used: [Sources: 1, 3]"""

        payload["prompt"] = prompt

        return payload, docs

    async def completions(self, partition: list[str], payload: dict):
        if partition is None:
            docs = []
        else:
            payload, docs = await self._prepare_for_completions(partition=partition, payload=payload)
        llm_output = self.llm_client.completions(request=payload)
        return llm_output, docs

    async def chat_completion(self, partition: list[str] | None, payload: dict):
        metadata = payload.get("metadata") or {}
        use_websearch = metadata.get("websearch", False)

        if partition is None and not use_websearch:
            # Direct LLM mode: no RAG, no web search
            docs = []
            web_results = []
        else:
            payload, docs, web_results = await self._prepare_for_chat_completion(partition=partition, payload=payload)
        llm_output = self.llm_client.chat_completion(request=payload)
        return llm_output, docs, web_results
