# Documents that has informations about the query

import json
import csv
import asyncio
import itertools
import os
from tqdm.asyncio import tqdm

from langchain_core.documents.base import Document
import httpx
from loguru import logger
from dotenv import load_dotenv

load_dotenv()


async def fetch_chunk_data(chunk_url) -> Document:
    async with httpx.AsyncClient(timeout=httpx.Timeout(4 * 10)) as client:
        response = await client.get(chunk_url)
        response.raise_for_status()  # raises exception for 4xx/5xx responses
        data = response.json()
        metadata = data.get("metadata", {})
        return {
            "id": metadata.get("_id"),
            "filename": metadata.get("filename"),
            "corpus_id": metadata.get("filename").split(".")[0],
            "content": data.get("page_content"),
        }


async def __get_relevant_chunks(
    query: str,
    partition: str = "all",
    top_k: int = 8,
    openrag_api_base_url: str = None,
    sempahore: asyncio.Semaphore = None,
    llm_semaphore: asyncio.Semaphore = None,
    add_chunk_relevancy: bool = False,
):
    async with sempahore:
        retries = 3  # Number of retries
        delay = 1  # Delay in seconds between retries
        for attempt in range(retries):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(4 * 60)) as client:
                    res = await client.get(
                        url=f"{openrag_api_base_url}/search/partition/{partition}",
                        params={
                            "text": query,
                            "top_k": top_k,
                        },
                    )
                    res.raise_for_status()
                    data: dict = res.json()
                # Extract the documents
                chunk_links = [doc["link"] for doc in data.get("documents", [])]
                # Extract the content and metadata
                chunks_tasks = [fetch_chunk_data(link) for link in chunk_links]
                chunks = await asyncio.gather(*chunks_tasks)
                return chunks
            except Exception as e:
                logger.debug(f"Attempt {attempt + 1} failed: {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(delay)  # Wait before retrying
                else:
                    logger.debug(f"Error fetching chunks after {retries} attempts: {e}")
                    return None


async def main():
    instruction_file = "./data/instruction.csv"
    query_file = "./data/queries.csv"
    qrel_file = "./data/qrels.csv"
    embedder = os.environ.get("EMBEDDER_MODEL_NAME")
    output_file = f"./data/retrieved_chunks_{embedder}.json"

    partition = "benchmark"
    top_k = 10

    llm_semaphore = asyncio.Semaphore(20)
    semaphore = asyncio.Semaphore(10)

    num_port = os.environ.get("APP_PORT")
    num_host = "localhost"
    openrag_api_base_url = f"http://{num_host}:{num_port}/"

    # load files
    instruction_file = open(instruction_file, "r", encoding="utf-8")
    query_file = open(query_file, "r", encoding="utf-8")
    qrel_file = open(qrel_file, "r", encoding="utf-8")

    instructions = csv.DictReader(instruction_file)
    instructions_list = list(itertools.islice(instructions, 500))

    queries = csv.DictReader(query_file)
    queries_list = list(itertools.islice(queries, 500))

    qrels = csv.DictReader(qrel_file)
    qrels_list = list(itertools.islice(qrels, 500))

    tasks = [
        __get_relevant_chunks(
            query=entry_instruction["instruction"] + entry_query["text"],
            partition=partition,
            top_k=top_k,
            openrag_api_base_url=openrag_api_base_url,
            sempahore=semaphore,
            llm_semaphore=llm_semaphore,
            add_chunk_relevancy=True,
        )
        for entry_instruction, entry_query in zip(instructions_list, queries_list)
    ]

    data = await tqdm.gather(*tasks, desc="Generating data for evaluation")

    data2 = []
    for instruction, query, qrel, chunks in zip(
        instructions_list, queries_list, qrels_list, data
    ):
        instruction["query"] = query["text"]
        instruction["response_id"] = qrel["corpus-id"]
        instruction["all_retrieved_chunks"] = chunks
        data2.append(instruction)

    with open(output_file, "w", encoding="utf-8") as json_file:
        json.dump(data2, json_file, indent=4, ensure_ascii=False)


if __name__ == "__main__":
    asyncio.run(main())
