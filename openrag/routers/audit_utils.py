def summarize_audit_run(run: dict) -> dict:
    result = run.get("result") or {}
    axes = []
    for axis_result in result.get("axis_results", []):
        metrics = axis_result.get("metrics") or {}
        axes.append(
            {
                "axis": axis_result.get("axis"),
                "score": axis_result.get("score"),
                "duration_seconds": axis_result.get("duration_seconds"),
                "metrics": {
                    "sub_scores": metrics.get("sub_scores", {}),
                    "total_docs": metrics.get("total_docs"),
                    "total_chunks": metrics.get("total_chunks"),
                    "total_queries": metrics.get("total_queries"),
                },
            }
        )
    return {
        "run_id": run.get("run_id"),
        "partition": run.get("partition"),
        "partition_name": run.get("partition_name") or run.get("partition"),
        "partition_id": run.get("partition_id"),
        "status": run.get("status"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "document_count": run.get("document_count"),
        "chunk_count": run.get("chunk_count"),
        "overall_score": run.get("overall_score"),
        "overall_grade": run.get("overall_grade"),
        "axes": axes,
    }
