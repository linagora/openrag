---
title: Catalog and vector reconciliation
---

Postgres and Milvus can disagree after interrupted indexing, deletion or restores. Retrieval checks catalog membership before returning chunks and fails closed if that check fails. Results may contain fewer matches than requested. The counter `openrag_retrieval_orphan_chunks_dropped_total` counts drop occurrences, so repeated retrievals can count the same chunk again. To help identify affected documents, warnings sample up to ten distinct partition/file keys, truncated to 128 characters per value. Warnings are limited to once per minute per process; catalog checks and the counter still run on every call.

Run `uv run python scripts/reconcile_catalog.py --partition NAME` with the deployment's usual configuration. The default is read-only, uses pages of 500 rows and excludes indexing timestamps younger than one hour. Adjust `--page-size` (maximum 1000) and `--grace-seconds` to suit the workload. Choose a grace window longer than normal indexing jobs. Each output line is JSON; findings stream as the scan progresses, followed by a summary. Exit codes are 0 for no findings, 1 for findings (including repaired ones), and 2 for failure. A failed or interrupted run is incomplete; rerun it before drawing conclusions.

To remove aged orphan chunks, first pause new indexing, copy, replace and delete operations and drain in-flight work, then add `--repair`. Keep writers paused until it finishes. Repair rechecks catalog membership and deletes only the reported chunk IDs. The recheck and deletion are not atomic across stores, so the grace window alone cannot make live repair safe. Run the read-only check again afterward. Catalog rows are never removed.

Missing chunks require an operator to decide whether to re-index or remove the catalog entry; empty documents may legitimately have no chunks. Unknown or invalid chunk timestamps are reported without repair. Indexing timestamp mismatches can indicate failed replacement cleanup, but also legacy imports or copies, so they are report-only. Matching timestamps do not prove a complete or unique chunk set: the summary explicitly marks duplicate-set verification as unsupported. Reliable duplicate repair needs a durable indexing-generation identity.

Online reports are observations, not a snapshot across both stores. For a completeness check after a restore or bulk import, finish the writes, wait out the grace window and scan each affected partition, including deleted partition names when looking for their orphan chunks. The `all` wildcard is rejected: each run must name one concrete partition. Grace values must be finite, nonnegative and produce a cutoff within Python's supported datetime range.

New copies use a fresh indexing timestamp shared by both stores, so they receive the same grace period as newly indexed documents. Older copies may still produce report-only timestamp mismatches.
