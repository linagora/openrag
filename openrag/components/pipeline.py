import asyncio
import copy
from datetime import datetime
from enum import Enum

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
from langchain_openai import ChatOpenAI
from models.openai import Attachment
from pydantic import BaseModel, Field
from utils.logger import get_logger

from .file_summarizer import FileReducer
from .llm import LLM
from .map_reduce import RAGMapReduce
from .reranker import Reranker
from .retriever import BaseRetriever, RetrieverFactory
from .utils import SOURCE_SEPARATOR

logger = get_logger()
config = load_config()
VECTORDB_TIMEOUT = config.ray.indexer.get("vectordb_timeout", 30)


class RAGMODE(Enum):
    SIMPLERAG = "SimpleRag"
    CHATBOTRAG = "ChatBotRag"


class SearchQueries(BaseModel):
    """Search queries for semantic retrieval."""

    query_list: list[str] = Field(..., description="Search sub-queries to retrieve relevant documents.")

    def __str__(self) -> str:
        return " -- ".join(f"Query: {q}" for q in self.query_list)


class RetrieverPipeline:
    def __init__(self) -> None:
        # retriever
        self.retriever: BaseRetriever = RetrieverFactory.create_retriever(config=config)

        # reranker
        self.reranker_enabled = config.reranker["enable"]
        self.reranker = Reranker(logger, config)
        logger.debug("Reranker", enabled=self.reranker_enabled)
        self.reranker_top_k = config.reranker["top_k"]

    async def retrieve_docs(
        self,
        partition: list[str],
        query: str,
        top_k: int | None = None,
        filter: str | None = None,
        filter_params: dict | None = None,
    ) -> list[Document]:
        docs = await self.retriever.retrieve(
            partition=partition, query=query, filter=filter, filter_params=filter_params
        )
        logger.debug("Documents retreived", document_count=len(docs))

        if docs:
            # 1. rerank all the docs
            if self.reranker_enabled:
                docs = await self.reranker.rerank(query=query, documents=docs, top_k=None)
                logger.debug("Documents reranked", document_count=len(docs))

            # 2. expand the docs with related documents
            if self.retriever.expansion_enabled:
                # Limit the number of docs to expand
                top_k = max(self.reranker_top_k, top_k) if top_k else self.reranker_top_k
                docs2expand = copy.deepcopy(docs[:top_k])

                logger.debug("Documents to expand", document_count=len(docs2expand))
                expanded_docs = await self.retriever.expand_search_results(results=docs2expand)
                if len(docs2expand) == len(expanded_docs):  # no expansion found, keep the original docs
                    return docs

                logger.debug("Documents expanded", document_count=len(expanded_docs))
                docs = expanded_docs

                # rerank again after expansion if reranker is enabled
                if self.reranker_enabled:
                    docs = await self.reranker.rerank(query=query, documents=docs, top_k=None)
                    logger.debug("Documents after expansion and reranking", document_count=len(docs))

        return docs

    async def get_relevant_docs(
        self,
        partition: list[str],
        search_queries: SearchQueries,
        top_k: int | None = None,
        filter: str | None = None,
        filter_params: dict | None = None,
    ) -> list[Document]:
        tasks = [
            self.retrieve_docs(partition=partition, query=q, top_k=top_k, filter=filter, filter_params=filter_params)
            for q in search_queries.query_list
        ]
        results = await asyncio.gather(*tasks)
        results = self.reranker.rrf_reranking(doc_lists=results)
        if top_k is not None:
            results = results[:top_k]
        logger.debug("Final relevant documents after RRF reranking", document_count=len(results))
        return results


