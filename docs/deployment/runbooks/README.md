# Alert runbooks

One page per alert, addressed by the `runbook_url` annotation on the rule itself. The
rules are defined once in `infra/charts/openrag-stack/rules/openrag-alerts.yaml.tpl` and
delivered two ways — wrapped in a `PrometheusRule` by the chart, loaded via `rule_files`
by the Compose monitoring overlay.

Every shipped alert has a page here. An alert whose `runbook_url` 404s is worse than one
with no annotation: it promises help at three in the morning and does not deliver it.

## Alerts

| Alert | Severity | Means |
| --- | --- | --- |
| [OpenRagIngestStalled](OpenRagIngestStalled.md) | critical | Documents queued, nothing completing |
| [OpenRagIngestFailureRate](OpenRagIngestFailureRate.md) | warning | Over 25% of documents failing to index |
| [OpenRagBacklogGrowing](OpenRagBacklogGrowing.md) | warning | Queue rising faster than it drains |
| [OpenRagInferenceProviderDown](OpenRagInferenceProviderDown.md) | critical | Over half the calls to one registry endpoint are failing |
| [OpenRagCircuitBreakerOpen](OpenRagCircuitBreakerOpen.md) | critical | OpenRag has stopped calling an endpoint after repeated failures |
| [OpenRagCatalogDriftDetected](OpenRagCatalogDriftDetected.md) | critical | Vector store and catalog disagree |
| [OpenRagTargetDown](OpenRagTargetDown.md) | critical | Prometheus cannot scrape — **every other alert is inert** |

`OpenRagInferenceProviderDown` and `OpenRagCircuitBreakerOpen` usually fire together —
the breaker opens *because* calls were failing. They are separate alerts so routing can
inhibit one while the other is firing; a unioned alert cannot be half-inhibited.

Start with `OpenRagTargetDown` whenever several alerts look wrong at once, and with
`OpenRagIngestStalled` before `OpenRagBacklogGrowing`: "nothing is completing" and
"completing too slowly" have different fixes and the first masks the second.

## Tuning before go-live

