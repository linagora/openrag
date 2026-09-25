---
title: Grafana service dashboard
description: What the OpenRAG Service dashboard shows, which scrape targets it needs, and how to read it.
---

# Grafana service dashboard

The **OpenRAG Service** dashboard (UID `openrag-service`) answers one question per row: is
OpenRAG indexing, answering and serving what it should? The
[HTTP dashboard](/openrag/documentation/grafana_http_dashboard/) says whether requests
succeed. This one covers what happens behind them: the indexing pipeline, the inference
endpoints, and retrieval's catalog drift.

It ships with the other dashboards in `infra/charts/openrag-stack/dashboards/`. The Compose
monitoring overlay provisions it into the **OpenRAG** folder, and the Helm chart delivers it
as a sidecar ConfigMap (see [Kubernetes monitoring](/openrag/documentation/kubernetes/#monitoring)).

## What it needs scraped

OpenRAG exports metrics from two places, and a panel stays empty when its target is not
scraped:

| Target | Carries |
| --- | --- |
| The API's `GET /metrics` | queue depth, model endpoint readiness, catalog drift, and the API process's inference calls and circuit breakers |
| Ray's metrics agent | document outcomes, stage durations, queue wait, the parse watchdog, and the indexing workers' inference calls and circuit breakers |

Neither target is limited to one kind of inference call. The API embeds each query and
calls the LLM and reranker to answer it. The indexing workers embed, caption images and call
the LLM to contextualize chunks. Under `ENABLE_RAY_SERVE=true` the API is itself a Ray actor,
and its calls move to the Ray target too.

How each deployment collects them:

| Deployment | API `/metrics` | Ray's metrics agent |
| --- | --- | --- |
| Compose, monitoring overlay | job `openrag` | job `openrag-ray`, on the port the overlay pins with `RAY_METRICS_EXPORT_PORT` |
| Kubernetes, `ray.enabled=true` | `openrag.metrics.serviceMonitor` | the Ray PodMonitor, once `ray.metrics.podMonitor.enabled=true` (off by default; see [Monitoring Ray, Postgres and Milvus](/openrag/documentation/kubernetes/#monitoring-ray-postgres-and-milvus)) |
| Kubernetes, `ray.enabled=false` | `openrag.metrics.serviceMonitor` | not collected (see Limitations) |

`OpenRagTargetDown` watches every job whose name contains `openrag`, which is why the Compose
Ray job is named `openrag-ray`.

Ray prefixes everything it exports with `ray_`. Both scrape paths above strip it from
OpenRAG's series, so they are stored under the names the API exports and the alert rules
query. Panels still select `{__name__=~"(ray_)?openrag_…"}`, so a Prometheus that scrapes
Ray without that rename fills them too. Either way, a metric exported from both targets,
such as `openrag_inference_requests_total`, is summed across the two.

See the [metrics reference](/openrag/documentation/metrics_reference/) for every series,
its labels, and the target that exports it.

## Rows

### At a glance

Eight tiles, each mirroring an alert:

| Tile | Turns red when | Alert |
| --- | --- | --- |
| API scrape | Prometheus cannot scrape `/metrics` | `OpenRagTargetDown` |
| Ray scrape | Prometheus cannot scrape Ray's metrics agent | `OpenRagTargetDown` |
| Queued tasks | yellow at 50 queued | `OpenRagBacklogGrowing` |
| Since last parse | neutral: only a problem while tasks are queued | `OpenRagIngestStalled` |
| Failed documents · 5m | 25% of the documents finished in the last 5 minutes failed | `OpenRagIngestFailureRate` |
| Inference errors · 10m | the worst endpoint fails half its calls | `OpenRagInferenceProviderDown` |
| Open breakers | any circuit breaker is open or half-open | `OpenRagCircuitBreakerOpen` |
| Catalog drift · 1h | any retrieval hit dropped for a missing file | `OpenRagCatalogDriftDetected` |

The colours use the alerts' default thresholds, and the two ratio tiles apply the alerts'
volume floors. *Failed documents* needs 5 documents finished in 15 minutes, and *Inference
errors* leaves out endpoints with fewer than 5 calls in 10 minutes that succeeded, failed
or timed out; like the alert, it ignores cancelled, rejected and breaker-refused calls.
Below the floor they read **Low volume**, because the alert does not judge a ratio that
small. If you tune an alert, the tile does not follow it.

Each tile shows the current value. When a series disappears, its tile empties and shows its
no-value text rather than the last value it had. An empty tile is not a healthy one, so that
text is neutral, never green:

- **Unknown** on *Queued tasks* means the API could not read the task state manager. An idle
  queue shows `0`.
- **Not scraped** on *Ray scrape* means no OpenRAG series from Ray reached Prometheus in the
  selected range.
- **Not scraped** on *API scrape* is red: without the API's scrape, most of the dashboard is
  blind.

While the Ray target is down, the tiles it feeds go quiet: *Since last parse* reads **No parse
yet**, and *Failed documents* reads **Idle** once its window has passed. *Ray scrape* is the
tile that says why.

### Ingestion throughput

Documents reaching a terminal state per minute, stacked by outcome; the failure ratio over
the 5-minute window the failure-rate alert reads; and totals over the selected range.
Cancelled uploads are counted but never treated as failures.

### Backlog

Tasks queued and in progress, queue wait (admission to the start of processing, p50 and
p95), and the time since each parser pool last finished a document. A pool nobody uploads
to (audio, say) climbs forever; that is expected. *Clock-skew events* counts queue-wait
measurements that came out negative because the API and worker clocks disagree. When it is
non-zero, the queue-wait panel reads low.

### Pipeline stages

The p95 duration of each stage, on a logarithmic axis because chunking takes milliseconds and
a large PDF parse takes minutes. Next to it, the average number of documents in each stage:
seconds of stage work per second of wall clock. The tallest band is where indexing time
goes. A stage's time is recorded when the stage finishes, so a long stage reads 0 while it
runs, then its whole duration lands in one spike. Read that panel over a range much longer
than your slowest stage.

### Inference

Calls by operation, summed across both targets, the error ratio per registry endpoint, and
failed calls by outcome. Below them: p95 latency per endpoint and operation, circuit-breaker
state (the worst across processes: open, then half-open, then closed), token throughput,
and the readiness probe of every model endpoint in use. `client_override` groups requests that
named their own endpoint through `metadata.llm_override`, so their failures never count
against an endpoint you run.

### Retrieval

Catalog drift over the trailing hour: retrieval hits dropped because their file is gone from
the catalog. A hit is dropped per query, so this is not a document count. Only non-zero
matters; see [catalog reconciliation](/openrag/documentation/catalog_reconciliation/).

## Aggregation choices

- **No per-partition, per-user or per-file breakdown.** No OpenRAG metric carries those
  labels, and a unit test fails the build if a dashboard query references one. Which partition
  is failing is a log question, answered by the `partition` field of the structured logs.
- **Queue depth takes `max`, not `sum`.** Every API replica attached to one Ray cluster reads
  the same task state manager, so summing would multiply the queue by the replica count. The
  same applies to model endpoint readiness (`min`).
- **Rates come before sums.** Each Ray worker is its own series and restarts reset it; summing
  first would read every restart as a drop.

## Limitations

- **Embedded Ray on Kubernetes is not collected.** With `ray.enabled=false` (the chart
  default) Ray runs inside the API pod on a random metrics port, and the chart scrapes nothing
  there. The ingestion rows and the inference calls made while indexing stay empty, and *Ray
  scrape* reads **Not scraped**.
- **Everything Prometheus scrapes is aggregated.** Two OpenRAG releases scraped by one
  Prometheus show as one.
- **Ray Serve.** Under `ENABLE_RAY_SERVE=true` the chart does not scrape the API's `/metrics`
  (see [Prometheus metrics](/openrag/documentation/prometheus_metrics/#limitations)), so every
  panel it feeds stays empty: API scrape, queue depth, readiness and catalog drift.
