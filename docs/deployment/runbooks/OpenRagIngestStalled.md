# OpenRagIngestStalled

**Severity:** critical · **Fires after:** ~14 min by default (12 min idle + `for: 2m`)

> **The numbers on this page are defaults; your deployment may differ.** Alert
> thresholds and `for` durations are chart values, because they depend on the SLO,
> the corpus size and the query volume of the deployment they run in — here `thresholds.ingestIdleSeconds` (default 720) and `for.OpenRagIngestStalled` (default 2m).
> If the behaviour here does not match what you are seeing, read the rule that is
> actually loaded:
>
> ```
> kubectl -n <namespace> get prometheusrule openrag-alerts -o yaml
> ```

```
max by (state) (openrag_ingest_tasks{state="QUEUED"}) > 0
and on()
max(min_over_time(openrag_ingest_tasks{state="QUEUED"}[720s])) > 0
and on()
(time() - max(openrag_ingest_last_parse_completion_timestamp_seconds) > 720)
```

## What it means

Documents have been queued for 12 minutes and **no parser pool has completed a parse in that time**.
Uploads are still being accepted and acknowledged; none of them are being indexed. This
is user-visible as "I uploaded it and it never appeared in search".

Distinguish from `OpenRagBacklogGrowing`, which means the pipeline *is* working but too
slowly. This alert means it is not working at all.

The precise latency depends on `evaluation_interval`: the condition can only be seen
on the first evaluation past 12 minutes, so the shipped Compose config (15s) fires at
~14m15s, while a 1-minute interval fires at 15m.

## First checks

```bash
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" "$OPENRAG/queue/info"
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" "$OPENRAG/queue/tasks?task_status=SERIALIZING"
```

- `workers.total_slots` vs `tasks.active` — are all slots occupied by tasks that never finish?
- Pick a stuck task and read `details.failed_stage`, then `GET /indexer/task/{id}/error`.
- Ray dashboard (`:8265`, loopback-bound) → Actors: are the `Indexer` actors alive?

## Likely causes, most common first

1. **A rolling deploy left replicas attached to the previous generation's actors.** The
   worker actors are named, detached and `get_if_exists`, so a changed remote contract
   without a `_INDEXER_ACTOR_PROTOCOL_VERSION` bump (`services/workers/indexer_pool.py`)
   makes every submit raise `TypeError`. Tasks enter `QUEUED` and never leave.
   Retire the old generation with `services/workers/retire_indexer_generation.py`.
2. **A parser worker is wedged holding its slot.** A Marker child that hangs without
   exiting keeps its pool slot; enough of those and the pool has no free slot to schedule
   into. Check for parse stages with no progress and restart the pool.
3. **A worker was OOM-killed mid-batch.** `max_restarts=5` brings it back, but every
   document that worker was holding goes with it. Look for restart counts in the Ray
   dashboard and memory pressure on the node.
4. **The GPU is gone** — driver reset, node eviction, or the inference endpoint the
   parse stage depends on is unreachable. Check `OpenRagInferenceProviderDown`.

## Verify recovery

`openrag_ingest_last_parse_completion_timestamp_seconds` starts advancing again and the
`QUEUED` gauge drains. The alert resolves on its own once a parse completes.

## Tuning: the idle window must exceed your longest normal parse

The rule sees parse *completions*, not parses in progress. One document that
legitimately takes longer than the idle window — a long scanned PDF on a single GPU
worker, where Marker's own timeout is an hour — with anything queued behind it reads
exactly like a wedged pool, and pages at the default 12 minutes while the parse is
healthy. It clears as soon as that parse lands.

Set `monitoring.prometheusRule.thresholds.ingestIdleSeconds` above the longest parse
that is normal on the deployment. On a single-GPU Marker deployment that ingests long
scans, that is well above 12 minutes. If this fired and the Ray dashboard shows a parse
still making progress, this is the cause: raise the threshold rather than restarting
anything.

## Known blind spot

The rule reads `openrag_ingest_last_parse_completion_timestamp_seconds`, and while that
gauge is absent the alert cannot fire. It is absent more often than "on a new
instance": it is exported per worker process (Ray's `WorkerId` label), and Ray drops a
dead worker's series about two minutes after the process exits. So the alert is blind
whenever **no pool has completed a parse since the workers last started** — once pools
seed the gauge on their first use (#1056), whenever no pool has completed *or started*
one. That covers a brand-new instance wedged from its first upload, and equally a pool
that wedges right after a worker restart or a redeploy: the old timestamps disappear
and nothing replaces them.

There is deliberately no `absent()` branch to close it: with the chart's default
embedded Ray the gauge is never scraped at all, and such a branch would page on every
long batch. `OpenRagBacklogGrowing` covers a queue that rises from zero; after a
restart with work queued, check the Ray dashboard rather than waiting for this alert.
