from __future__ import annotations

import collections
import re
from difflib import SequenceMatcher

from sklearn.feature_extraction.text import TfidfVectorizer

from rag_audit.models import AuditChunk, AuditDocument
from rag_audit.stopwords import get_stopwords_for_sklearn

KV_PATTERNS = {
    "sla": re.compile(r"SLA\s*[:=]\s*([^\n,;]{3,50})", re.I),
    "version": re.compile(r"version\s*[:=]\s*([^\n,;]{1,30})", re.I),
    "port": re.compile(r"port\s*[:=]\s*(\d{2,5})", re.I),
    "url": re.compile(r"(?:url|endpoint|uri)\s*[:=]\s*(https?://[^\s,;\"']{5,200})", re.I),
    "date": re.compile(
        r"(?:date|échéance|deadline)\s*[:=]\s*(\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4})", re.I
    ),
    "timeout": re.compile(r"timeout\s*[:=]\s*(\d+\s*(?:ms|s|sec|min)?)", re.I),
    "limit": re.compile(r"(?:limit|max|maximum)\s*[:=]\s*(\d[\d\s]*\w*)", re.I),
}

ENTITY_PATTERNS = {
    "date": re.compile(r"\b(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})\b"),
    "version": re.compile(r"\bv?(\d+\.\d+(?:\.\d+)?(?:-\w+)?)\b"),
    "url": re.compile(r"(https?://[^\s<>\"']{5,200})"),
    "ip": re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b"),
}


def run(documents: list[AuditDocument], chunks: list[AuditChunk], config: dict):
    if len(chunks) < 3:
        return 100.0, {"total_chunks": len(chunks)}, {}, {"message": "Trop peu de chunks"}

    min_freq = config.get("min_term_frequency", 3)
    lev_threshold = config.get("levenshtein_threshold", 0.85)
    doc_terms = _extract_doc_terms(chunks, min_freq)
    variants, variant_note = _detect_variants(doc_terms, lev_threshold)
    kv_conflicts = _detect_kv_conflicts(chunks)
    entity_conflicts = _detect_entity_conflicts(chunks)

    total_docs = len({chunk.document_id for chunk in chunks})
    conflict_count = sum(len(v["conflicting_values"]) for v in kv_conflicts)
    conflict_score = max(0, 100 * (1 - (conflict_count / max(total_docs, 1)) * 5))
    term_consistency = max(0, 100 - len(variants) * 2)
    entity_conflict_count = sum(
        len(e.get("values", [])) - 1 for e in entity_conflicts if len(e.get("values", [])) > 1
    )
    entity_score = max(0, 100 * (1 - entity_conflict_count / max(total_docs * 3, 1)))
    score = 0.40 * conflict_score + 0.30 * term_consistency + 0.30 * entity_score

    metrics = {
        "total_chunks": len(chunks),
        "total_docs": total_docs,
        "kv_conflict_count": conflict_count,
        "kv_conflict_keys": len(kv_conflicts),
        "terminology_variant_groups": len(variants),
        "entity_conflicts": entity_conflict_count,
        "sub_scores": {
            "kv_conflicts": round(conflict_score, 1),
            "terminology": round(term_consistency, 1),
            "entities": round(entity_score, 1),
        },
    }
    chart_data = {
        "conflict_bar": [
            {
                "key": kv["key"],
                "conflict_count": len(kv["conflicting_values"]),
                "values": kv["conflicting_values"][:5],
            }
            for kv in kv_conflicts[:20]
        ],
        "variant_groups": [
            {
                "canonical": variant["canonical"],
                "variants": variant["variants"][:10],
                "doc_count": variant["doc_count"],
            }
            for variant in variants[:30]
        ],
        "entity_summary": _entity_summary(entity_conflicts),
    }
    details = {
        "kv_conflicts": kv_conflicts[:50],
        "terminology_variants": variants[:50],
        "entity_conflicts": entity_conflicts[:50],
    }
    if variant_note:
        details["warnings"] = [variant_note]
    return score, metrics, chart_data, details


