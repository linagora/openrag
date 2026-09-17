# OpenRagIngestFailureRate

**Severity:** warning · **Fires after:** ~8 min from the onset of failures, with default thresholds

> **The numbers on this page are defaults; your deployment may differ.** Alert
> thresholds and `for` durations are chart values, because they depend on the SLO,
> the corpus size and the query volume of the deployment they run in — here `thresholds.ingestFailureRatio` (default 0.25), `thresholds.ingestVolumeFloor` (default 5) and `for.OpenRagIngestFailureRate` (default 5m).
> If the behaviour here does not match what you are seeing, read the rule that is
> actually loaded:
>
> ```
> kubectl -n <namespace> get prometheusrule <release>-alerts -o yaml
> ```

```
sum(rate(openrag_ingest_documents_total{status="failed"}[5m]))
/
sum(rate(openrag_ingest_documents_total{status=~"completed|failed"}[5m])) > 0.25
and sum(increase(openrag_ingest_documents_total{status=~"completed|failed"}[15m])) >= 5
```

## What it means

More than a quarter of documents reaching a terminal state are failing — a quarter
being the default threshold. `cancelled` is
excluded from both sides — a user cancelling an upload is not a failure — and the volume
floor (5 documents by default) stops a quiet instance paging on one bad file.

**The two windows are different on purpose.** The ratio reads the last **5 minutes**, so
a real failure surfaces in about 8 minutes. The volume floor reads the last **15
minutes**, because it is answering "did enough work happen to judge at all?" — an
instance finishing one document every few minutes never reaches five inside a 5-minute
window, and with both windows short it is never detected. `for: 5m` is what rejects a
brief blip, so the short ratio window costs no stability.

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
