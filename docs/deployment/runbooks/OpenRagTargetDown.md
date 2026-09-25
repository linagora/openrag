# OpenRagTargetDown

**Severity:** critical · **Fires after:** 5 min by default

> **The numbers on this page are defaults; your deployment may differ.** Alert
> thresholds and `for` durations are chart values, because they depend on the SLO,
> the corpus size and the query volume of the deployment they run in — here
> `for.OpenRagTargetDown` (default 5m) and `jobMatcher`, the regex this alert
> matches the `job` label on.
> If the behaviour here does not match what you are seeing, read the rule that is
> actually loaded:
>
> ```
> kubectl -n <namespace> get prometheusrule openrag-alerts -o yaml
> ```

```
up{job=~".*openrag.*|ray", job!~".*-(postgresql|milvus)(-.*)?"} == 0
```

With `monitoring.bundled`, the bundled stack's own jobs (`openrag-monitoring-*`,
`openrag-grafana`) are excluded as well.

## What it means

Prometheus cannot scrape one of OpenRag's own targets: the API's `/metrics`, or the Ray
metrics agent that exports the Ray-side series (ingest outcomes, parse completions,
worker-side inference) — the Compose `ray` job, or the chart's
`<namespace>/<release>-raycluster` PodMonitor. The `job` label says which.

The datastore exporters (`<release>-postgresql-metrics`, `<release>-milvus*`) are
deliberately not matched: their being down does not blind any OpenRag alert.

**Every other OpenRag alert is inert while this is firing.** An absent series cannot
breach a threshold, so a dashboard of green panels and a silent alert list mean nothing
until this is resolved. That is the whole reason this rule exists.

## What it is NOT

This is **not** a readiness alert. `up` reports whether `/metrics` answered — and
`/metrics` is designed to keep answering while the service container is degraded, because
that is exactly when the metrics are wanted. A degraded instance is `up == 1` and not
ready at the same time.

A true `OpenRagNotReady` needs a readiness gauge exported from the readiness service; it
does not exist yet. Until it does, check `/ready` by hand:

```bash
curl -s -o /dev/null -w '%{http_code}\n' "$OPENRAG/ready"   # 503 = degraded
curl -s "$OPENRAG/ready" | jq .checks
```

## First checks

```bash
# The API (job openrag-openrag). The chart's fullname is `openrag` by default.
kubectl -n <ns> get pods -l app.kubernetes.io/name=openrag,app.kubernetes.io/instance=openrag
# The RayCluster (job <ns>/openrag-raycluster).
kubectl -n <ns> get pods -l ray.io/cluster=openrag-raycluster
kubectl -n <ns> logs <pod> --tail=100
```

On Compose, `docker compose ps openrag` — the `ray` job scrapes the same container on
port 8091.

## Likely causes, most common first

1. **The pod is down, crash-looping, or failing its startup probe.**
2. **The scrape credential is wrong.** Where `METRICS_TOKEN` is set, a scraper sending the
   wrong bearer gets 403 and the target reads as down.
3. **`ServiceMonitor` selector mismatch.** The operator's `serviceMonitorSelector` did not
   match the labels, so nothing is discovered. Check the operator's targets page.
4. **NetworkPolicy.** Only the ports in `networkPolicy.externalPorts` are reachable from
   outside the namespace; a Prometheus in another namespace scraping a different port is
   blocked.
5. **The whole node or namespace is gone** — in which case other alerts are firing too.

## Verify recovery

The target returns to `up == 1` on the operator's targets page, and the other OpenRag
alerts become meaningful again — check that none of them fire immediately afterwards.
