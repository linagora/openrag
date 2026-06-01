from __future__ import annotations

from typing import Any

from rag_audit.axes.openrag_retrievability import run_openrag_retrievability
from rag_audit.config import AXIS_ORDER, DEFAULT_CONFIG, merge_config
from rag_audit.models import AuditResult
from rag_audit.openrag_adapter import from_openrag_documents
from rag_audit.runner import grade, run_axis
from rag_audit.sanitize import sanitize_audit_result

OPENRAG_GOVERNANCE_CONFIG = {
    "required_fields": ["file_id", "partition", "filename", "source", "created_at"],
    "optional_fields": ["author", "mimetype", "doc_type", "relationship_id", "parent_id"],
}


async def run_openrag_audit(
    *,
    partition: str,
    openrag_chunks: list[Any],
    file_records: list[dict[str, Any]],
    indexer: Any,
    config: dict | None = None,
    axes: list[str] | None = None,
) -> AuditResult:
    documents, chunks = from_openrag_documents(openrag_chunks, file_records)
    effective = merge_config(config)
    effective["governance"] = {
        **effective.get("governance", {}),
        **OPENRAG_GOVERNANCE_CONFIG,
    }
    selected_axes = axes or list(AXIS_ORDER)
    axis_results = []
    for axis in selected_axes:
        if axis == "retrievability":
            axis_results.append(
                await run_openrag_retrievability(
                    partition=partition,
                    documents=documents,
                    chunks=chunks,
                    indexer=indexer,
                    config=effective.get("retrievability", {}),
                )
            )
        else:
            axis_results.append(run_axis(axis, documents, chunks, effective))

    weights = effective.get("axis_weights", DEFAULT_CONFIG["axis_weights"])
    weighted_sum = 0.0
    total_weight = 0.0
    for result in axis_results:
        weight = weights.get(result.axis, 1.0 / max(len(selected_axes), 1))
        weighted_sum += result.score * weight
        total_weight += weight
    overall = round(weighted_sum / total_weight, 1) if total_weight > 0 else 0.0
    return AuditResult(
        overall_score=overall,
        overall_grade=grade(overall),
        axis_results=axis_results,
        weights={axis: weights.get(axis, 1.0 / max(len(selected_axes), 1)) for axis in selected_axes},
    )


async def execute_openrag_audit_run(
    *,
    partition: str,
    vectordb: Any,
    indexer: Any,
    run_id: str | None = None,
    config: dict | None = None,
    retention_days: int = 90,
) -> dict[str, Any]:
    effective_config = _openrag_audit_config(config)
    run = {"run_id": run_id} if run_id else await vectordb.create_audit_run.remote(partition, effective_config)
    run_id = run["run_id"]
    partition_id = run.get("partition_id")
    try:
        chunks = await vectordb.list_all_chunk.remote(partition=partition, include_embedding=False)
        files_result = await vectordb.list_partition_files.remote(partition=partition, limit=None)
        file_records = files_result.get("files", []) if files_result else []
        if not chunks and not file_records:
            skipped = await vectordb.update_audit_run.remote(
                run_id,
                status="skipped",
                chunk_count=0,
                document_count=0,
                result_json={"message": "Partition has no files or chunks to audit."},
            )
            return skipped or {"run_id": run_id, "partition": partition, "partition_id": partition_id, "status": "skipped"}
        result = await run_openrag_audit(
            partition=partition,
            openrag_chunks=chunks,
            file_records=file_records,
            indexer=indexer,
            config=effective_config,
        )
        result_dict = sanitize_audit_result(result.to_dict())
        updated = await vectordb.update_audit_run.remote(
            run_id,
            status="completed",
            chunk_count=len(chunks),
            document_count=len(file_records),
            overall_score=result.overall_score,
            overall_grade=result.overall_grade,
            result_json=result_dict,
        )
        await vectordb.cleanup_audit_runs.remote(partition, retention_days)
        return updated or {"run_id": run_id, "partition": partition, "partition_id": partition_id, "status": "completed"}
    except Exception as exc:
        await vectordb.update_audit_run.remote(run_id, status="failed", error=str(exc))
        raise


def _openrag_audit_config(config: dict | None) -> dict:
    effective = merge_config(config)
    effective["governance"] = {
        **effective.get("governance", {}),
        **OPENRAG_GOVERNANCE_CONFIG,
    }
    return effective
