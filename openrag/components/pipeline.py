import asyncio
import copy
from enum import Enum

from components.prompts import (
    QUERY_CONTEXTUALIZER_PROMPT,
    SPOKEN_STYLE_ANSWER_PROMPT,
    SYS_PROMPT_TMPLT,
)
from config import load_config
from langchain_core.documents.base import Document
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from utils.logger import get_logger

from .llm import LLM
from .map_reduce import RAGMapReduce
from .reranker import Reranker
from .retriever import BaseRetriever, RetrieverFactory
from .utils import format_context

logger = get_logger()
config = load_config()


class RAGMODE(Enum):
    SIMPLERAG = "SimpleRag"
    CHATBOTRAG = "ChatBotRag"


class SearchQueries(BaseModel):
    """Generate search queries from chat history.

    Rules:
    - Use full, descriptive sentences (not short keywords) for better semantic retrieval.
    - Split into multiple subqueries only when the request spans distinct topics.
    - Enrich with prior conversation context only when relevant to the latest user message.
    - For date ranges, create one subquery per time period.
        * Example: "Budget from 2023 to 2025" → three subqueries, one per year.
    """

    query_list: list[str] = Field(..., description="Search queries to retrieve relevant documents.")

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

    async def retrieve_docs(self, partition: list[str], query: str, top_k: int | None = None) -> list[Document]:
        docs = await self.retriever.retrieve(partition=partition, query=query)
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
        self, partition: str, search_queries: SearchQueries, top_k: int | None = None
    ) -> list[Document]:
        tasks = [self.retrieve_docs(partition=partition, query=q, top_k=top_k) for q in search_queries.query_list]
        results = await asyncio.gather(*tasks)
        results = self.reranker.rrf_reranking(doc_lists=results)
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
            temperature=config.llm.get("temperature", 0.2),
        )
        self.max_contextualized_query_len = config.rag["max_contextualized_query_len"]

        # map reduce
        self.map_reduce: RAGMapReduce = RAGMapReduce(config=config)

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

                params = {}
                params["max_completion_tokens"] = self.max_contextualized_query_len
                params["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}

                messages = [
                    {"role": "system", "content": QUERY_CONTEXTUALIZER_PROMPT},
                    {
                        "role": "user",
                        "content": f"Here is the chat history: \n{chat_history}\n",
                    },
                ]

                # generate queries based on the chat history
                sllm = self.query_generator.with_structured_output(SearchQueries, method="function_calling")
                output: SearchQueries = await sllm.ainvoke(messages, config=params)
                return output

    async def _prepare_for_chat_completion(self, partition: list[str], payload: dict):
        messages = payload["messages"]
        messages = messages[-self.chat_history_depth :]  # limit history depth

        # 1. get the query
        queries: SearchQueries = await self.generate_query(messages)
        logger.debug("Prepared query for chat completion", queries=str(queries))

        metadata = payload.get("metadata", {})
        use_map_reduce = metadata.get("use_map_reduce", False)
        spoken_style_answer = metadata.get("spoken_style_answer", False)

        logger.debug(
            "Metadata parameters",
            use_map_reduce=use_map_reduce,
            spoken_style_answer=spoken_style_answer,
        )

        # 2. get docs
        top_k = config.map_reduce["max_total_documents"] if use_map_reduce else None
        docs = await self.retriever_pipeline.get_relevant_docs(partition=partition, search_queries=queries, top_k=top_k)

        if use_map_reduce and docs:
            docs = await self.map_reduce.map(query=" ".join(queries.query_list), chunks=docs)

        # 3. Format the retrieved docs
        context, n_docs = format_context(docs, max_context_tokens=self.max_context_tokens)

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
        return payload, docs[:n_docs]

    async def _prepare_for_completions(self, partition: list[str], payload: dict):
        prompt = payload["prompt"]

        # 1. get the query
        query = await self.generate_query(messages=[{"role": "user", "content": prompt}])
        # 2. get docs
        docs = await self.retriever_pipeline.retrieve_docs(partition=partition, query=query)

        # 3. Format the retrieved docs
        context, n_docs = format_context(docs, max_context_tokens=self.max_context_tokens)

        # 4. prepare the output
        if docs:
            prompt = f"""Given the content
            {context}
            Complete the following prompt: {prompt}
            """

        payload["prompt"] = prompt

        return payload, docs[:n_docs]

    async def completions(self, partition: list[str], payload: dict):
        try:
            if partition is None:
                docs = []
            else:
                payload, docs = await self._prepare_for_completions(partition=partition, payload=payload)
            llm_output = self.llm_client.completions(request=payload)
            return llm_output, docs
        except Exception as e:
            logger.error(f"Error during chat completion: {e!s}")
            raise e

    async def chat_completion(self, partition: list[str] | None, payload: dict):
        try:
            if partition is None:
                docs = []
            else:
                payload, docs = await self._prepare_for_chat_completion(partition=partition, payload=payload)
            llm_output = self.llm_client.chat_completion(request=payload)
            return llm_output, docs
        except Exception as e:
            logger.error(f"Error during chat completion: {e!s}")
            raise e
