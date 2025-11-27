import copy
from enum import Enum

from components.prompts import (
    QUERY_CONTEXTUALIZER_PROMPT,
    SPOKEN_STYLE_ANSWER_PROMPT,
    SYS_PROMPT_TMPLT,
)
from langchain_core.documents.base import Document
from openai import AsyncOpenAI
from utils.logger import get_logger

from .llm import LLM
from .map_reduce import RAGMapReduce
from .reranker import Reranker
from .retriever import ABCRetriever, RetrieverFactory
from .utils import format_context

logger = get_logger()


class RAGMODE(Enum):
    SIMPLERAG = "SimpleRag"
    CHATBOTRAG = "ChatBotRag"


class RetrieverPipeline:
    def __init__(self, config) -> None:
        self.config = config

        # retriever
        self.retriever: ABCRetriever = RetrieverFactory.create_retriever(config=config)

        # reranker
        self.reranker = None
        self.reranker_enabled = config.reranker["enable"]
        logger.debug("Reranker", enabled=self.reranker_enabled)
        self.reranker_top_k = int(config.reranker["top_k"])

        # map & reduce
        self.retriever_top_k = int(config.retriever["top_k"])
        self.map_reduce_max_docs = self.config.map_reduce["max_total_documents"]

        if self.reranker_enabled:
            self.reranker = Reranker(logger, config)

    async def retrieve_docs(
        self, partition: list[str], query: str, use_map_reduce: bool = False
    ) -> list[Document]:
        docs = await self.retriever.retrieve(partition=partition, query=query)
        top_k = (
            max(self.map_reduce_max_docs, self.reranker_top_k)
            if use_map_reduce
            else self.reranker_top_k
        )
        logger.debug("Documents retreived", document_count=len(docs))
        if docs:
            # rerank documents
            if self.reranker_enabled:
                docs = await self.reranker.rerank(query, documents=docs, top_k=top_k)
                logger.debug("Documents after reranking", document_count=len(docs))
            else:
                docs = docs[:top_k]
        return docs


class RagPipeline:
    def __init__(self, config) -> None:
        self.config = config

        # retriever pipeline
        self.retriever_pipeline = RetrieverPipeline(config=config)

        self.rag_mode = config.rag["mode"]
        self.chat_history_depth = config.rag["chat_history_depth"]

        self.llm_client = LLM(config.llm, logger)
        self.contextualizer = AsyncOpenAI(
            base_url=config.llm["base_url"], api_key=config.llm["api_key"]
        )
        self.max_contextualized_query_len = config.rag["max_contextualized_query_len"]

        # map reduce
        self.map_reduce: RAGMapReduce = RAGMapReduce(config=config)

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

                params = dict(self.config.llm_params)
                params.pop("max_retries")
                params["max_completion_tokens"] = self.max_contextualized_query_len
                params["extra_body"] = {
                    "chat_template_kwargs": {"enable_thinking": False}
                }

                response = await self.contextualizer.chat.completions.create(
                    model=self.config.llm["model"],
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

    async def _prepare_for_chat_completion(self, partition: list[str], payload: dict):
        messages = payload["messages"]
        messages = messages[-self.chat_history_depth :]  # limit history depth

        # 1. get the query
        query = await self.generate_query(messages)
        logger.debug("Prepared query for chat completion", query=query)

        metadata = payload.get("metadata", {})

        use_map_reduce = metadata.get("use_map_reduce", False)
        spoken_style_answer = metadata.get("spoken_style_answer", False)

        logger.debug(
            "Metadata parameters",
            use_map_reduce=use_map_reduce,
            spoken_style_answer=spoken_style_answer,
        )

        # 2. get docs
        docs = await self.retriever_pipeline.retrieve_docs(
            partition=partition, query=query, use_map_reduce=use_map_reduce
        )

        if use_map_reduce and docs:
            docs = await self.map_reduce.map(query=query, chunks=docs)

        # 3. Format the retrieved docs
        context = format_context(docs)

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
        return payload, docs

    async def _prepare_for_completions(self, partition: list[str], payload: dict):
        prompt = payload["prompt"]

        # 1. get the query
        query = await self.generate_query(
            messages=[{"role": "user", "content": prompt}]
        )
        # 2. get docs
        docs = await self.retriever_pipeline.retrieve_docs(
            partition=partition, query=query
        )

        # 3. Format the retrieved docs
        context = format_context(docs)

        # 4. prepare the output
        if docs:
            prompt = f"""Given the content
            {context}
            Complete the following prompt: {prompt}
            """

        payload["prompt"] = prompt

        return payload, docs

    async def completions(self, partition: list[str], payload: dict):
        payload, docs = await self._prepare_for_completions(
            partition=partition, payload=payload
        )
        llm_output = self.llm_client.completions(request=payload)
        return llm_output, docs

    async def chat_completion(self, partition: list[str], payload: dict):
        try:
            payload, docs = await self._prepare_for_chat_completion(
                partition=partition, payload=payload
            )
            llm_output = self.llm_client.chat_completion(request=payload)
            return llm_output, docs
        except Exception as e:
            logger.error(f"Error during chat completion: {str(e)}")
            raise e