def _extract_doc_terms(chunks: list[AuditChunk], _min_freq: int):
    doc_texts = collections.defaultdict(list)
    for chunk in chunks:
        doc_texts[chunk.document_id].append(chunk.content)
    doc_ids = list(doc_texts.keys())
    texts = [" ".join(doc_texts[doc_id]) for doc_id in doc_ids]
    if len(texts) < 2:
        return {}

    vectorizer = TfidfVectorizer(
        max_features=5000,
        ngram_range=(1, 1),
        min_df=1,
        max_df=0.95,
        stop_words=get_stopwords_for_sklearn(),
    )
    tfidf = vectorizer.fit_transform(texts)
    feature_names = vectorizer.get_feature_names_out()

    doc_terms = {}
    for i, doc_id in enumerate(doc_ids):
        row = tfidf[i].toarray().flatten()
        top_indices = row.argsort()[-20:][::-1]
        doc_terms[doc_id] = [str(feature_names[j]) for j in top_indices if row[j] > 0]
    return doc_terms


def _detect_variants(doc_terms, threshold):
    all_terms = collections.Counter()
    for terms in doc_terms.values():
        all_terms.update(terms)

    stem_groups = collections.defaultdict(set)
    for term in all_terms:
        stem = _simple_stem(term)
        stem_groups[stem].add(term)

    variants = []
    for terms in stem_groups.values():
        if len(terms) < 2:
            continue
        terms_list = sorted(terms)
        is_variant = any(
            SequenceMatcher(None, terms_list[i], terms_list[j]).ratio() >= threshold
            and terms_list[i] != terms_list[j]
            for i in range(len(terms_list))
            for j in range(i + 1, len(terms_list))
        )
        if is_variant:
            canonical = max(terms, key=lambda term: all_terms[term])
            variants.append(
                {
                    "canonical": canonical,
                    "variants": [term for term in terms if term != canonical],
                    "doc_count": sum(
                        1
                        for doc_terms_list in doc_terms.values()
                        if any(term in doc_terms_list for term in terms)
                    ),
                }
            )
    return sorted(variants, key=lambda v: v["doc_count"], reverse=True), ""


def _simple_stem(term: str) -> str:
    normalized = term.lower()
    for suffix in (
        "ements",
        "ement",
        "ations",
        "ation",
        "iques",
        "ique",
        "ités",
        "ité",
        "ées",
        "ée",
        "ers",
        "er",
        "es",
        "s",
    ):
        if len(normalized) > len(suffix) + 3 and normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized[:6]


def _detect_kv_conflicts(chunks: list[AuditChunk]):
    kv_map = collections.defaultdict(lambda: collections.defaultdict(set))
    for chunk in chunks:
        for key, pattern in KV_PATTERNS.items():
            for value in pattern.findall(chunk.content):
                kv_map[key][value.strip().lower()].add(chunk.document_id)

    conflicts = []
    for key, values in kv_map.items():
        if len(values) > 1:
            conflicts.append(
                {
                    "key": key,
                    "conflicting_values": [
                        {"value": value, "doc_count": len(docs), "doc_ids": list(docs)[:5]}
                        for value, docs in sorted(values.items(), key=lambda item: -len(item[1]))
                    ],
                    "total_values": len(values),
                }
            )
    return sorted(conflicts, key=lambda conflict: conflict["total_values"], reverse=True)


def _detect_entity_conflicts(chunks: list[AuditChunk]):
    entity_map = collections.defaultdict(lambda: collections.defaultdict(set))
    for chunk in chunks:
        for entity_type, pattern in ENTITY_PATTERNS.items():
            for value in pattern.findall(chunk.content):
                entity_map[entity_type][value].add(chunk.document_id)

    return [
        {
            "entity_type": entity_type,
            "unique_values": len(values),
            "values": [
                {"value": value, "doc_count": len(docs)}
                for value, docs in sorted(values.items(), key=lambda item: -len(item[1]))[:20]
            ],
        }
        for entity_type, values in entity_map.items()
        if values
    ]


def _entity_summary(entity_conflicts):
    return [
        {"type": conflict["entity_type"], "unique_values": conflict["unique_values"]}
        for conflict in entity_conflicts
    ]
