import asyncio
import copy
from enum import Enum

from components.prompts import (
    QUERY_CONTEXTUALIZER_PROMPT,
    SPOKEN_STYLE_ANSWER_PROMPT,
    SYS_PROMPT_TMPLT,
)
from components.websearch import WebSearchFactory
from config import load_config
from langchain_core.documents.base import Document
from openai import AsyncOpenAI
from utils.logger import get_logger

from .llm import LLM
from .map_reduce import RAGMapReduce
from .reranker import Reranker
from .retriever import BaseRetriever, RetrieverFactory
from .utils import SOURCE_SEPARATOR, format_context, format_web_context

logger = get_logger()
config = load_config()


class RAGMODE(Enum):
    SIMPLERAG = "SimpleRag"
    CHATBOTRAG = "ChatBotRag"


class RetrieverPipeline:
    def __init__(self) -> None:
        # retriever
        self.retriever: BaseRetriever = RetrieverFactory.create_retriever(config=config)

        # reranker
        self.reranker_enabled = config.reranker["enable"]
        self.reranker = Reranker(logger, config)
        logger.debug("Reranker", enabled=self.reranker_enabled)
        self.reranker_top_k = config.reranker["top_k"]

    async def retrieve_docs(self, partition: list[str], query: str, top_k: int | None = None) -> list[Document]:
        docs = await self.retriever.retrieve(partition=partition, query=query)
        logger.debug("Documents retreived", document_count=len(docs))

        if docs:
            # 1. rerank all the docs
            if self.reranker_enabled:
                docs = await self.reranker.rerank(query, documents=docs, top_k=None)
                logger.debug("Documents reranked", document_count=len(docs))

            # 2. expand the docs with related documents
            if self.retriever.expansion_enabled:
                # Limit the number of docs to expand
                top_k = max(self.reranker_top_k, top_k) if top_k else self.reranker_top_k
                docs2expand = copy.deepcopy(docs[:top_k])

                logger.debug("Documents to expand", document_count=len(docs2expand))

                expanded_docs = await self.retriever.expand_search_results(results=docs2expand)

                logger.debug("Documents expanded", document_count=len(expanded_docs))

                if len(docs2expand) == len(expanded_docs):  # no expansion found, keep the original docs
                    return docs

                docs = expanded_docs

                # rerank again after expansion if reranker is enabled
                if self.reranker_enabled:
                    docs = await self.reranker.rerank(query, documents=docs, top_k=None)
                    logger.debug("Documents after expansion and reranking", document_count=len(docs))

        return docs


class RagPipeline:
    def __init__(self) -> None:
        # retriever pipeline
        self.retriever_pipeline = RetrieverPipeline()

        # RAG
        self.rag_mode = config.rag["mode"]
        self.chat_history_depth = config.rag["chat_history_depth"]
        self.max_context_tokens = config.reranker.get("top_k", 10) * config.chunker.get("chunk_size", 512)

        self.llm_client = LLM(config.llm, logger)
        self.contextualizer = AsyncOpenAI(base_url=config.llm["base_url"], api_key=config.llm["api_key"])
        self.max_contextualized_query_len = config.rag["max_contextualized_query_len"]

        # map reduce
        self.map_reduce: RAGMapReduce = RAGMapReduce(config=config)

        # Web search
        self.web_search_service = WebSearchFactory.create_service(config)
        if self.web_search_service.provider:
            logger.info("Web search enabled", provider=config.websearch.get("provider"))
        else:
            logger.info("Web search disabled (WEBSEARCH_API_TOKEN not set)")

    async def generate_query(self, messages: list[dict]) -> str:
        match RAGMODE(self.rag_mode):
            case RAGMODE.SIMPLERAG:
                # For SimpleRag, we don't need to contextualize the query as the chat history is not taken into account
                last_msg = messages[-1]
                return last_msg["content"]

            case RAGMODE.CHATBOTRAG:
                # Contextualize the query based on the chat history
                chat_history = ""
                for m in messages:
                    chat_history += f"{m['role']}: {m['content']}\n"

                params = dict(config.llm_params)
                params.pop("max_retries")
                params["max_completion_tokens"] = self.max_contextualized_query_len
                params["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}

                response = await self.contextualizer.chat.completions.create(
                    model=config.llm["model"],
                    messages=[
                        {"role": "system", "content": QUERY_CONTEXTUALIZER_PROMPT},
                        {
                            "role": "user",
                            "content": f"Given the following chat, generate a query. \n{chat_history}\n",
                        },
                    ],
                    **params,
                )
                contextualized_query = response.choices[0].message.content
                return contextualized_query

    async def _prepare_for_chat_completion(self, partition: list[str] | None, payload: dict):
        messages = payload["messages"]
        messages = messages[-self.chat_history_depth :]  # limit history depth

        # 1. get the query
        query = await self.generate_query(messages)
        logger.debug("Prepared query for chat completion", query=query)

        metadata = payload.get("metadata") or {}

        use_map_reduce = metadata.get("use_map_reduce", False)
        spoken_style_answer = metadata.get("spoken_style_answer", False)
        use_websearch = metadata.get("websearch", False)

        logger.debug(
            "Metadata parameters",
            use_map_reduce=use_map_reduce,
            spoken_style_answer=spoken_style_answer,
            use_websearch=use_websearch,
        )

        # 2. get docs and/or web results concurrently
        top_k = config.map_reduce["max_total_documents"] if use_map_reduce else None
        if partition is not None and use_websearch:
            docs, web_results = await asyncio.gather(
                self.retriever_pipeline.retrieve_docs(partition=partition, query=query, top_k=top_k),
                self.web_search_service.search(query),
            )
        elif partition is not None:
            docs = await self.retriever_pipeline.retrieve_docs(partition=partition, query=query, top_k=top_k)
            web_results = []
        else:
            # Web-only mode (partition is None): no RAG retrieval
            docs = []
            web_results = await self.web_search_service.search(query)

        # Web-only with no results: fall back to plain direct LLM mode
        if not docs and not web_results and partition is None:
            return payload, [], []

        if use_map_reduce and docs:
            docs = await self.map_reduce.map(query=query, chunks=docs)

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
        query = await self.generate_query(messages=[{"role": "user", "content": prompt}])
        # 2. get docs
        docs = await self.retriever_pipeline.retrieve_docs(partition=partition, query=query)

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
