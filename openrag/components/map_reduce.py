from pathlib import Path

from config import load_config
from langchain_core.documents.base import Document
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from tqdm.asyncio import tqdm
from utils.logger import get_logger

from .utils import get_llm_semaphore

logger = get_logger()
config = load_config()

LOG_DIR = Path(config.paths.log_dir)


system_prompt_map = """You are an AI assistant specialized in extracting and synthesizing relevant information from text.

Your task:
1. Analyze the provided text in relation to the user's question
2. Extract only the essential information that directly addresses the query
3. Preserve necessary context (Key words, project names or initiatives, dates, etc.) to maintain accuracy and clarity of the summary for it to be self-understandable

Guidelines:
- Present information clearly and concisely without unnecessary rephrasing or commentary
- Focus on precision: include what matters, exclude what doesn't.
- If a document does have any relevant content with respect to a query, classify it irrelevant such without providing a `synthesis`.
"""


class SummarizedChunk(BaseModel):
    relevancy: bool = Field(..., description="Indicates if the chunk is relevant to the query")
    summary: str = Field(
        "",
        description="The summarized content of the chunk. The field should be empty if relevancy is False.",
    )


user_prompt = """
Here is a text:
{content}

From this document, identify and comprehensively summarize the information useful for answering the following question:
{query}
"""


class RAGMapReduce:
    def __init__(self, config):
        self.config = config
        self.slm: ChatOpenAI = ChatOpenAI(**config.llm.model_dump()).with_structured_output(SummarizedChunk)
        map_reduce_config = self.config.map_reduce
        self.initial_batch_size = map_reduce_config.initial_batch_size
        self.expansion_batch_size = map_reduce_config.expansion_batch_size
        self.max_total_documents = map_reduce_config.max_total_documents

        self.debug = map_reduce_config.debug

        assert self.max_total_documents >= self.initial_batch_size, (
            "`max_total_documents` must be greater than or equal to `initial_batch_size`"
        )

    async def infer_chunk_relevancy(self, query, chunk: Document) -> SummarizedChunk:
        async with get_llm_semaphore():
            try:
                params = {
                    "max_tokens": 512,
                    "temperature": 0.3,
                }
                output_chunk: SummarizedChunk = await self.slm.ainvoke(
                    [
                        {"role": "system", "content": system_prompt_map},
                        {
                            "role": "user",
                            "content": user_prompt.format(query=query, content=chunk.page_content),
                        },
                    ],
                    **params,
                )
                return output_chunk
            except Exception as e:
                logger.error("Error during chunk relevancy inference", error=str(e))
                return SummarizedChunk(relevancy=False, summary="")

    async def map_batch(
        self,
        query: str,
        chunks: list[Document],
        summaries: list[SummarizedChunk],
        kth_batch=1,
    ):
        """Process a batch of chunks"""
        logger.debug(f"Processing {kth_batch}-th batch of chunks", batch_size=len(chunks))
        tasks = [self.infer_chunk_relevancy(query, chunk) for chunk in chunks]
        outputs: list[SummarizedChunk] = await tqdm.gather(
            *tasks, desc="Map & Reduce processing chunks", total=len(chunks)
        )

        # if the last 'expansion_batch_size' chunks are all irrelevant, we can terminate
        terminate = all(not o.relevancy for o in outputs[-self.expansion_batch_size :])

        for o, chunk in zip(outputs, chunks):
            if o.relevancy:
                summaries.append(Document(page_content=o.summary, metadata=chunk.metadata))

            if self.debug:
                with open(LOG_DIR / "map_reduce.md", "a") as f:
                    f.write(f"### Query: \n{query}\n")
                    f.write(f"### Chunk Content: \n* Relevancy: {o.relevancy} \n\n {chunk.page_content}\n")
                    f.write(f"### Summary: \n{o.summary}\n")
                    f.write("\n-------\n\n")

        return outputs, terminate

    async def map(self, query: str, chunks: list[Document]):
        """Perform the map phase of map-reduce on the provided chunks.
        Initally processes `initial_batch_size` number of documents to identify relevant ones. If they are all found to be relevant,
        it continues to process additional documents in batches of `expansion_batch_size` until a
        Args:
            query (str): The user's query.
            chunks (list[Document]): The list of document chunks (the `RETRIEVER_TOP_K` documents from the retreiver) to process.

        Returns: list[Document]: A list of relevant document summaries.
        """

        summaries: list[Document] = []

        initial_batch, remaining_chunks = (
            chunks[: self.initial_batch_size],
            chunks[self.initial_batch_size :],
        )
        _, terminate = await self.map_batch(query, initial_batch, summaries=summaries, kth_batch=1)

        if terminate or not remaining_chunks or len(summaries) >= self.max_total_documents:
            return summaries

        for jth_batch, i in enumerate(range(0, len(remaining_chunks), self.expansion_batch_size), start=2):
            n = min(self.expansion_batch_size, self.max_total_documents - len(summaries))
            if n <= 0:
                break

            logger.debug(
                f"Expanding map phase: processing batch {jth_batch} with size {n}",
                summaries_count=len(summaries),
            )

            next_batch = remaining_chunks[i : i + n]
            _, terminate = await self.map_batch(
                query=query, chunks=next_batch, summaries=summaries, kth_batch=jth_batch
            )

            if terminate or len(summaries) >= self.max_total_documents:
                break

        logger.debug("Map reduce completed", relevant_chunks_count=len(summaries), query=query)
        return summaries
