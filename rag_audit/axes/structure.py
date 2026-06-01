from __future__ import annotations

import collections
import math
import re

from rag_audit.models import AuditChunk, AuditDocument
from rag_audit.stopwords import STOPWORDS_ALL

from .utils import doc_map, histogram, source_name


def run(documents: list[AuditDocument], chunks: list[AuditChunk], config: dict):
    if not chunks:
        return 100.0, {"total_chunks": 0}, {}, {"message": "Aucun chunk"}

    docs = doc_map(documents)
    ordered = sorted(chunks, key=lambda c: (c.document_id, c.chunk_index))
    min_tokens = config.get("min_chunk_tokens", 50)
    max_tokens = config.get("max_chunk_tokens", 1024)
    optimal_tokens = config.get("optimal_chunk_tokens", 512)

    total = len(ordered)
    token_counts = [c.token_count or len(c.content.split()) for c in ordered]
    mean_tc = sum(token_counts) / total
    variance = sum((t - mean_tc) ** 2 for t in token_counts) / total
    std_tc = math.sqrt(variance)
    too_small = sum(1 for t in token_counts if t < min_tokens)
    too_large = sum(1 for t in token_counts if t > max_tokens)
    outlier_ratio = (too_small + too_large) / total

    densities = []
    for chunk in ordered:
        words = re.findall(r"\w+", chunk.content.lower())
        densities.append(1 - sum(1 for word in words if word in STOPWORDS_ALL) / len(words) if words else 0)
    avg_density = sum(densities) / len(densities)

    sentence_counts = []
    words_per_sentence = []
    chars_per_word = []
    for chunk in ordered:
        sentences = [s.strip() for s in re.split(r"[.!?]+", chunk.content) if s.strip()]
        sentence_counts.append(len(sentences))
        words = chunk.content.split()
        if sentences:
            words_per_sentence.append(len(words) / max(len(sentences), 1))
        if words:
            chars_per_word.append(sum(len(word) for word in words) / len(words))

    avg_sentences = sum(sentence_counts) / total if sentence_counts else 0
    avg_wps = sum(words_per_sentence) / len(words_per_sentence) if words_per_sentence else 0
    avg_cpw = sum(chars_per_word) / len(chars_per_word) if chars_per_word else 0
    overlaps = _compute_overlaps(ordered)

    cv = std_tc / mean_tc if mean_tc > 0 else 1
    uniformity_score = max(0, 100 * (1 - cv))
    outlier_score = max(0, 100 * (1 - outlier_ratio * 3))
    density_score = min(100, avg_density * 150)
    readability_score = 100
    if avg_wps > 30:
        readability_score -= min(40, (avg_wps - 30) * 2)
    if avg_sentences < 2:
        readability_score -= 20
    readability_score = max(0, readability_score)
    score = (
        0.30 * uniformity_score
        + 0.25 * outlier_score
        + 0.25 * density_score
        + 0.20 * readability_score
    )

    metrics = {
        "total_chunks": total,
        "mean_tokens": round(mean_tc, 1),
        "std_tokens": round(std_tc, 1),
        "min_tokens_actual": min(token_counts),
        "max_tokens_actual": max(token_counts),
        "too_small": too_small,
        "too_large": too_large,
        "outlier_ratio": round(outlier_ratio, 4),
        "avg_density": round(avg_density, 4),
        "avg_sentences_per_chunk": round(avg_sentences, 1),
        "avg_words_per_sentence": round(avg_wps, 1),
        "avg_chars_per_word": round(avg_cpw, 1),
        "avg_overlap": round(sum(overlaps) / max(len(overlaps), 1), 4),
        "sub_scores": {
            "uniformity": round(uniformity_score, 1),
            "outliers": round(outlier_score, 1),
            "density": round(density_score, 1),
            "readability": round(readability_score, 1),
        },
    }

    source_stats = collections.defaultdict(list)
    for i, chunk in enumerate(ordered):
        source_stats[source_name(docs.get(chunk.document_id))].append(token_counts[i])
    source_violin = []
    for source, counts in source_stats.items():
        q1, median, q3 = _quartiles(counts)
        source_violin.append(
            {
                "source": source,
                "count": len(counts),
                "mean": round(sum(counts) / len(counts), 1),
                "min": min(counts),
                "max": max(counts),
                "q1": q1,
                "median": median,
                "q3": q3,
            }
        )

    chart_data = {
        "token_histogram": histogram(token_counts, bins=25),
        "source_violin": source_violin,
        "size_density_scatter": [
            {
                "tokens": token_counts[i],
                "density": round(densities[i], 3),
                "doc_title": (docs.get(chunk.document_id).title if docs.get(chunk.document_id) else "")[:50],
            }
            for i, chunk in enumerate(ordered[:500])
        ],
        "thresholds": {"min": min_tokens, "max": max_tokens, "optimal": optimal_tokens},
    }

    details = {
        "too_small_chunks": [
            {
                "chunk_id": chunk.id,
                "doc_title": (docs.get(chunk.document_id).title if docs.get(chunk.document_id) else "")[:80],
                "tokens": token_counts[i],
            }
            for i, chunk in enumerate(ordered)
            if token_counts[i] < min_tokens
        ][:30],
        "too_large_chunks": [
            {
                "chunk_id": chunk.id,
                "doc_title": (docs.get(chunk.document_id).title if docs.get(chunk.document_id) else "")[:80],
                "tokens": token_counts[i],
            }
            for i, chunk in enumerate(ordered)
            if token_counts[i] > max_tokens
        ][:30],
    }
    return score, metrics, chart_data, details


def _compute_overlaps(chunks: list[AuditChunk]) -> list[float]:
    overlaps = []
    prev_doc = None
    prev_tokens = set()
    for chunk in chunks:
        tokens = set(re.findall(r"\w+", chunk.content.lower()))
        if chunk.document_id == prev_doc and tokens and prev_tokens:
            union = len(tokens | prev_tokens)
            overlaps.append(len(tokens & prev_tokens) / union if union else 0)
        prev_doc = chunk.document_id
        prev_tokens = tokens
    return overlaps


def _quartiles(values):
    sorted_values = sorted(values)
    n = len(sorted_values)
    if n == 0:
        return 0, 0, 0
    return sorted_values[n // 4], sorted_values[n // 2], sorted_values[3 * n // 4]
