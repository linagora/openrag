
import asyncio
from openai import AsyncOpenAI
from langchain_core.documents.base import Document
from utils.logger import get_logger

class Reranker:
    def __init__(self, logger, config):
        import os
        self.model_name = config.reranker.get("model_name")
        self.base_url = config.reranker.get("base_url")
        self.api_key = config.reranker.get("api_key", os.environ.get("RERANKER_API_KEY"))
        if not self.api_key:
            raise ValueError("OpenAI API key must be provided in config or as RERANKER_API_KEY env variable.")
        self._async_client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key)
        self.logger = logger
        self.semaphore = asyncio.Semaphore(5)
        self.logger.debug("OpenAI Reranker initialized", model_name=self.model_name)

    async def rerank(self, query: str, documents: list[Document], top_k: int) -> list[Document]:
        async with self.semaphore:
            self.logger.debug(
                "OpenAI Reranking documents", documents_count=len(documents), top_k=top_k
            )
            top_k = min(top_k, len(documents))
            prompt = f"Rank the following documents by relevance to the query: '{query}'.\n\n" + "\n".join([
                f"Document {i+1}: {doc.page_content}" for i, doc in enumerate(documents)
            ]) + f"\n\nReturn the top {top_k} most relevant document numbers as a comma-separated list."
            try:
                response = await self._async_client.chat.completions.create(
                    model=self.model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                )
                content = response.choices[0].message.content
                indices = [int(x.strip()) - 1 for x in content.split(",") if x.strip().isdigit()]
                output = []
                for idx in indices[:top_k]:
                    doc = documents[idx]
                    doc.metadata["relevance_score"] = 1.0  # Mark as selected, score is dummy
                    output.append(doc)
                return output
            except Exception as e:
                self.logger.error(
                    "OpenAI Reranking failed",
                    error=str(e),
                    model_name=self.model_name,
                    documents_count=len(documents),
                )
                raise e
