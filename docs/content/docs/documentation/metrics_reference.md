---
title: Metrics reference
description: The Tier-1 OpenRAG metrics, which endpoint exports each one, and how to query them correctly.
---

# Metrics reference

OpenRAG exports its metrics from **two endpoints**, not one. Which endpoint a
metric appears on is decided by the process that produces it, and a deployment
that scrapes only one of them will see a partial picture with no error to
indicate it.

## The two scrape targets

| Target | Produced by | How to scrape |
|---|---|---|
| The API's `/metrics` | The API process | `ServiceMonitor` on the API Service; the `openrag` job on Compose |
| Ray's metrics agent, on every Ray node | Every Ray actor — indexing workers, and the API itself under `ENABLE_RAY_SERVE=true` | `PodMonitor` on the Ray pods; the `openrag-ray` job on Compose |

Indexing runs in Ray actors, which are separate processes and often separate
nodes; a `prometheus_client` counter incremented there is written to a registry
that nothing ever reads. Under Ray Serve the API is itself an actor, and Serve
replicas are not individually addressable over HTTP — a scrape reaches whichever
replica the proxy picks, so per-replica counters in the API process are
unreliable too.

The Helm chart scrapes the Ray target with a `PodMonitor` on port `8090`,
off by default (`ray.metrics.podMonitor.enabled`, for the RayCluster or the Ray embedded in the `openrag` pod; `monitoring.bundled` turns it on; see
[Monitoring Ray, Postgres and Milvus](/openrag/documentation/kubernetes/#monitoring-ray-postgres-and-milvus)).
The Compose monitoring overlay scrapes it as the `openrag-ray` job, on the port
it pins for the embedded Ray with `RAY_METRICS_EXPORT_PORT` (`8091`).

Ray prefixes every metric it exports with `ray_`: on the wire,
`openrag_ingest_documents_total` is `ray_openrag_ingest_documents_total`. The
chart's `PodMonitor` and the Compose `openrag-ray` job rename OpenRAG's series
back, so that both targets store one name and one query covers the API and the
workers. A scrape job written by hand needs the same rule:

```yaml
metric_relabel_configs:   # metricRelabelings on a PodMonitor
  - source_labels: [__name__]
    regex: ray_(openrag_.+)
    target_label: __name__
    replacement: $1
```

Ray's own `ray_*` metrics keep their names. The queries on this page assume the
rename; without it, prefix the Ray-produced names with `ray_`.

## Querying the Ray-exported metrics

Ray attaches `WorkerId`, `SessionName`, `NodeAddress`, `Component` and
`Version` to everything it exports. Two of those churn: `WorkerId` changes with
every actor process, and `SessionName` embeds a timestamp, so it changes on
every cluster restart.

**Always aggregate them away, and always rate before summing:**

```promql
sum without(WorkerId, SessionName, NodeAddress, Component, Version) (
  rate(openrag_ingest_documents_total[5m])
)
```

`rate()` detects a counter reset only *within* a series. Because each worker is
its own series and workers die, `rate(sum(...))` silently loses every reset —
`sum(rate(...))` is the only correct order. Encode this once in recording rules
rather than in every dashboard panel.

Rename only the metric name (above); **never drop `WorkerId` with
`metric_relabel_configs`.** It is what keeps
concurrent workers' series distinct; collapsing them puts duplicate samples in a
single scrape, which Prometheus rejects wholesale. Losing the entire scrape is a
far worse outcome than the cardinality it would save.

That cardinality is bounded, not unbounded: Ray's metrics agent drops a dead
worker's series after `RAY_WORKER_TIMEOUT_S` (default 120 s). The worst case is
restart-rate × timeout-window, which at ten indexer actors is roughly 2 400
series in steady state.

## Tier 1

| Metric | Type | Labels | Target | Answers |
|---|---|---|---|---|
| `openrag_ingest_documents_total` | counter | `status` | Ray | Indexing throughput and failure rate |
| `openrag_ingest_stage_duration_seconds` | histogram | `stage` | Ray | Where indexing time goes |
| `openrag_ingest_tasks` | gauge | `state` | `/metrics` | Queue depth / backlog |
| `openrag_inference_requests_total` | counter | `provider`, `operation`, `outcome` | both | Are the endpoints healthy |
| `openrag_inference_duration_seconds` | histogram | `provider`, `operation` | both | Are they slow |
| `openrag_llm_tokens_total` | counter | `operation`, `kind` | both | Aggregate token burn |

Supporting metrics:

| Metric | Type | Labels | Target | Answers |
|---|---|---|---|---|
| `openrag_ingest_queue_wait_seconds` | histogram | — | Ray | Admission-to-processing latency |
| `openrag_ingest_last_parse_completion_timestamp_seconds` | gauge | `pool` | Ray | Progress watchdog — is a parser pool wedged. Last completed parse, or the pool's first use in the process |
| `openrag_ingest_clock_skew_events_total` | counter | — | Ray | Queue-wait measurements that came out negative |
| `openrag_circuit_breaker_state` | gauge | `name` | both | 0 closed, 1 open, 2 half-open, -1 unknown |

`both` means each process exports its own calls, under the same name once the
Ray series are renamed. Neither side is limited to one kind of call: the API
process embeds every query and calls the LLM and reranker to answer it, while
the indexing workers embed, caption, contextualize and topic-tag documents with
the same clients. Every breaker name can therefore appear on either target, so
aggregate across both — `max by (name) (openrag_circuit_breaker_state)`,
`sum by (provider) (rate(openrag_inference_requests_total[5m]))`. Under
`ENABLE_RAY_SERVE=true` the API is itself a Ray actor, and all of these move to
the Ray target.

Label values:

- `status` — `completed`, `failed`, `cancelled`
- `stage` — `parse`, `caption`, `chunk`, `contextualize`, `topic_tag`, `embed`, `store`
- `state` — `QUEUED`, `SERIALIZING`
- `operation` — `embed`, `chat`, `completion` (text completions), `rerank`, `vlm`
- `outcome` — `success`, `error`, `timeout`, `circuit_open`, `cancelled` (the caller gave
  up: a closed stream, its own deadline, or siblings cancelled after one failed; not a
  provider failure, so keep it out of error ratios), `rejected` (a 4xx the request caused,
  such as an unknown model or an over-long prompt; 408 and 429 stay `error`, and so
  does 401 except on LLM calls, whose request the caller shapes)
- `kind` — `prompt`, `completion`
- `pool` — `marker`, `docling`, `pymupdf`, `pdf_client`, `local_whisper`, `audio_client` for PDF and audio; any other format is labelled by its document type (`text`, `docx`, `eml`, `image`, ...)
- `name` — `llm`, `embedder`, `vlm`, `reranker`

## No metric carries `partition`

Partitions are created on write by callers, so the label is bounded by user
behaviour rather than by anything the deployment controls. At 10 000 partitions
`openrag_ingest_stage_duration_seconds{partition,stage}` would be ~700 000
series, and in the integrated deployment that lands on the collaborative
platform's shared Prometheus.

The same reasoning excludes `user_id`, `file_id`, `task_id`, `request_id` and
`filename`. A unit test fails the build if any metric declares one of them.

Per-tenant questions are answered elsewhere by design:

| Question | Where |
|---|---|
| Is ingestion failing? | Prometheus, aggregate, by `status` |
| Which partition is failing? | Logs — `partition` is a structured log field |
| How much has this tenant consumed? | Postgres — usage accounting |

If one tenant ever needs its own alerting, the pattern is an allowlist: label
the few partitions that matter and bucket the rest as `other`, so the series
count is fixed at allowlist size + 1 however many partitions exist.

## Two metrics that need care

**The parse watchdog exports a timestamp, not an age.** Compute the age at
evaluation time, and only while work is waiting:

```promql
(max(openrag_ingest_tasks{state="QUEUED"}) > 0)
and on()
(time() - max(openrag_ingest_last_parse_completion_timestamp_seconds) > 720)
```

The stamp is the last completed parse, or the pool's first use in the worker
process: a pool that fails from its very first parse would otherwise never
produce the series, and a stall starting at boot would have nothing to age. A
worker that restarts more often than the threshold re-seeds each time and so
never ages past it; for a crash-looping worker, pod restarts are the signal.

A "seconds since" gauge would have to be rewritten continuously to stay
truthful, and would freeze at its last value exactly when a pool wedges — the
condition it exists to detect. A timestamp climbs on its own — which is also
why the age alone is not an alert:

- After a pool's first use it is stamped only when a parse succeeds, so an idle
  system ages exactly like a wedged one. Gate it on queued work.
- Take `max()` across pools and nodes, not the age per pool: rarely used
  formats go hours without a parse, and a node that simply got no work is not
  stalled while another makes progress.
- It is absent until some pool is first used, and again after every worker
  restart until a pool is used again (Ray drops a dead worker's series after
  `RAY_WORKER_TIMEOUT_S`), so it cannot fire then. Pair it with an alert on a
  growing backlog.
- Set the threshold above your slowest normal parse; large scanned PDFs take
  minutes.

**`openrag_ingest_tasks` is the same number on every API replica.** Each
replica samples the cluster-wide backlog, so aggregate with `max`, never `sum`
— `sum` multiplies the backlog by the replica count:

```promql
max by (state) (openrag_ingest_tasks)
```

**`provider` is the configured endpoint name, not the model.** A request that
overrides the endpoint through `metadata.llm_override` is counted under the
fixed value `client_override`, so a third-party endpoint's failures never
corrupt the error rate of an endpoint the operator runs.

## `openrag_ingest_tasks` may be absent

It is sampled during the scrape from the durable `jobs` table, reconciled with the
live TaskStateManager one task at a time, so tasks still queued when the actor
restarted keep counting. If the durable rows cannot be read — Postgres is down,
or the deployment has no job store — it falls back to the actor alone. If the
actor is unreachable, or the API booted degraded, the gauge is withdrawn and the rest of
`/metrics` is served normally. Absence means "could not be read", never "the
queue is empty" — an idle queue publishes an explicit `0`.
