# RAG Audit Integration Document

This document describes what the `rag_audit` package contains, how audit results are stored and exposed, and how to configure or run it.

## Package Files

The package lives at `rag_audit/`.

| File | Purpose |
| --- | --- |
| `rag_audit/__init__.py` | Exposes the package API: `AuditChunk`, `AuditDocument`, `AuditResult`, `AxisResult` dataclasses, plus `run_axis` and `grade` functions. |
| `rag_audit/models.py` | Defines the audit input and output dataclasses: `AuditDocument`, `AuditChunk`, `AxisResult`, and `AuditResult`. |
| `rag_audit/config.py` | Defines axis order, axis labels, default axis weights, default metric settings, and `merge_config()`. |
| `rag_audit/runner.py` | Shared audit helpers. Dynamically loads axis modules (except retrievability) and assigns grade `A` through `E`. Retrievability is handled directly in `openrag_runner.py` because it requires the live `indexer` actor to evaluate queries through OpenRAG's search path. |
| `rag_audit/openrag_adapter.py` | Converts OpenRAG chunks and partition file metadata into `AuditDocument` and `AuditChunk` objects. This is the boundary between OpenRAG storage objects and the audit engine. |
| `rag_audit/openrag_runner.py` | OpenRAG-specific runner. It fetches chunks/files from `vectordb`, runs all audit axes, sanitizes the result, persists the run, marks empty partitions as skipped, and applies retention cleanup. |
| `rag_audit/openrag_job.py` | CLI entrypoint for OpenRAG audit jobs. It connects to Ray, discovers every partition, and runs one all-partitions pass for cron or manual execution. |
| `rag_audit/axes/openrag_retrievability.py` | OpenRAG retrievability axis implementation. It generates queries from document titles, headings, and TF-IDF terms, then evaluates them through OpenRAG's actual `indexer.asearch` path. |
| `rag_audit/sanitize.py` | Redacts sensitive values before persisting or exposing audit results. It removes common secret, token, password, credential, API key, and email patterns. |
| `rag_audit/stopwords.py` | Small English/French stopword list shared by metrics that need lexical filtering. |
| `rag_audit/axes/__init__.py` | Marks the axis implementation package. |
| `rag_audit/axes/utils.py` | Shared axis helpers for score clamping, histograms, document maps, source names, and datetime normalization. |
| `rag_audit/axes/hygiene.py` | Scores corpus hygiene: exact duplicates, near duplicates, boilerplate, language homogeneity, and PII/secret-like findings. |
| `rag_audit/axes/structure.py` | Scores RAG chunk structure: chunk size distribution, token density, readability signals, and adjacent chunk overlap. |
| `rag_audit/axes/coverage.py` | Scores semantic coverage using TF-IDF, SVD/NMF topics, clustering, and outlier detection. |
| `rag_audit/axes/coherence.py` | Scores internal coherence by finding terminology variants, conflicting key-value facts, and entity conflicts. |
| `rag_audit/axes/governance.py` | Scores governance and metadata completeness, freshness, orphan source records, and source-level metadata quality. |

## Runtime Flow

1. The OpenRAG container starts.
2. `entrypoint.sh` exports `PYTHONPATH=/app:/app/openrag`.
3. If `RAG_AUDIT_CRON_ENABLED=true`, the entrypoint registers the audit crontab.
4. Cron runs the configured `RAG_AUDIT_CRON_SCHEDULE`.
5. It runs:

   ```bash
   uv run --no-dev python -m rag_audit.openrag_job
   ```

6. `openrag_job.py` connects to the running Ray runtime used by OpenRAG.
7. It loads OpenRAG config and gets the existing `vectordb` and `indexer` actors.
8. It audits every partition.
9. `openrag_runner.py` reads partition chunks and file metadata through `vectordb`.
10. `openrag_adapter.py` converts OpenRAG records into audit package dataclasses.
11. The six axes run and produce scores, metrics, chart data, and details.
12. Results are sanitized and stored in the relational DB.
13. API endpoints read the persisted results from `vectordb`.

## Result Storage

Audit results are stored in the relational database table:

```text
rag_audit_runs
```

The model is `RagAuditRun` in `openrag/components/indexer/vectordb/models.py`.

Important columns:

| Column | Meaning |
| --- | --- |
| `run_id` | Public UUID for the audit run. |
| `partition_id` | Database ID of the partition that was audited. |
| `partition_name` | Name of the partition that was audited. |
| `status` | `running`, `completed`, `failed`, or `skipped`. |
| `started_at` / `finished_at` | Run timestamps. |
| `document_count` | Number of file records audited. |
| `chunk_count` | Number of chunks audited. |
| `overall_score` | Weighted total score from `0` to `100`. |
| `overall_grade` | Letter grade `A`, `B`, `C`, `D`, or `E`. |
| `config_json` | Effective audit config used for the run. |
| `result_json` | Sanitized full audit result with per-axis metrics, chart data, and details. |
| `error` | Failure message when status is `failed`. |

Old rows are removed by `cleanup_audit_runs()` after a successful run, using `rag_audit.retention_days`.

## API Access

The partition audit endpoints are mounted under:

