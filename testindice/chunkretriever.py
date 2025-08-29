"""
This utility reads a JSONL file containing search queries and retrieves the
best matching chunks for each query without invoking the LLM. Results are
stored in JSONL format.
"""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import httpx
from loguru import logger


async def _fetch_chunks(
    client: httpx.AsyncClient,
    base_url: str,
    partition: str,
    query: str,
    top_k: int,
    semaphore: asyncio.Semaphore,
) -> Dict[str, Any]:
    """Retrieve chunks for a single query."""
    async with semaphore:
        url = f"{base_url}/search/partition/{partition}"
        for attempt in range(3):
            try:
                resp = await client.get(url, params={"text": query, "top_k": top_k})
                resp.raise_for_status()
                data = resp.json()
                return {"query": query, "documents": data.get("documents", [])}
            except Exception as e:  # pragma: no cover - network errors
                logger.debug(f"Attempt {attempt + 1} failed for query '{query}': {e}")
                if attempt < 2:
                    await asyncio.sleep(1)
                else:
                    logger.error(f"Failed to fetch chunks for query '{query}': {e}")
                    return {"query": query, "documents": []}


async def main(args: argparse.Namespace) -> None:
    queries: List[str] = []
    with open(args.queries, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    q = (
                        obj.get("query")
                        or obj.get("text")
                        or obj.get("question")
                        or obj.get("q")
                    )
                    queries.append(q if q is not None else line)
                else:
                    queries.append(str(obj))
            except json.JSONDecodeError:
                queries.append(line)

    semaphore = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(timeout=httpx.Timeout(4 * 60)) as client:
        tasks = [
            _fetch_chunks(
                client, args.base_url, args.partition, q, args.top_k, semaphore
            )
            for q in queries
        ]
        results = await asyncio.gather(*tasks)

    output_path = Path(args.output)
    with output_path.open("w", encoding="utf-8") as out:
        for item in results:
            out.write(json.dumps(item, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch retrieve chunks from OpenRAG")
    parser.add_argument("queries", help="Path to the queries JSONL file")
    parser.add_argument(
        "-o", "--output", default="retrieved_chunks.jsonl", help="Output JSONL file"
    )
    parser.add_argument(
        "-b",
        "--base-url",
        default="http://localhost:8090",
        help="Base URL of the OpenRAG API",
    )
    parser.add_argument("-p", "--partition", default="all", help="Partition to search")
    parser.add_argument(
        "-k", "--top_k", type=int, default=5, help="Number of chunks to retrieve"
    )
    parser.add_argument(
        "-c", "--concurrency", type=int, default=10, help="Number of parallel requests"
    )

    args = parser.parse_args()
    asyncio.run(main(args))
