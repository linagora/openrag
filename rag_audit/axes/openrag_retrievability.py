from __future__ import annotations

import collections
import time
from typing import Any

from sklearn.feature_extraction.text import TfidfVectorizer

from rag_audit.models import AuditChunk, AuditDocument, AxisResult


async def run_openrag_retrievability(
    *,
    partition: str,
    documents: list[AuditDocument],
    chunks: list[AuditChunk],
    indexer: Any,
    config: dict | None = None,
) -> AxisResult:
    started = time.time()
    config = config or {}
    top_k = int(config.get("top_k", config.get("bm25_top_k", 10)))
    max_queries = int(config.get("max_queries", 500))
    queries_per_doc = int(config.get("queries_per_doc", 1))
    recall_k_values = config.get("recall_k_values", [1, 5, 10])
    recall_k_values = [int(k) for k in recall_k_values if int(k) <= top_k] or [top_k]

    if len(chunks) < 5:
        return AxisResult(
            axis="retrievability",
            score=100.0,
            metrics={"total_chunks": len(chunks)},
            chart_data={},
            details={"message": "Too few chunks"},
            duration_seconds=time.time() - started,
        )

    queries = _generate_queries(documents, chunks, queries_per_doc)[:max_queries]
    if not queries:
        return AxisResult(
            axis="retrievability",
            score=50.0,
            metrics={"total_queries": 0},
            chart_data={},
            details={"message": "No queries generated"},
            duration_seconds=time.time() - started,
        )

    eval_results = []
    reciprocal_ranks = []
    zero_results = 0
    for query_text, expected_doc_id, expected_chunk_id in queries:
        results = await indexer.asearch.remote(
            query=query_text,
            top_k=top_k,
            similarity_threshold=0.0,
            partition=partition,
        )
        hits = [_result_identity(result) for result in results]
        found_at = _first_matching_rank(hits, expected_doc_id, expected_chunk_id)
        if not hits:
            zero_results += 1
        reciprocal_ranks.append(1.0 / found_at if found_at > 0 else 0.0)
        eval_results.append(
            {
                "query": query_text[:100],
                "expected_doc": expected_doc_id,
                "expected_chunk": expected_chunk_id,
                "found_at": found_at,
                "recalls": {str(k): 1 if _has_match(hits[:k], expected_doc_id, expected_chunk_id) else 0 for k in recall_k_values},
            }
        )

    total_queries = len(eval_results)
    mrr = sum(reciprocal_ranks) / total_queries
    zero_ratio = zero_results / total_queries
    recall_at_k = {
        str(k): sum(result["recalls"].get(str(k), 0) for result in eval_results) / total_queries
        for k in recall_k_values
    }
    all_top_docs = {doc_id for result in eval_results for doc_id, _ in [_expected_from_eval(result)] if doc_id}
    diversity = len(all_top_docs) / max(len(documents), 1)
    largest_k = str(max(recall_k_values))
    score = (
        0.35 * mrr * 100
        + 0.30 * recall_at_k.get(largest_k, 0) * 100
        + 0.20 * (1 - zero_ratio) * 100
        + 0.15 * min(diversity, 1.0) * 100
    )

    found_ranks = [result["found_at"] for result in eval_results if result["found_at"] > 0]
    zero_result_queries = [result["query"] for result in eval_results if result["found_at"] == -1][:20]
    metrics = {
        "total_chunks": len(chunks),
        "total_docs": len(documents),
        "total_queries": total_queries,
        "mrr": round(mrr, 4),
        "zero_result_ratio": round(zero_ratio, 4),
        "diversity": round(diversity, 4),
        "recall_at_k": {k: round(v, 4) for k, v in recall_at_k.items()},
        "sub_scores": {
            "mrr": round(mrr * 100, 1),
            f"recall_{largest_k}": round(recall_at_k.get(largest_k, 0) * 100, 1),
            "zero_results": round((1 - zero_ratio) * 100, 1),
            "diversity": round(min(diversity, 1.0) * 100, 1),
        },
    }
    return AxisResult(
        axis="retrievability",
        score=max(0.0, min(100.0, float(score))),
        metrics=metrics,
        chart_data={
            "recall_curve": [
                {"k": int(k), "recall": round(v, 4)}
                for k, v in sorted(recall_at_k.items(), key=lambda item: int(item[0]))
            ],
            "rank_histogram": _histogram(found_ranks, bins=max(recall_k_values)) if found_ranks else [],
            "zero_result_queries": zero_result_queries,
        },
        details={"eval_results": eval_results[:100], "zero_result_queries": zero_result_queries},
        duration_seconds=time.time() - started,
    )