```text
http://localhost:8080/partition/{partition}
```

If auth is enabled, pass the same bearer token used for the rest of OpenRAG:

```bash
export AUTH_TOKEN=YOUR_AUTH_TOKEN
```

### Compact Summary

Use this for UI list/detail cards or dashboards:

```bash
curl -s \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  "http://localhost:8080/partition/abc/audit/summary" | jq
```

Endpoint:

```http
GET /partition/{partition}/audit/summary
```

Returns:

- `run_id`
- `partition`
- `status`
- `started_at`
- `finished_at`
- `document_count`
- `chunk_count`
- `overall_score`
- `overall_grade`
- `axes[]` with `axis`, `score`, `duration_seconds`, and key metrics

It intentionally omits:

- full `result`
- per-axis `details`
- per-axis `chart_data`

### Full Latest Audit Result

Use this when the UI needs charts, detailed metric payloads, or debug data:

```bash
curl -s \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  "http://localhost:8080/partition/abc/audit" | jq
```

Endpoint:

```http
GET /partition/{partition}/audit
```

### List Audit Runs

```bash
curl -s \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  "http://localhost:8080/partition/abc/audit/runs?limit=20&status=completed" | jq
```

Endpoint:

```http
GET /partition/{partition}/audit/runs?limit=20&status=completed
```

The `status` filter is optional. Valid values are:

- `running`
- `completed`
- `failed`
- `skipped`

### Full Result for a Specific Run

```bash
export RUN_ID=REPLACE_WITH_RUN_ID

curl -s \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  "http://localhost:8080/partition/abc/audit/runs/$RUN_ID" | jq
```

Endpoint:

```http
GET /partition/{partition}/audit/runs/{run_id}
```

## Manual Run Commands

Run audit for all partitions from inside the existing OpenRAG container:

```bash
docker compose exec openrag uv run python -m rag_audit.openrag_job --pretty
```

For production-like Docker Compose usage, run the command inside the existing `openrag` container. That lets the job connect to the same Ray actors and the same mounted data/database services as the API.

## Configuration

### OpenRAG Audit Config

Defaults live in `conf/config.yaml`:

```yaml
rag_audit:
  enabled: true
  retention_days: 90
  retrievability_top_k: 10
  retrievability_max_queries: 500
  max_concurrent_partitions: 1
```

Environment overrides:

| Env var | Config field | Meaning |
| --- | --- | --- |
| `RAG_AUDIT_ENABLED` | `rag_audit.enabled` | Enables or disables audit execution. |
| `RAG_AUDIT_RETENTION_DAYS` | `rag_audit.retention_days` | Number of days to keep historical audit runs per partition. |
| `RAG_AUDIT_RETRIEVABILITY_TOP_K` | `rag_audit.retrievability_top_k` | Search `top_k` used by OpenRAG retrievability evaluation. |
| `RAG_AUDIT_RETRIEVABILITY_MAX_QUERIES` | `rag_audit.retrievability_max_queries` | Maximum generated retrievability queries per partition. |
| `RAG_AUDIT_MAX_CONCURRENT_PARTITIONS` | `rag_audit.max_concurrent_partitions` | Maximum number of partitions audited concurrently. |

When running through OpenRAG's audit job, the package-level `retrievability` defaults (`bm25_top_k`, `queries_per_doc`, `recall_k_values`) are merged with OpenRAG-level overrides: `rag_audit.retrievability_top_k` maps to `top_k`, and `rag_audit.retrievability_max_queries` maps to `max_queries`. OpenRAG-level values take precedence.
### Container Cron

The Docker Compose setup does not create a second audit container. It registers cron inside the existing `openrag` or `openrag-cpu` container.

These variables are set in `docker-compose.yaml`:

```yaml
RAG_AUDIT_CRON_ENABLED: true
RAG_AUDIT_CRON_SCHEDULE: 0 0 * * *
RAG_AUDIT_CRON_TZ: Europe/Paris
```

Meaning:

- `RAG_AUDIT_CRON_ENABLED=true` registers the in-container audit cron job.
- `RAG_AUDIT_CRON_SCHEDULE="0 0 * * *"` runs the all-partitions audit at midnight.
- `RAG_AUDIT_CRON_TZ=Europe/Paris` makes that midnight Paris time, including daylight-saving changes.

Override the schedule:

```bash
RAG_AUDIT_CRON_SCHEDULE="10 2 * * *" RAG_AUDIT_CRON_TZ=Europe/Paris docker compose up openrag
```

Disable scheduled audit:

```bash
RAG_AUDIT_CRON_ENABLED=false docker compose up openrag
```

## Logs

Check whether the startup audit ran:

```bash
docker compose logs --tail=500 openrag
```

Useful log lines:

```text
RAG audit one-off run scheduled in 600s...
Starting one-off RAG audit...
partition: abc
run_id: ...
status: completed
overall: 75.8 (B)
```

If there is no completed result for a partition, the API returns `404`:

```json
{
  "detail": "No completed audit run found for partition 'abc'"
}
```

That means the audit has not run yet, the run failed, the partition is empty and was skipped, or the result has been removed by retention cleanup.
