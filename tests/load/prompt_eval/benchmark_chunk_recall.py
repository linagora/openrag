"""Compare real contextualizers on frozen SciFact abstract chunks.

Uses the selected source tree's QueryService and RetrieverPipeline, live model
endpoints, and exact dense cosine search. This isolates decomposition from
Milvus/BM25/indexing changes; it is not an end-to-end API benchmark.
Credentials are loaded from a supplied dotenv file and never included in reports.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
from dotenv import dotenv_values


def chunk_recall(gold: list[str], retrieved: list[str]) -> dict:
    if not gold:
        raise ValueError("Every case requires gold chunks")
    found = len(set(gold) & set(retrieved))
    return {"recall": found / len(set(gold)), "full_recall": found == len(set(gold))}


async def run(args):
    sys.path.insert(0, str(args.source_root / "openrag"))
    from core.models.chunk import Chunk
    from core.retrieval.pipeline import RetrieverPipeline
    from core.retrieval.retriever import Retriever
    from services.orchestrators.query_service import QueryService

    query_module = sys.modules[QueryService.__module__]

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 13, tzinfo=UTC).astimezone(tz)

    query_module.datetime = FrozenDatetime
    env = dotenv_values(args.env_file)
    corpus_bytes = (args.corpus / "corpus.jsonl").read_bytes()
    records = [json.loads(line) for line in corpus_bytes.splitlines()]
    chunks = [Chunk(id=r["_id"], document_id=r["_id"], text=r["title"] + "\n" + r["text"]) for r in records]
    cases = json.loads(args.cases.read_text())
    ids = {c.id for c in chunks}
    if any(not set(case["gold_chunk_ids"]) <= ids for case in cases):
        raise ValueError("Gold chunk absent from corpus")
    fingerprint = hashlib.sha256(corpus_bytes + str(env["EMBEDDER_MODEL_NAME"]).encode()).hexdigest()
    args.cache.mkdir(parents=True, exist_ok=True)
    cache_path = args.cache / f"{fingerprint}.npy"

    async with httpx.AsyncClient(timeout=120) as client:

        async def post(url_key, key_key, route, body):
            response = await client.post(
                str(env[url_key]).rstrip("/") + route,
                headers={"Authorization": "Bearer " + str(env.get(key_key) or "EMPTY")},
                json=body,
            )
            response.raise_for_status()
            return response.json()

        async def embed(texts):
            data = await post(
                "EMBEDDER_BASE_URL",
                "EMBEDDER_API_KEY",
                "/embeddings",
                {
                    "model": env["EMBEDDER_MODEL_NAME"],
                    "input": texts,
                },
            )
            rows = np.asarray(
                [r["embedding"] for r in sorted(data["data"], key=lambda r: r["index"])], dtype=np.float32
            )
            return rows / np.maximum(np.linalg.norm(rows, axis=1, keepdims=True), 1e-12)

        if cache_path.exists():
            vectors = np.load(cache_path, allow_pickle=False)
        else:
            batches = []
            for offset in range(0, len(chunks), 64):
                batches.append(await embed([c.text for c in chunks[offset : offset + 64]]))
                print(f"embedded {min(offset + 64, len(chunks))}/{len(chunks)}", flush=True)
            vectors = np.concatenate(batches)
            np.save(cache_path, vectors)
        if len(vectors) != len(chunks):
            raise ValueError("Embedding cache and corpus disagree")

        class LiveLLM:
            calls = None

            async def chat(self, messages, **params):
                start = time.perf_counter()
                response = await post(
                    "BASE_URL",
                    "API_KEY",
                    "/chat/completions",
                    {
                        "model": env["MODEL"],
                        "messages": messages,
                        **params,
                    },
                )
                self.calls.append(
                    {
                        "seconds": time.perf_counter() - start,
                        "usage": response.get("usage"),
                        "finish_reason": response["choices"][0].get("finish_reason"),
                        "response": response["choices"][0]["message"]["content"],
                        "system_prompt_sha256": hashlib.sha256(messages[0]["content"].encode()).hexdigest(),
                    }
                )
                return response

        class DenseRetriever(Retriever):
            async def retrieve(self, partition, query, filter=None, filter_params=None, trace=None):
                if filter:
                    raise ValueError("This corpus has no creation timestamps; temporal filtering is unsupported")
                vector = (await embed([query]))[0]
                scores = vectors @ vector
                indexes = np.argsort(-scores, kind="stable")[: args.candidates]
                selected = [chunks[int(i)] for i in indexes]
                retrievals.append({"query": query, "pre_rerank": [c.id for c in selected]})
                return selected

            async def expand_search_results(self, results, filter_params=None):
                return results

        class LiveReranker:
            async def rerank(self, query, documents, top_k=None):
                response = await post(
                    "RERANKER_BASE_URL",
                    "RERANKER_API_KEY",
                    "/rerank",
                    {
                        "model": env["RERANKER_MODEL"],
                        "query": query,
                        "documents": documents,
                        "top_n": len(documents),
                        "return_documents": False,
                        "raw_scores": True,
                    },
                )
                ranking = [(r["index"], r["relevance_score"]) for r in response["results"]]
                pre = next(r["pre_rerank"] for r in retrievals if r["query"] == query)
                rerankings.append(
                    {
                        "query": query,
                        "post_rerank": [pre[i] for i, _ in ranking],
                        "scores": [score for _, score in ranking],
                    }
                )
                return ranking

        template = (args.source_root / "openrag/prompts/templates/query_contextualizer_tmpl.txt").read_text()

        class PromptStore:
            async def resolve_prompt(self, *_args, **_kwargs):
                return template

        # Exercise the actual production method; the storage and HTTP adapters
        # below replace DI/bootstrap so this run needs no Ray/Postgres/Milvus.
        service = QueryService.__new__(QueryService)
        llm = LiveLLM()
        service._llm = llm
        service._rag_mode = "ChatBotRag"
        service._max_contextualized_query_len = args.output_tokens
        service._prompt_service = PromptStore()
        service._config = SimpleNamespace(partitions={})
        pipeline = RetrieverPipeline(DenseRetriever(), LiveReranker(), rrf_k=60, allow_filterless_fallback=False)
        report = {
            "source_sha": subprocess.check_output(
                ["git", "-C", str(args.source_root), "rev-parse", "HEAD"], text=True
            ).strip(),
            "source_dirty": bool(
                subprocess.check_output(["git", "-C", str(args.source_root), "status", "--porcelain"], text=True)
            ),
            "runtime_date_utc": "2026-10-13T00:00:00+00:00",
            "source_files_sha256": {
                str(path): hashlib.sha256((args.source_root / path).read_bytes()).hexdigest()
                for path in [
                    Path("openrag/services/orchestrators/query_service.py"),
                    Path("openrag/core/models/query.py"),
                    Path("openrag/core/retrieval/pipeline.py"),
                ]
            },
            "template_sha256": hashlib.sha256(template.encode()).hexdigest(),
            "corpus_sha256": hashlib.sha256(corpus_bytes).hexdigest(),
            "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
            "embedding_cache_sha256": hashlib.sha256(cache_path.read_bytes()).hexdigest(),
            "chunk_count": len(chunks),
            "chunking": "one complete title+abstract per chunk; SciFact corpus ID is chunk ID",
            "models": {k: env[k] for k in ["MODEL", "EMBEDDER_MODEL_NAME", "RERANKER_MODEL"]},
            "settings": {
                "candidates_per_query": args.candidates,
                "final_top_k": args.top_k,
                "rrf_k": 60,
                "output_tokens": args.output_tokens,
                "expansion": False,
                "hybrid_search": False,
                "search": "exact dense cosine; no similarity threshold",
                "rerank": "all candidates per query before RRF",
            },
            "limitations": [
                "No Milvus or BM25",
                "No answer generation, prompt-budget selection, or API TTFT",
                "Small held-out comparison set; not the original issue-751 arXiv corpus",
            ],
            "expected_cases": len(cases),
            "completed": False,
            "cases": [],
        }
        for case in cases:
            retrievals, rerankings = [], []
            llm.calls = []
            started = time.perf_counter()
            queries = await service.generate_query([{"role": "user", "content": case["question"]}])
            generation_seconds = time.perf_counter() - started
            started = time.perf_counter()
            fused = await pipeline.get_relevant_docs(["scifact"], queries, top_k=args.top_k)
            gold = case["gold_chunk_ids"]
            pre = [i for r in retrievals for i in r["pre_rerank"]]
            post_ids = [i for r in rerankings for i in r["post_rerank"][: args.top_k]]
            row = {
                **case,
                "generated_queries": queries.model_dump(),
                "generation_seconds": generation_seconds,
                "retrieval_seconds": time.perf_counter() - started,
                "llm_calls": llm.calls,
                "retrievals": retrievals,
                "rerankings": rerankings,
                "fused_chunk_ids": [c.id for c in fused],
                "pre_rerank": chunk_recall(gold, pre),
                "post_rerank_union": chunk_recall(gold, post_ids),
                "final": chunk_recall(gold, [c.id for c in fused]),
            }
            report["cases"].append(row)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            print(case["id"], "queries", len(queries.query_list), "final", row["final"], flush=True)
        report["completed"] = True
        args.output.write_text(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["source-root", "env-file", "corpus", "cases", "cache", "output"]:
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--candidates", type=int, default=50)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--output-tokens", type=int, default=1024)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
