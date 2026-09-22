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
| The API's `/metrics` | The API process | `ServiceMonitor` on the API Service |
| Ray's metrics agent, port `8090` | Every Ray actor — indexing workers, and the API itself under `ENABLE_RAY_SERVE=true` | `PodMonitor` on the Ray pods |

Indexing runs in Ray actors, which are separate processes and often separate
nodes; a `prometheus_client` counter incremented there is written to a registry
that nothing ever reads. Under Ray Serve the API is itself an actor, and Serve
replicas are not individually addressable over HTTP — a scrape reaches whichever
replica the proxy picks, so per-replica counters in the API process are
unreliable too.

Neither the Helm chart nor the compose monitoring overlay scrapes the Ray
target yet: the chart opens port `8090` on the Ray pods but ships no
`PodMonitor`, and the overlay's Prometheus has no job for it. Until that is
added, the Ray-exported series below exist but are not collected.

Ray prefixes every metric it exports with `ray_`. A metric named
`openrag_ingest_documents_total` is therefore queried as
`ray_openrag_ingest_documents_total`.

## Querying the Ray-exported metrics

Ray attaches `WorkerId`, `SessionName`, `NodeAddress`, `Component` and
`Version` to everything it exports. Two of those churn: `WorkerId` changes with
every actor process, and `SessionName` embeds a timestamp, so it changes on
every cluster restart.

**Always aggregate them away, and always rate before summing:**

```promql
sum without(WorkerId, SessionName, NodeAddress, Component, Version) (
  rate(ray_openrag_ingest_documents_total[5m])
)
```

`rate()` detects a counter reset only *within* a series. Because each worker is
its own series and workers die, `rate(sum(...))` silently loses every reset —
`sum(rate(...))` is the only correct order. Encode this once in recording rules
rather than in every dashboard panel.

**Never drop `WorkerId` with `metric_relabel_configs`.** It is what keeps
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
| `openrag_ingest_last_parse_completion_timestamp_seconds` | gauge | `pool` | Ray | Progress watchdog — is a parser pool wedged |
| `openrag_ingest_clock_skew_events_total` | counter | — | Ray | Queue-wait measurements that came out negative |
| `openrag_circuit_breaker_state` | gauge | `name` | both | 0 closed, 1 open, 2 half-open, -1 unknown |

Label values:

- `status` — `completed`, `failed`, `cancelled`
- `stage` — `parse`, `caption`, `chunk`, `contextualize`, `topic_tag`, `embed`, `store`
- `state` — `QUEUED`, `SERIALIZING`
- `operation` — `embed`, `chat`, `rerank`, `vlm`
- `outcome` — `success`, `error`, `timeout`, `circuit_open`
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
evaluation time:

```promql
time() - max without(WorkerId, SessionName, NodeAddress, Component, Version) (
  ray_openrag_ingest_last_parse_completion_timestamp_seconds
) > 300
```

A "seconds since" gauge would have to be rewritten continuously to stay
truthful, and would freeze at its last value exactly when a pool wedges — the
condition it exists to detect. A timestamp climbs on its own.

**`provider` is the configured endpoint name, not the model.** A request that
overrides the endpoint through `metadata.llm_override` is counted under the
fixed value `client_override`, so a third-party endpoint's failures never
corrupt the error rate of an endpoint the operator runs.

## `openrag_ingest_tasks` may be absent

It is sampled during the scrape by asking the TaskStateManager. If that actor is
unreachable, or the API booted degraded, the gauge is withdrawn and the rest of
`/metrics` is served normally. Absence means "could not be read", never "the
queue is empty" — an idle queue publishes an explicit `0`.
