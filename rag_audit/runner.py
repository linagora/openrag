from __future__ import annotations

import importlib
import time

from rag_audit.config import merge_config
from rag_audit.models import AuditChunk, AuditDocument, AxisResult

AXIS_MODULES = {
    "hygiene": "rag_audit.axes.hygiene",
    "structure": "rag_audit.axes.structure",
    "coverage": "rag_audit.axes.coverage",
    "coherence": "rag_audit.axes.coherence",
    "governance": "rag_audit.axes.governance",
}


def grade(score: float) -> str:
    if score >= 80:
        return "A"
    if score >= 60:
        return "B"
    if score >= 40:
        return "C"
    if score >= 20:
        return "D"
    return "E"


def run_axis(
    axis: str,
    documents: list[AuditDocument],
    chunks: list[AuditChunk],
    config: dict | None = None,
) -> AxisResult:
    effective = merge_config(config)
    if axis not in AXIS_MODULES:
        raise ValueError(f"Unknown audit axis: {axis}")
    started = time.time()
    module = importlib.import_module(AXIS_MODULES[axis])
    score, metrics, chart_data, details = module.run(
        documents, chunks, effective.get(axis, {})
    )
    duration = time.time() - started
    return AxisResult(
        axis=axis,
        score=max(0.0, min(100.0, float(score))),
        metrics=metrics,
        chart_data=chart_data,
        details=details,
        duration_seconds=duration,
    )
