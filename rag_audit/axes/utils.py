from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from rag_audit.models import AuditChunk, AuditDocument


def clamp_score(score: float) -> float:
    return max(0.0, min(100.0, float(score)))


def histogram(values: Sequence[float | int], bins: int = 20) -> list[dict[str, Any]]:
    if not values:
        return []
    mn, mx = min(values), max(values)
    if mn == mx:
        return [{"bin_start": mn, "bin_end": mx, "count": len(values)}]
    step = (mx - mn) / bins
    result = []
    for i in range(bins):
        lo = mn + i * step
        hi = mn + (i + 1) * step
        count = (
            sum(1 for v in values if lo <= v < hi)
            if i < bins - 1
            else sum(1 for v in values if lo <= v <= hi)
        )
        result.append({"bin_start": round(lo, 1), "bin_end": round(hi, 1), "count": count})
    return result


def chunks_by_doc(chunks: Sequence[AuditChunk]) -> dict[str, list[AuditChunk]]:
    grouped: dict[str, list[AuditChunk]] = {}
    for chunk in chunks:
        grouped.setdefault(chunk.document_id, []).append(chunk)
    return grouped


def doc_map(documents: Sequence[AuditDocument]) -> dict[str, AuditDocument]:
    return {doc.id: doc for doc in documents}


def source_name(doc: AuditDocument | None) -> str:
    return doc.source_name if doc and doc.source_name else "Inconnu"


def now_utc() -> datetime:
    return datetime.now(UTC)


def normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
