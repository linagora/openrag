from __future__ import annotations

import math

import numpy as np
from sklearn.cluster import KMeans
from sklearn.decomposition import NMF, PCA, TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.neighbors import LocalOutlierFactor
from sklearn.preprocessing import normalize

from rag_audit.models import AuditChunk, AuditDocument
from rag_audit.stopwords import get_stopwords_for_sklearn

from .utils import doc_map, source_name


def run(documents: list[AuditDocument], chunks: list[AuditChunk], config: dict):
    if len(chunks) < 5:
        return (
            100.0,
            {"total_chunks": len(chunks)},
            {},
            {"message": "Trop peu de chunks pour l'analyse de couverture"},
        )

    docs = doc_map(documents)
    max_features = config.get("tfidf_max_features", 10000)
    n_components = config.get("svd_components", 50)
    max_topics = config.get("max_topics", 20)
    contamination = config.get("outlier_contamination", 0.05)
    texts = [chunk.content for chunk in chunks]
    n_docs = len(texts)

    vectorizer = TfidfVectorizer(
        max_features=max_features,
        ngram_range=(1, 2),
        stop_words=get_stopwords_for_sklearn(),
        min_df=2,
        max_df=0.95,
    )
    tfidf_matrix = vectorizer.fit_transform(texts)
    feature_names = vectorizer.get_feature_names_out()

    actual_components = min(n_components, tfidf_matrix.shape[1] - 1, n_docs - 1)
    actual_components = max(2, actual_components)
    svd = TruncatedSVD(n_components=actual_components, random_state=42)
    svd_normed = normalize(svd.fit_transform(tfidf_matrix))

    k_topics = max(3, min(int(math.sqrt(n_docs / 2)), max_topics))
    k_topics = min(k_topics, tfidf_matrix.shape[1])
    nmf = NMF(n_components=k_topics, random_state=42, max_iter=300)
    nmf_matrix = nmf.fit_transform(tfidf_matrix)

    topics = []
    for i, component in enumerate(nmf.components_):
        top_idx = component.argsort()[-10:][::-1]
        topics.append(
            {
                "id": i,
                "terms": [str(feature_names[j]) for j in top_idx],
                "weight": float(component.sum()),
            }
        )

    labels = KMeans(n_clusters=k_topics, random_state=42, n_init=10).fit_predict(svd_normed)
    cluster_sizes = {}
    for label in labels:
        cluster_sizes[int(label)] = cluster_sizes.get(int(label), 0) + 1

    if n_docs >= 20:
        lof = LocalOutlierFactor(contamination=contamination, n_neighbors=min(20, n_docs - 1))
        outlier_labels = lof.fit_predict(svd_normed)
        outlier_count = int((outlier_labels == -1).sum())
    else:
        outlier_labels = np.ones(n_docs)
        outlier_count = 0
    outlier_ratio = outlier_count / n_docs
    coords_2d = PCA(n_components=2, random_state=42).fit_transform(svd_normed)

    gini = _gini(sorted(cluster_sizes.values()))
    balance_score = (1 - gini) * 100
    topic_doc_counts = [int((nmf_matrix[:, i] > 0.01).sum()) for i in range(k_topics)]
    covered_topics = sum(1 for count in topic_doc_counts if count >= 3)
    coverage_ratio = covered_topics / k_topics if k_topics > 0 else 1
    coverage_score = coverage_ratio * 100
    outlier_score = max(0, (1 - outlier_ratio * 5)) * 100

    coherences = []
    for i in range(k_topics):
        mask = labels == i
        if mask.sum() > 1:
            cluster_vecs = svd_normed[mask]
            centroid = cluster_vecs.mean(axis=0)
            coherences.append(float((cluster_vecs @ centroid).mean()))
    avg_coherence = sum(coherences) / len(coherences) if coherences else 0.5
    coherence_score = avg_coherence * 100
    score = (
        0.30 * balance_score
        + 0.30 * coverage_score
        + 0.20 * outlier_score
        + 0.20 * coherence_score
    )

    metrics = {
        "total_chunks": n_docs,
        "k_topics": k_topics,
        "gini_coefficient": round(gini, 4),
        "balance_score": round(balance_score, 1),
        "covered_topics": covered_topics,
        "coverage_ratio": round(coverage_ratio, 4),
        "outlier_count": outlier_count,
        "outlier_ratio": round(outlier_ratio, 4),
        "avg_coherence": round(avg_coherence, 4),
        "sub_scores": {
            "balance": round(balance_score, 1),
            "coverage": round(coverage_score, 1),
            "outliers": round(outlier_score, 1),
            "coherence": round(coherence_score, 1),
        },
    }

    sample_n = min(500, n_docs)
    indices = (
        np.random.default_rng(42).choice(n_docs, sample_n, replace=False)
        if n_docs > sample_n
        else np.arange(n_docs)
    )
    scatter = [
        {
            "x": float(coords_2d[idx, 0]),
            "y": float(coords_2d[idx, 1]),
            "topic": int(labels[idx]),
            "outlier": int(outlier_labels[idx] == -1),
            "doc_title": (docs.get(chunks[idx].document_id).title if docs.get(chunks[idx].document_id) else "")[:50],
        }
        for idx in indices
    ]

    source_topic = {}
    for i, chunk in enumerate(chunks):
        source = source_name(docs.get(chunk.document_id))
        topic = int(labels[i])
        source_topic[(source, topic)] = source_topic.get((source, topic), 0) + 1

    chart_data = {
        "scatter_2d": scatter,
        "topic_volumes": [
            {"topic": i, "terms": topics[i]["terms"][:5], "count": cluster_sizes.get(i, 0)}
            for i in range(k_topics)
        ],
        "source_topic_stacked": [
            {"source": source, "topic": topic, "count": count}
            for (source, topic), count in source_topic.items()
        ],
        "topics_table": topics,
    }
    details = {
        "topics": topics,
        "cluster_sizes": cluster_sizes,
        "outlier_chunks": [
            {
                "chunk_id": chunks[i].id,
                "doc_title": (docs.get(chunks[i].document_id).title if docs.get(chunks[i].document_id) else "")[:80],
            }
            for i in range(n_docs)
            if outlier_labels[i] == -1
        ][:50],
    }
    return score, metrics, chart_data, details


def _gini(values):
    if not values or sum(values) == 0:
        return 0
    n = len(values)
    cumsum = sum((i + 1) * value for i, value in enumerate(sorted(values)))
    return (2 * cumsum) / (n * sum(values)) - (n + 1) / n
