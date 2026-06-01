AXIS_ORDER = ["hygiene", "structure", "coverage", "coherence", "retrievability", "governance"]

AXIS_LABELS = {
    "hygiene": "Hygiène du corpus",
    "structure": "Structure RAG",
    "coverage": "Couverture sémantique",
    "coherence": "Cohérence interne",
    "retrievability": "Retrievability",
    "governance": "Gouvernance & metadata",
}

DEFAULT_CONFIG = {
    "axis_weights": {
        "hygiene": 0.20,
        "structure": 0.15,
        "coverage": 0.20,
        "coherence": 0.15,
        "retrievability": 0.20,
        "governance": 0.10,
    },
    "hygiene": {
        "minhash_num_perm": 64,
        "neardup_jaccard_threshold": 0.5,
        "boilerplate_freq_threshold": 0.3,
    },
    "structure": {
        "min_chunk_tokens": 80,
        "max_chunk_tokens": 1024,
        "optimal_chunk_tokens": 768,
    },
    "coverage": {
        "tfidf_max_features": 5000,
        "svd_components": 30,
        "max_topics": 10,
        "outlier_contamination": 0.05,
    },
    "coherence": {
        "min_term_frequency": 3,
        "levenshtein_threshold": 0.85,
    },
    "retrievability": {
        "bm25_top_k": 5,
        "queries_per_doc": 1,
        "recall_k_values": [1, 5, 10],
    },
    "governance": {
        "required_fields": ["author", "source_modified_at", "doc_type", "path"],
        "staleness_days": 180,
    },
}


def merge_config(config: dict | None = None) -> dict:
    merged = {
        key: value.copy() if isinstance(value, dict) else value
        for key, value in DEFAULT_CONFIG.items()
    }
    for key, value in (config or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged
