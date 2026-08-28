"""Context-contribution ablation: with-context (OpenRAG) vs without-context (closed-book).

For N questions from the dataset, capture two answers side by side:
  - with_context:    the OpenRAG answer (retrieval + chunks fed to the gen model)
  - without_context: the SAME gen model answering the bare question, no chunks

The closed-book call reuses OpenRAG's own generation model (MODEL/BASE_URL/API_KEY
env vars) so the only variable between the two columns is the retrieved context.

No judging here by design — both answers (plus the ground-truth llm_answer) are
written to a CSV so you can eyeball whether retrieval is actually pulling its weight.

Usage:
  python3 context_ablation.py                       # first 5 answerable questions
  python3 context_ablation.py --limit 10 --partition default
"""
import argparse
import asyncio
import os
import random

import pandas as pd
import utils

# Reuse the exact with-context retrieval path the benchmark uses.
from benchmark import retrieve_response_and_docs_openrag
from config import CONFIG
from dotenv import load_dotenv
from loguru import logger
from openai import AsyncOpenAI
from tqdm.asyncio import tqdm

load_dotenv()


async def closed_book_answer(query: str, semaphore: asyncio.Semaphore) -> str | None:
    """Answer the bare question with OpenRAG's gen model — no partition, no chunks."""
    async with semaphore:
        client = AsyncOpenAI(
            api_key=os.environ.get("API_KEY"),
            base_url=os.environ.get("BASE_URL"),
        )
        try:
            res = await client.chat.completions.create(
                model=os.environ.get("MODEL"),
                messages=[{"role": "user", "content": query}],
                temperature=CONFIG.ablation.temperature,
                stream=False,
                timeout=CONFIG.ablation.timeout,
            )
            return res.choices[0].message.content
        except Exception as e:
            logger.debug(f"Closed-book call failed: {e}")
            return None


async def main(
    partition: str = CONFIG.common.partition,
    base_url: str = os.environ.get("BASE_URL"),
    dataset_path: str = CONFIG.common.dataset_path,
    output_dir: str = CONFIG.common.output_dir,
    limit: int = CONFIG.ablation.limit,
):
    os.makedirs(output_dir, exist_ok=True)
    dataset = utils.load_and_validate_dataset(dataset_path)

    # 1. Filter the dataset first
    filtered_dataset = [r for r in dataset if r.get("answerable", True)]

    # 2. Randomly sample up to the limit
    rows = random.sample(filtered_dataset, min(len(filtered_dataset), limit))

    logger.info(f"Running context ablation on {len(rows)} questions (partition={partition})")

    openrag_sem = asyncio.Semaphore(CONFIG.ablation.openrag_concurrency)
    closed_sem = asyncio.Semaphore(CONFIG.ablation.closed_book_concurrency)

    with_tasks = [
        retrieve_response_and_docs_openrag(
            query=r["question"], partition=partition, _base_url=base_url, semaphore=openrag_sem
        )
        for r in rows
    ]
    without_tasks = [closed_book_answer(r["question"], closed_sem) for r in rows]

    with_results, without_results = await asyncio.gather(
        tqdm.gather(*with_tasks, desc="With context (OpenRAG)"),
        tqdm.gather(*without_tasks, desc="Without context (closed-book)"),
    )

    # If every OpenRAG query failed there is no "with context" side to compare —
    # a systematic problem (wrong partition, bad AUTH_TOKEN, server down) rather
    # than per-row flakiness. Fail loudly instead of writing an empty ablation.
    if sum(1 for r in with_results if r[0] is not None) == 0:
        raise RuntimeError(
            f"All {len(with_results)} OpenRAG queries failed (0 responses) for model "
            f"'openrag-{partition}'. Check the partition name, AUTH_TOKEN, and that the "
            f"server at {base_url} is reachable."
        )

    records = []
    for r, with_res, without_ans in zip(rows, with_results, without_results):
        with_ctx_answer = with_res[0]  # (response, chunk_ids, chunk_urls, latency, ...)
        n_chunks = len(with_res[1])
        records.append(
            {
                "question": r["question"],
                "ground_truth": r["llm_answer"],
                "with_context": with_ctx_answer,
                "without_context": without_ans,
                "n_retrieved_chunks": n_chunks,
            }
        )

    df = pd.DataFrame(records)
    out_path = os.path.join(output_dir, CONFIG.ablation.csv_name)
    df.to_csv(out_path, index=False)

    print(f"\nWrote {len(df)} rows to {out_path}\n")
    for _, row in df.iterrows():
        print("=" * 80)
        print(f"Q: {row['question']}")
        print(f"\n[ground truth]    {str(row['ground_truth'])[:200]}")
        print(f"\n[with context]    {str(row['with_context'])[:200]}")
        print(f"\n[without context] {str(row['without_context'])[:200]}")
        print(f"\n(retrieved {row['n_retrieved_chunks']} chunks)\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="With/without-context answer ablation.")
    parser.add_argument("--partition", default=CONFIG.common.partition, help="OpenRAG partition to query")
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL"), help="OpenRAG base URL")
    parser.add_argument("--dataset", default=None, help="Dataset JSON (default: config.common.dataset_path)")
    parser.add_argument("--output-dir", default=CONFIG.common.output_dir, help="Directory for the CSV")
    parser.add_argument("--limit", type=int, default=CONFIG.ablation.limit, help="Number of questions")
    args = parser.parse_args()

    resolved_dataset = args.dataset or os.environ.get("GOLDEN_DATASET") or CONFIG.common.dataset_path

    asyncio.run(
        main(
            partition=args.partition,
            base_url=args.base_url,
            dataset_path=resolved_dataset,
            output_dir=args.output_dir,
            limit=args.limit,
        )
    )
