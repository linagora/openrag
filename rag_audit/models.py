from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class AuditDocument:
    id: str
    title: str = ""
    content_hash: str = ""
    author: str = ""
    source_modified_at: datetime | None = None
    doc_type: str = ""
    path: str = ""
    source_url: str = ""
    source_name: str = ""
    created_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AuditChunk:
    id: str
    document_id: str
    content: str
    content_hash: str = ""
    token_count: int | None = None
    chunk_index: int = 0
    heading_path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AxisResult:
    axis: str
    score: float
    metrics: dict[str, Any]
    chart_data: dict[str, Any]
    details: dict[str, Any]
    duration_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AuditResult:
    overall_score: float
    overall_grade: str
    axis_results: list[AxisResult]
    weights: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
