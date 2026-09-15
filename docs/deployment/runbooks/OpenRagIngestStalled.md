# OpenRagIngestStalled

**Severity:** critical · **Fires after:** ~20 min (15 min idle + `for: 5m`)

```
(openrag_ingest_tasks{state="QUEUED"} > 0)
and on()
(time() - max(openrag_ingest_last_parse_completion_timestamp_seconds) > 900)
```

## What it means

Documents are queued and **no parser pool has completed a parse for 15 minutes**.
Uploads are still being accepted and acknowledged; none of them are being indexed. This
is user-visible as "I uploaded it and it never appeared in search".

Distinguish from `OpenRagBacklogGrowing`, which means the pipeline *is* working but too
slowly. This alert means it is not working at all.

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

## Known blind spot

If **no** pool has ever completed a parse, the gauge is absent and this alert cannot
fire — a brand-new instance that is wedged from the very first upload stays silent here.
`OpenRagBacklogGrowing` covers a queue that rises from zero.
