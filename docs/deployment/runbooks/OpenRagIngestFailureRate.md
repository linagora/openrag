# OpenRagIngestFailureRate

**Severity:** warning · **Fires after:** 10 min above threshold

```
sum(rate(openrag_ingest_documents_total{status="failed"}[15m]))
/
sum(rate(openrag_ingest_documents_total{status=~"completed|failed"}[15m])) > 0.25
and sum(increase(openrag_ingest_documents_total{status=~"completed|failed"}[15m])) >= 5
```

## What it means

More than a quarter of documents reaching a terminal state are failing. `cancelled` is
excluded from both sides — a user cancelling an upload is not a failure — and the volume
floor of 5 documents stops a quiet instance paging on one bad file.

## First checks

```bash
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" "$OPENRAG/queue/tasks?task_status=FAILED"
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" "$OPENRAG/indexer/task/<task_id>/error"
```

**The question that splits the diagnosis: is one stage failing, or many?** Each failed
task carries `details.failed_stage`. Stages are `parse`, `caption`, `chunk`,
`contextualize`, `topic_tag`, `embed`, `store`.

- **One stage dominates → systemic.** An endpoint, a dependency, a config change.
- **Failures spread across stages → the input.** A batch of corrupt or unusual documents.

## Likely causes

| `failed_stage` | Look at |
| --- | --- |
| `parse` | Malformed or password-protected files; a parser backend crash-looping |
| `caption`, `contextualize`, `topic_tag` | The VLM / LLM endpoint — check `OpenRagInferenceProviderDown` |
| `embed` | The embedder endpoint, or a dimension mismatch after a model change |
| `store` | Milvus — collection missing, schema mismatch, disk pressure |

## Which partition?

Deliberately not in the metric — `partition` is caller-created and unbounded, so it is a
**log** field, not a label. With `LOG_FORMAT=json`, filter on `partition` and `file_id`
in the collector.

## Verify recovery

The ratio falls below 25% and the alert resolves. Re-index the failed files once the
cause is fixed; a failed task is not retried automatically.
