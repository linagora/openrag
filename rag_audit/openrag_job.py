from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


def _ensure_openrag_path() -> None:
    root = Path(__file__).resolve().parents[1]
    openrag_dir = root / "openrag"
    if openrag_dir.exists() and str(openrag_dir) not in sys.path:
        sys.path.insert(0, str(openrag_dir))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run persisted OpenRAG RAG audits for every partition")
    parser.add_argument(
        "--ray-address",
        default=os.getenv("RAY_ADDRESS", "auto"),
        help="Ray address to connect to. Defaults to RAY_ADDRESS or auto.",
    )
    parser.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--pretty", action="store_true", help="Pretty-print the persisted run JSON")
    return parser


def _init_ray(ray, *, address: str) -> None:
    try:
        ray.init(address=address, ignore_reinit_error=True)
    except ConnectionError as exc:
        raise SystemExit(
            "No running Ray instance was found. Run this job inside the OpenRAG container after the API has started."
        ) from exc


async def _run_partition_audit(*, partition: str, vectordb, indexer, config) -> dict:
    from rag_audit.openrag_runner import execute_openrag_audit_run

    return await execute_openrag_audit_run(
        partition=partition,
        vectordb=vectordb,
        indexer=indexer,
        config={
            "retrievability": {
                "top_k": config.rag_audit.retrievability_top_k,
                "max_queries": config.rag_audit.retrievability_max_queries,
            }
        },
        retention_days=config.rag_audit.retention_days,
    )


async def _audit_partitions(*, partitions: list[str], vectordb, indexer, config) -> list[dict]:
    semaphore = asyncio.Semaphore(max(1, int(config.rag_audit.max_concurrent_partitions)))

    async def run_one(partition: str) -> dict:
        async with semaphore:
            try:
                return await _run_partition_audit(partition=partition, vectordb=vectordb, indexer=indexer, config=config)
            except Exception as exc:
                return {"partition": partition, "status": "failed", "error": str(exc)}

    return await asyncio.gather(*(run_one(partition) for partition in partitions))


async def _discover_partitions(*, vectordb) -> list[str]:
    partition_rows = await vectordb.list_partitions.remote()
    return [row["partition"] for row in partition_rows if row.get("partition")]


def _print_results(results: list[dict], *, pretty: bool) -> None:
    if pretty:
        print(json.dumps(results, ensure_ascii=True, indent=2))
        return
    for run in results:
        print(
            f"partition: {run.get('partition')}\n"
            f"partition_id: {run.get('partition_id')}\n"
            f"run_id: {run.get('run_id')}\n"
            f"status: {run.get('status')}\n"
            f"overall: {run.get('overall_score')} ({run.get('overall_grade')})"
        )


async def _amain() -> int:
    _ensure_openrag_path()

    import ray
    args = _build_parser().parse_args()

    _init_ray(
        ray,
        address=args.ray_address,
    )

    from config import load_config
    from utils.dependencies import get_indexer, get_vectordb

    config = load_config()
    if not config.rag_audit.enabled:
        print("RAG audit is disabled by configuration.")
        return 0

    vectordb = get_vectordb()
    indexer = get_indexer()

    partitions = await _discover_partitions(vectordb=vectordb)
    _print_results(await _audit_partitions(partitions=partitions, vectordb=vectordb, indexer=indexer, config=config), pretty=args.pretty)
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
