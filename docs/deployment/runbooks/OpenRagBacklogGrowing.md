# OpenRagBacklogGrowing

**Severity:** warning · **Fires after:** 15 min

```
deriv(openrag_ingest_tasks{state="QUEUED"}[30m]) > 0
and openrag_ingest_tasks{state="QUEUED"} > 50
```

## What it means

The queue has trended upward for half an hour **and** is over 50 tasks. Both conditions
matter: a burst upload rises steeply and drains fine, and a steady small queue is a
healthy pipeline. This is arrival rate exceeding capacity.

**This is capacity, not a fault.** If nothing is completing at all, that is
`OpenRagIngestStalled` — check whether it is also firing before treating this as scale.

## First checks

```bash
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" "$OPENRAG/queue/info"
```

- `workers.total_slots`, `workers.pool_size`, `workers.max_per_actor` — the ceiling.
- `openrag_ingest_queue_wait_seconds` — how long admission to start now takes.
- `openrag_ingest_stage_duration_seconds` by `stage` — which stage is the bottleneck.

## Likely causes

1. **A legitimate bulk import.** The production target is ~2,000-document batches. Expect
   this alert during one; confirm the queue drains afterwards rather than plateauing.
2. **Parse is the bottleneck and the GPU is saturated.** Marker is the default PDF
   parser and GPU-bound. Check `OpenRagGpuSaturated` once S3-8 exists; until then, look
   at the stage-duration histogram for `parse`.
3. **Pool sized below the workload.** Slots are fixed at pool creation.
4. **Throughput lost to restarts.** Workers that OOM and restart lose their in-flight
   documents, so effective throughput drops without any failure being recorded.

## Actions

Scale the parser pool or add GPU capacity. On Kubernetes this is horizontal scale of the
parser workers; on a single node it is bounded by the one GPU, and the honest answer may
be that the batch will take as long as it takes.

## Verify recovery

`deriv(...)` goes negative — the queue is draining. The absolute depth may stay high for
a while; that is fine as long as the trend has turned.