def _generate_queries(
    documents: list[AuditDocument],
    chunks: list[AuditChunk],
    queries_per_doc: int,
) -> list[tuple[str, str, str | None]]:
    queries: list[tuple[str, str, str | None]] = []
    for doc in documents:
        if doc.title and len(doc.title.strip()) > 3:
            queries.append((doc.title.strip(), doc.id, None))
    for chunk in chunks:
        if chunk.heading_path and len(chunk.heading_path.strip()) > 3:
            queries.append((chunk.heading_path.strip(), chunk.document_id, chunk.id))

    doc_texts = collections.defaultdict(list)
    for chunk in chunks:
        doc_texts[chunk.document_id].append(chunk.content)
    if len(doc_texts) >= 2:
        doc_ids = list(doc_texts.keys())
        texts = [" ".join(doc_texts[doc_id]) for doc_id in doc_ids]
        try:
            vectorizer = TfidfVectorizer(ngram_range=(2, 2), max_features=5000, min_df=1, max_df=0.9)
            tfidf = vectorizer.fit_transform(texts)
            feature_names = vectorizer.get_feature_names_out()
            for i, doc_id in enumerate(doc_ids):
                row = tfidf[i].toarray().flatten()
                for j in row.argsort()[-queries_per_doc:][::-1]:
                    if row[j] > 0:
                        queries.append((str(feature_names[j]), doc_id, None))
        except ValueError:
            pass

    seen = set()
    unique = []
    for query, doc_id, chunk_id in queries:
        key = (query.lower(), doc_id, chunk_id)
        if key not in seen:
            seen.add(key)
            unique.append((query, doc_id, chunk_id))
    return unique


def _result_identity(result: Any) -> tuple[str | None, str | None]:
    metadata = dict(getattr(result, "metadata", None) or {})
    doc_id = metadata.get("file_id") or metadata.get("document_id")
    chunk_id = metadata.get("_id") or metadata.get("section_id")
    return (str(doc_id) if doc_id is not None else None, str(chunk_id) if chunk_id is not None else None)


def _first_matching_rank(
    hits: list[tuple[str | None, str | None]],
    expected_doc_id: str,
    expected_chunk_id: str | None,
) -> int:
    for index, hit in enumerate(hits, start=1):
        if _hit_matches(hit, expected_doc_id, expected_chunk_id):
            return index
    return -1


def _has_match(
    hits: list[tuple[str | None, str | None]],
    expected_doc_id: str,
    expected_chunk_id: str | None,
) -> bool:
    return any(_hit_matches(hit, expected_doc_id, expected_chunk_id) for hit in hits)


def _hit_matches(hit: tuple[str | None, str | None], expected_doc_id: str, expected_chunk_id: str | None) -> bool:
    doc_id, chunk_id = hit
    if expected_chunk_id and chunk_id == str(expected_chunk_id):
        return True
    return doc_id == str(expected_doc_id)


def _expected_from_eval(result: dict[str, Any]) -> tuple[str | None, str | None]:
    return result.get("expected_doc"), result.get("expected_chunk")


def _histogram(values: list[int], bins: int) -> list[dict[str, int]]:
    counts = collections.Counter(values)
    return [{"value": i, "count": counts.get(i, 0)} for i in range(1, bins + 1)]
