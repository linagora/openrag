from __future__ import annotations

import collections
import logging
import re

from rag_audit.models import AuditChunk, AuditDocument

from .utils import doc_map, histogram

logger = logging.getLogger(__name__)

PII_PATTERNS = {
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    "phone_fr": re.compile(r"\b(?:\+33|0)\s*[1-9](?:[\s.-]*\d{2}){4}\b"),
    "phone_intl": re.compile(r"\b\+\d{1,3}[\s.-]?\d{4,14}\b"),
    "api_key": re.compile(r"\b(?:sk|pk|api[_-]?key)[_-]?[A-Za-z0-9]{20,}\b", re.I),
    "ip_address": re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    "secret_generic": re.compile(r"(?:password|secret|token|credentials?)\s*[:=]\s*\S+", re.I),
}


def run(documents: list[AuditDocument], chunks: list[AuditChunk], config: dict):
    if not chunks:
        return 100.0, {"total_chunks": 0}, {}, {"message": "Aucun chunk à analyser"}

    docs = doc_map(documents)
    total_chunks = len(chunks)
    total_docs = len(documents)

    hash_counts = collections.Counter(c.content_hash for c in chunks if c.content_hash)
    exact_dup_count = sum(v - 1 for v in hash_counts.values() if v > 1)
    exact_dup_ratio = exact_dup_count / total_chunks if total_chunks else 0

    neardup_pairs, neardup_ratio, neardup_note = _neardup_analysis(chunks, docs, config)
    boilerplate_lines, boilerplate_ratio = _boilerplate_analysis(chunks, config)
    lang_dist, lang_score, lang_note = _language_analysis(documents, chunks)
    pii_findings, pii_ratio = _pii_analysis(chunks, docs)

    uniqueness_score = max(0, 100 * (1 - exact_dup_ratio * 5))
    neardup_score = max(0, 100 * (1 - neardup_ratio * 3))
    boilerplate_score = max(0, 100 * (1 - boilerplate_ratio * 3))
    pii_score = max(0, 100 * (1 - pii_ratio * 10))

    score = (
        0.30 * uniqueness_score
        + 0.20 * neardup_score
        + 0.20 * boilerplate_score
        + 0.15 * lang_score
        + 0.15 * pii_score
    )

    metrics = {
        "total_chunks": total_chunks,
        "total_docs": total_docs,
        "exact_duplicates": exact_dup_count,
        "exact_dup_ratio": round(exact_dup_ratio, 4),
        "neardup_pairs": len(neardup_pairs),
        "neardup_ratio": round(neardup_ratio, 4),
        "boilerplate_lines": len(boilerplate_lines),
        "boilerplate_ratio": round(boilerplate_ratio, 4),
        "language_distribution": lang_dist,
        "language_homogeneity": round(lang_score, 1),
        "pii_findings_count": len(pii_findings),
        "pii_ratio": round(pii_ratio, 4),
        "sub_scores": {
            "uniqueness": round(uniqueness_score, 1),
            "neardup": round(neardup_score, 1),
            "boilerplate": round(boilerplate_score, 1),
            "language": round(lang_score, 1),
            "pii": round(pii_score, 1),
        },
    }

    lengths = [len(c.content) for c in chunks]
    doc_dup_counts = collections.Counter()
    for content_hash, count in hash_counts.items():
        if count > 1:
            for chunk in chunks:
                if chunk.content_hash == content_hash:
                    doc_dup_counts[chunk.document_id] += 1

    chart_data = {
        "length_histogram": histogram(lengths, bins=20),
        "dup_distribution": [
            {"doc_id": did, "count": count} for did, count in doc_dup_counts.most_common(20)
        ],
        "language_pie": [{"language": lang, "count": count} for lang, count in lang_dist.items()],
        "pii_by_type": _pii_by_type(pii_findings),
    }

    details = {
        "exact_dup_hashes": [
            {"hash": content_hash, "count": count}
            for content_hash, count in hash_counts.most_common(20)
            if count > 1
        ],
        "neardup_pairs": neardup_pairs[:50],
        "boilerplate_lines": boilerplate_lines[:30],
        "pii_findings": pii_findings[:50],
    }
    warnings = [note for note in (neardup_note, lang_note) if note]
    if warnings:
        details["warnings"] = warnings
    return score, metrics, chart_data, details


