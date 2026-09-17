# OpenRagBacklogGrowing

**Severity:** warning · **Fires after:** 25 min of continuous growth by default

> **The numbers on this page are defaults; your deployment may differ.** Alert
> thresholds and `for` durations are chart values, because they depend on the SLO,
> the corpus size and the query volume of the deployment they run in — here `thresholds.backlogDepth` (default 50) and `for.OpenRagBacklogGrowing` (default 25m).
> If the behaviour here does not match what you are seeing, read the rule that is
> actually loaded:
>
> ```
> kubectl -n <namespace> get prometheusrule <release>-alerts -o yaml
> ```

```
openrag_ingest_tasks{state="QUEUED"} > 50
and deriv(openrag_ingest_tasks{state="QUEUED"}[5m]) > 0
```

## What it means

The queue has trended upward for half an hour **and** is over the depth floor, 50
tasks by default. Both conditions
matter: a burst upload rises steeply and drains fine, and a steady small queue is a
healthy pipeline. This is arrival rate exceeding capacity.

**This is capacity, not a fault.** If nothing is completing at all, that is
`OpenRagIngestStalled` — check whether it is also firing before treating this as scale.

## It will fire on a large enough bulk import, and that is not fixable here

`for: 25m` outlasts the arrival phase of a typical batch, so an import that lands and
drains does not alert. **An import whose queue climbs for more than 25 minutes will.**

No threshold on these two series can prevent that: during a 2,000-document upload the
queue genuinely is growing and capacity genuinely is below arrival rate, so "a batch just
landed" and "we are underwater" are literally the same shape. The signal that separates
them is the **age of the oldest pending item** — a healthy batch consumes promptly from a
deep queue, a collapsed one lets the oldest item age without bound — and the in-process
queue does not expose it.

If you know a bulk import is running, silence this for the duration rather than widening
the threshold.

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

`deriv(...)` goes negative within about five minutes of the queue turning — it is a
short window on purpose, so it tracks the turn rather than lagging behind it. The absolute depth may stay high for
a while; that is fine as long as the trend has turned.