class RagPipeline:
    def __init__(self) -> None:
        # retriever pipeline
        self.retriever_pipeline = RetrieverPipeline()

        # RAG
        self.rag_mode = config.rag["mode"]
        self.chat_history_depth = config.rag["chat_history_depth"]
        self.max_context_tokens = config.reranker.get("top_k", 10) * config.chunker.get("chunk_size", 512)

        self.llm_client = LLM(config.llm, logger)
        self.query_generator = ChatOpenAI(
            base_url=config.llm.get("base_url"),
            api_key=config.llm.get("api_key"),
            model=config.llm.get("model"),
            temperature=config.llm.get("temperature", 0.3),
        ).with_structured_output(SearchQueries, method="function_calling")

        self.max_contextualized_query_len = config.rag["max_contextualized_query_len"]

        # map reduce
        self.map_reduce: RAGMapReduce = RAGMapReduce(config=config)

        # file reducer
        self.file_reducer = FileReducer(config)

        # Web search
        self.web_search_service = WebSearchFactory.create_service(config)
        if self.web_search_service.provider:
            logger.info("Web search enabled", provider=config.websearch.get("provider"))
        else:
            logger.info("Web search disabled (WEBSEARCH_API_TOKEN not set)")

    async def generate_query(self, messages: list[dict]) -> SearchQueries:
        match RAGMODE(self.rag_mode):
            case RAGMODE.SIMPLERAG:
                # For SimpleRag, we don't need to contextualize the query as the chat history is not taken into account
                last_msg = messages[-1]
                return SearchQueries(query_list=[last_msg["content"]])

            case RAGMODE.CHATBOTRAG:
                # Contextualize the query based on the chat history
                chat_history = ""
                for m in messages:
                    chat_history += f"{m['role']}: {m['content']}\n"

                query_language = detect_language(messages[-1]["content"])

                model_kwargs = {
                    "max_completion_tokens": self.max_contextualized_query_len,
                    "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
                }
                prompt = QUERY_CONTEXTUALIZER_PROMPT.format(
                    query_language=query_language,
                    current_date=datetime.now().strftime("%Y-%m-%d"),
                )

                messages = [
                    {
                        "role": "system",
                        "content": prompt,
                    },
                    {
                        "role": "user",
                        "content": f"Here is the chat history: \n{chat_history}\n",
                    },
                ]

                # generate queries based on the chat history
                output: SearchQueries = await self.query_generator.bind(**model_kwargs).ainvoke(messages)
                return output

    async def _prepare_for_chat_completion(self, partition: list[str] | None, payload: dict):
        messages = payload["messages"]
        messages = messages[-self.chat_history_depth :]  # limit history depth

        metadata = payload.get("metadata") or {}

        # Extract and validate attachments from metadata
        attachments_raw = metadata.get("attachments")
        file_ids: list[str] = []
        if attachments_raw:
            try:
                attachments = [Attachment.model_validate(att) for att in attachments_raw if isinstance(att, dict)]
                file_ids = [att.id for att in attachments if att.id]
            except Exception as e:
                logger.warning("Failed to validate attachments", error=str(e))
                file_ids = []

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
            file_ids_count=len(file_ids),
        )

        # FILE_ID RETRIEVAL MODE (skip semantic search)
        if file_ids:
            log = logger.bind(file_ids=file_ids, mode="file_based_retrieval")
            log.info("File-based retrieval mode enabled")

            # Retrieve chunks directly by file_id (parallel retrieval)
            vectordb = ray.get_actor("Vectordb", namespace="openrag")
            try:
                docs_by_file: list[list[Document]] = await call_ray_actor_with_timeout(
                    vectordb.get_chunks_by_file_ids.remote(file_ids=file_ids, partition=partition),
                    timeout=VECTORDB_TIMEOUT,
                    task_description=f"get_chunks_by_file_ids({len(file_ids)} files)",
                )
                log.debug(f"Retrieved {sum(len(d) for d in docs_by_file)} chunks from {len(file_ids)} files")
            except TimeoutError as e:
                # Timeout handling - log and return empty docs
                log.error("Timeout retrieving chunks for file_ids", timeout=VECTORDB_TIMEOUT, error=str(e))
                docs_by_file = []

            # Create dummy queries for logging consistency
            queries = SearchQueries(query_list=[messages[-1]["content"]])
            web_results = []

            # Apply file reduction per file, then flatten
            if docs_by_file:
                docs = await self.file_reducer.reduce_all(query=queries.query_list[0], docs_l=docs_by_file)
            else:
                docs = []

        # NORMAL SEMANTIC SEARCH MODE
        else:
            # 1. get the query
            queries: SearchQueries = await self.generate_query(messages)
            logger.debug("Prepared query for chat completion", queries=str(queries))

            # 2. get docs and/or web results concurrently
            top_k = config.map_reduce["max_total_documents"] if use_map_reduce else None
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
                web_tasks = [self.web_search_service.search(q) for q in queries.query_list]
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
                raw_web_lists = await asyncio.gather(*[self.web_search_service.search(q) for q in queries.query_list])
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

        if not file_ids and use_map_reduce and docs:
            docs = await self.map_reduce.map(query=" ".join(queries.query_list), chunks=docs)

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
                "content": prompt.format(context=context),
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
