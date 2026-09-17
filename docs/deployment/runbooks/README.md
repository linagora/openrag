# Alert runbooks

One page per alert, addressed by the `runbook_url` annotation on the rule itself. The
rules are defined once in `infra/charts/openrag-stack/rules/openrag-alerts.yaml` and
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

## Not yet written

Three alerts named in the observability plan have no rule yet, because nothing exports
the signal they need. They are listed here so their absence is visible rather than
assumed:

| Alert | Blocked on |
| --- | --- |
| `OpenRagNotReady` | A readiness gauge exported from the readiness service. `up == 0` is **not** a substitute — see [OpenRagTargetDown](OpenRagTargetDown.md). |
| `OpenRagGpuSaturated` | A GPU exporter on Kubernetes, and a query that spans both metric namespaces. |
| `OpenRagCanaryFailing` | The synthetic canary. A canary nobody alerts on manufactures confidence instead of providing it, so the canary is not finished until this rule exists. |

## Limitation: Ray Serve multi-replica

Every rule reads per-process counters. With `ENABLE_RAY_SERVE=true` and
`num_replicas > 1`, a scrape reaches one replica at random, so rates and gauges are a
random 1/N sample that appears to reset between scrapes — these alerts will both miss real
conditions and fire on phantom ones. Run `num_replicas=1`, or treat them as advisory,
until per-replica scraping exists.

## Adding an alert

1. Add the rule to `infra/charts/openrag-stack/rules/openrag-alerts.yaml`, with
   `severity`, `summary`, `description` and `runbook_url` annotations.
2. Add the page here, named exactly after the alert.
3. `tests/unit/infra/test_alert_rules.py` enforces both, plus that the expression uses
   only known metric names and no caller-controlled label.