def _neardup_analysis(chunks: list[AuditChunk], docs: dict[str, AuditDocument], config: dict):
    try:
        from datasketch import MinHash, MinHashLSH
    except ImportError:
        logger.warning("datasketch not installed, skipping near-dup analysis")
        return [], 0.0, "datasketch not installed; near-duplicate analysis skipped"

    num_perm = config.get("minhash_num_perm", 128)
    threshold = config.get("neardup_jaccard_threshold", 0.5)
    sample = chunks[:2000]
    minhashes = {}
    for chunk in sample:
        m = MinHash(num_perm=num_perm)
        words = chunk.content.lower().split()
        for i in range(len(words) - 2):
            m.update(" ".join(words[i : i + 3]).encode("utf-8"))
        minhashes[chunk.id] = (m, chunk.document_id)

    lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
    for chunk_id, (mh, _) in minhashes.items():
        try:
            lsh.insert(chunk_id, mh)
        except ValueError:
            pass

    pairs = []
    seen = set()
    for chunk_id, (mh, doc_id) in minhashes.items():
        for result in lsh.query(mh):
            if result == chunk_id:
                continue
            pair_key = tuple(sorted([chunk_id, result]))
            if pair_key not in seen:
                seen.add(pair_key)
                pairs.append(
                    {
                        "chunk_a": chunk_id,
                        "chunk_b": result,
                        "doc_a": doc_id,
                        "doc_a_name": _doc_display_name(docs, doc_id),
                        "doc_b": minhashes[result][1],
                        "doc_b_name": _doc_display_name(docs, minhashes[result][1]),
                    }
                )
    return pairs, len(pairs) / max(len(sample), 1), ""


def _doc_display_name(docs: dict[str, AuditDocument], doc_id: str) -> str:
    doc = docs.get(doc_id)
    if not doc:
        return doc_id
    return doc.title or doc.metadata.get("filename") or doc.metadata.get("original_filename") or doc_id


def _boilerplate_analysis(chunks: list[AuditChunk], config: dict):
    threshold = config.get("boilerplate_freq_threshold", 0.3)
    total_docs = max(len({c.document_id for c in chunks}), 1)
    line_doc_count = collections.Counter()
    for chunk in chunks:
        seen_in_doc = set()
        for line in chunk.content.split("\n"):
            normalized = line.strip().lower()
            if len(normalized) > 10 and normalized not in seen_in_doc:
                seen_in_doc.add(normalized)
                line_doc_count[normalized] += 1

    boilerplate = [
        {"line": line[:200], "doc_count": count, "ratio": round(count / total_docs, 3)}
        for line, count in line_doc_count.most_common(50)
        if count / total_docs > threshold
    ]
    total_boilerplate_occurrences = sum(
        count for _, count in line_doc_count.items() if count / total_docs > threshold
    )
    total_lines = sum(len(c.content.split("\n")) for c in chunks)
    return boilerplate, total_boilerplate_occurrences / max(total_lines, 1)


def _language_analysis(documents: list[AuditDocument], chunks: list[AuditChunk]):
    try:
        import langid
    except ImportError:
        logger.warning("langid not installed, skipping language analysis")
        return {"unknown": len(documents)}, 50.0, "langid not installed; language analysis skipped"

    lang_counts = collections.Counter()
    for chunk in chunks[:200]:
        text = chunk.content[:500]
        if len(text.strip()) >= 20:
            lang, _ = langid.classify(text)
            lang_counts[lang] += 1
    if not lang_counts:
        return {"unknown": len(documents)}, 50.0, ""
    total = sum(lang_counts.values())
    return dict(lang_counts), min(100, lang_counts.most_common(1)[0][1] / total * 100), ""


def _pii_analysis(chunks: list[AuditChunk], docs: dict[str, AuditDocument]):
    findings = []
    total_with_pii = 0
    for chunk in chunks[:1000]:
        chunk_pii = []
        for pii_type, pattern in PII_PATTERNS.items():
            matches = pattern.findall(chunk.content)
            if matches:
                sample = matches[0]
                chunk_pii.append(
                    {
                        "type": pii_type,
                        "count": len(matches),
                        "sample": sample[:50] + "..." if len(sample) > 50 else sample,
                    }
                )
        if chunk_pii:
            total_with_pii += 1
            doc = docs.get(chunk.document_id)
            findings.append(
                {
                    "chunk_id": chunk.id,
                    "doc_id": chunk.document_id,
                    "doc_title": (doc.title if doc else "")[:100],
                    "pii_types": chunk_pii,
                }
            )
    return findings, total_with_pii / max(len(chunks[:1000]), 1)


def _pii_by_type(findings):
    type_counts = collections.Counter()
    for finding in findings:
        for pii in finding["pii_types"]:
            type_counts[pii["type"]] += pii["count"]
    return [{"type": pii_type, "count": count} for pii_type, count in type_counts.most_common()]
