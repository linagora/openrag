from __future__ import annotations

import collections
from datetime import timedelta

from rag_audit.models import AuditChunk, AuditDocument

from .utils import histogram, normalize_datetime, now_utc


def run(documents: list[AuditDocument], _chunks: list[AuditChunk], config: dict):
    if not documents:
        return 100.0, {"total_docs": 0}, {}, {"message": "Aucun document"}

    required_fields = config.get("required_fields", ["author", "source_modified_at", "doc_type", "path"])
    optional_fields = config.get("optional_fields", [])
    staleness_days = config.get("staleness_days", 180)
    total = len(documents)
    now = now_utc()

    field_completeness = {}
    for field in required_fields:
        filled = sum(1 for doc in documents if _field_value(doc, field))
        field_completeness[field] = {
            "filled": filled,
            "total": total,
            "ratio": round(filled / total, 4),
        }
    avg_completeness = sum(fc["ratio"] for fc in field_completeness.values()) / max(
        len(field_completeness), 1
    )

    optional_completeness = {}
    for field in optional_fields:
        filled = sum(1 for doc in documents if _field_value(doc, field))
        optional_completeness[field] = {
            "filled": filled,
            "total": total,
            "ratio": round(filled / total, 4),
        }
    optional_avg = (
        sum(fc["ratio"] for fc in optional_completeness.values()) / len(optional_completeness)
        if optional_completeness
        else 0.0
    )
    metadata_score = (0.8 * avg_completeness + 0.2 * optional_avg) * 100 if optional_fields else avg_completeness * 100

    source_completeness = collections.defaultdict(lambda: {"total": 0, "filled": 0.0})
    for doc in documents:
        source = doc.source_name or "Inconnu"
        source_completeness[source]["total"] += 1
        filled_count = sum(1 for field in required_fields if _field_value(doc, field))
        source_completeness[source]["filled"] += filled_count / max(len(required_fields), 1)

    stale_threshold = now - timedelta(days=staleness_days)
    stale_docs = []
    age_days_list = []
    for doc in documents:
        modified_at = normalize_datetime(doc.source_modified_at) or normalize_datetime(doc.created_at)
        if modified_at:
            age = (now - modified_at).days
            age_days_list.append(age)
            if modified_at < stale_threshold:
                stale_docs.append(
                    {
                        "doc_id": doc.id,
                        "title": doc.title[:80],
                        "age_days": age,
                        "source": doc.source_name or "Inconnu",
                    }
                )

    stale_ratio = len(stale_docs) / total
    freshness_score = max(0, (1 - stale_ratio) * 100)
    orphan_docs = [
        {"doc_id": doc.id, "title": doc.title[:80], "source": doc.source_name or "Inconnu"}
        for doc in documents
        if not doc.path and not doc.source_url
    ]
    orphan_ratio = len(orphan_docs) / total
    orphan_score = max(0, (1 - orphan_ratio * 3) * 100)
    path_graph, connectivity_score = _build_path_graph(documents)

    score = (
        0.30 * metadata_score
        + 0.25 * freshness_score
        + 0.25 * orphan_score
        + 0.20 * connectivity_score
    )

    metrics = {
        "total_docs": total,
        "field_completeness": field_completeness,
        "optional_field_completeness": optional_completeness,
        "avg_completeness": round(avg_completeness, 4),
        "optional_avg_completeness": round(optional_avg, 4),
        "stale_count": len(stale_docs),
        "stale_ratio": round(stale_ratio, 4),
        "staleness_threshold_days": staleness_days,
        "orphan_count": len(orphan_docs),
        "orphan_ratio": round(orphan_ratio, 4),
        "connectivity_score": round(connectivity_score, 1),
        "sub_scores": {
            "completeness": round(metadata_score, 1),
            "freshness": round(freshness_score, 1),
            "orphans": round(orphan_score, 1),
            "connectivity": round(connectivity_score, 1),
        },
    }

    source_problems = collections.Counter()
    for item in stale_docs + orphan_docs:
        source_problems[item["source"]] += 1
    for field, data in field_completeness.items():
        if data["total"] - data["filled"] > 0:
            for doc in documents:
                if not _field_value(doc, field):
                    source_problems[doc.source_name or "Inconnu"] += 1

    chart_data = {
        "completeness_bar": [
            {
                "field": field,
                "ratio": data["ratio"],
                "filled": data["filled"],
                "total": data["total"],
            }
            for field, data in field_completeness.items()
        ],
        "source_completeness": [
            {
                "source": source,
                "total": data["total"],
                "avg_completeness": round(data["filled"] / data["total"], 4),
            }
            for source, data in source_completeness.items()
        ],
        "age_histogram": histogram(age_days_list, bins=15) if age_days_list else [],
        "pareto_by_source": [
            {"source": source, "problems": count}
            for source, count in source_problems.most_common(20)
        ],
        "path_graph": path_graph,
    }
    return score, metrics, chart_data, {"stale_docs": stale_docs[:50], "orphan_docs": orphan_docs[:50]}


def _build_path_graph(documents: list[AuditDocument]):
    paths = {doc.id: doc.path for doc in documents if doc.path}
    if len(paths) < 2:
        return {"nodes": [], "edges": []}, 50.0

    prefix_groups = collections.defaultdict(list)
    for doc_id, path in paths.items():
        parts = path.replace("\\", "/").split("/")
        prefix_groups["/".join(parts[:2]) if len(parts) >= 2 else parts[0]].append(doc_id)

    nodes = [
        {"id": prefix, "type": "prefix", "count": len(doc_ids)}
        for prefix, doc_ids in prefix_groups.items()
    ]
    edges = []
    prefix_list = list(prefix_groups.keys())
    for i in range(len(prefix_list)):
        for j in range(i + 1, len(prefix_list)):
            if prefix_list[i].split("/")[0] == prefix_list[j].split("/")[0]:
                edges.append({"source": prefix_list[i], "target": prefix_list[j], "weight": 1})

    if not edges:
        connectivity = 30.0
    else:
        total_connected = sum(len(ids) for ids in prefix_groups.values() if len(ids) > 1)
        connectivity = min(100, (total_connected / len(paths)) * 100)
    return {"nodes": nodes[:100], "edges": edges[:200]}, connectivity


def _field_value(doc: AuditDocument, field: str):
    if field == "filename":
        return doc.metadata.get("filename") or doc.metadata.get("original_filename") or doc.title
    if field == "source":
        return doc.metadata.get("source") or doc.path or doc.source_url
    if hasattr(doc, field):
        return getattr(doc, field)
    return doc.metadata.get(field)