The thresholds are chart values (`monitoring.prometheusRule.thresholds`); the defaults
were measured on synthetic traffic. One must be checked against the deployment before
alerts are routed to anyone: **`ingestIdleSeconds` (default 720) must exceed the longest
normal parse.** `OpenRagIngestStalled` sees parse completions only, so a single parse
longer than the window — a long scanned PDF on a single-GPU Marker worker, whose timeout
is 3600s — pages while it runs, whenever anything is queued behind it. On single-GPU
Marker deployments set it above the expected longest parse. See
[OpenRagIngestStalled](OpenRagIngestStalled.md#tuning-the-idle-window-must-exceed-your-longest-normal-parse).

## Not yet written

Three alerts named in the observability plan have no rule yet, because nothing exports
the signal they need. They are listed here so their absence is visible rather than
assumed:

| Alert | Blocked on |
| --- | --- |
| `OpenRagNotReady` | A readiness gauge exported from the readiness service. `up == 0` is **not** a substitute — see [OpenRagTargetDown](OpenRagTargetDown.md). |
| `OpenRagGpuSaturated` | A GPU exporter on Kubernetes, and a query that spans both metric namespaces. |
| `OpenRagCanaryFailing` | The synthetic canary. A canary nobody alerts on manufactures confidence instead of providing it, so the canary is not finished until this rule exists. |

## Topologies where alerts cannot fire

The rules read two scrape targets, and each series lives on exactly one of them:

- **API `/metrics`** (`prometheus_client`): `openrag_ingest_tasks`, the HTTP metrics,
  `openrag_retrieval_orphan_chunks_dropped_total`, and — on the plain uvicorn API only —
  the API's own inference calls and breaker states.
- **Ray's metrics agent** (`ray.util.metrics`, `core/observability/ray_metrics.py`):
  `openrag_ingest_documents_total`, `openrag_ingest_last_parse_completion_timestamp_seconds`,
  and every inference call and breaker state recorded inside a Ray actor — the indexing
  workers, and under Ray Serve the API replicas too (`core/observability/inference_metrics.py`).

A series nobody scrapes is absent, and an absent series never breaches a threshold, so
the alerts that depend on it stay silent **without anything saying so** —
`OpenRagTargetDown` only covers targets that are configured and failing, not targets
that were never configured.

| Topology | Not scraped | Alerts that cannot fire |
| --- | --- | --- |
| **Helm, `ray.enabled=false`** (default: Ray embedded in the API pod) | The Ray agent, unless `ray.metrics.podMonitor.enabled=true` or `monitoring.bundled` (both default `false`). The chart pins the embedded agent to port 8090 (`RAY_METRICS_EXPORT_PORT`), and the `PodMonitor` then selects the API pod. With `RAY_ADDRESS` set, the API attaches to an external cluster instead, and that cluster must be scraped where it runs. | Without the PodMonitor: `OpenRagIngestStalled`, `OpenRagIngestFailureRate`. `OpenRagInferenceProviderDown` and `OpenRagCircuitBreakerOpen` see only the API's calls (chat, query embedding), never indexing's. |
| **Helm, `ray.enabled=true`, uvicorn API** | The Ray agent, unless `ray.metrics.podMonitor.enabled=true` or `monitoring.bundled`. | Without the PodMonitor, as the row above. |
| **Helm, Ray Serve** (`ray.enabled=true` + `ENABLE_RAY_SERVE=true`, e.g. `values-linagora.yaml`) | The API's `/metrics`: the chart renders no API `ServiceMonitor` under Ray Serve (`monitoring.bundled` skips it; enabling it explicitly fails the render), since each replica keeps its own registry behind one proxy. The Ray agent too, unless `ray.metrics.podMonitor.enabled=true` or `monitoring.bundled`. | `OpenRagBacklogGrowing` and `OpenRagIngestStalled` (both read `openrag_ingest_tasks`), `OpenRagCatalogDriftDetected`, and `OpenRagTargetDown` for the API (there is no API target to be down). Without the PodMonitor, also `OpenRagIngestFailureRate`, `OpenRagInferenceProviderDown` and `OpenRagCircuitBreakerOpen` — nothing is scraped at all. |
| **Compose** | The Ray agent, unless the monitoring overlay (`monitoring.docker-compose.yaml`) is used: it pins `RAY_METRICS_EXPORT_PORT` so the `ray` scrape job can reach it. Ray otherwise picks a random port. | Without the overlay's pinned port, as the first row. |

Even where the Ray agent is scraped, `OpenRagIngestStalled` has one more blind window.
Its gauge is per worker process and Ray drops a dead worker's series about two minutes
after it exits, so the alert cannot fire while **no pool has completed or started a parse
since the workers last started** (a pool seeds the gauge on its first use) — a new instance, and equally any worker restart or redeploy. See
[OpenRagIngestStalled](OpenRagIngestStalled.md#known-blind-spot).

Wherever a Ray-side series *is* scraped from more than one process, it is summed across
them, which is correct for counters. Scraping a Ray Serve API through its proxy would not
be: a scrape reaches one replica at random, so rates and gauges become a random 1/N
sample that appears to reset between scrapes. That is why the chart refuses to render
that target rather than render a misleading one.

## Adding an alert

1. Add the rule to `infra/charts/openrag-stack/rules/openrag-alerts.yaml.tpl`, with
   `severity`, `summary`, `description` and `runbook_url` annotations, and
   `for: {{ $for.<AlertName> }}`.
2. Register the alert's default `for` in the template's `$for` dict, near the top of the
   same file. Without the key the rule renders a bare `for:` — null, so the alert fires
   on a single evaluation — and `monitoring.prometheusRule.for.<AlertName>` is refused
   as "not an alert in this chart".
3. Add the alert and its default to the `for` list in the comment above
   `monitoring.prometheusRule.for` in `infra/charts/openrag-stack/values.yaml`.
4. Regenerate the Compose copy with `uv run python scripts/gen_alert_rules.py` (needs
   `helm`). `infra/compose/prometheus/rules/openrag-alerts.yaml` is its output and must
   never be edited by hand; CI runs `gen_alert_rules.py --check` and fails on any drift.
5. Add the page here, named exactly after the alert.
6. Add its scenarios to `tests/unit/infra/alert_rules.promtool.yaml`, which replays
   synthetic series through the generated rules.
7. `tests/unit/infra/test_alert_rules.py` enforces the annotations and the page, that
   every rule the chart renders waits a non-empty `for`, that the `values.yaml` list
   matches the template's defaults, plus that the expression uses only known metric
   names and no caller-controlled label.
