---
title: Prometheus metrics
description: Expose OpenRAG metrics to an external Prometheus and Grafana.
---

# Prometheus metrics

OpenRAG exposes Prometheus metrics on `GET /metrics`, on the same port as the
API. Prometheus pulls them on a schedule; OpenRAG never pushes anything. This
page covers the exposed series, how to protect the endpoint, and how to scrape
it from a Prometheus that lives outside the OpenRAG stack, on a VM or in
Kubernetes.

The bundled monitoring overlay (`infra/compose/monitoring.docker-compose.yaml`)
is a self-contained Prometheus + Grafana for single-host deployments and is
described in the [Docker installation guide](/openrag/installation/docker/).
Everything below applies to both.

## Exposed series

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `openrag_http_requests_total` | counter | `method`, `endpoint`, `status_code` | Requests served, per route template. |
| `openrag_http_request_failures_total` | counter | `method`, `endpoint`, `status_code` | Subset with a status of 400 or above. |
| `openrag_http_request_duration_seconds` | histogram | `method`, `endpoint` | Full request duration, including the streamed body for chat completions. |
| `openrag_circuit_breaker_state` | gauge | `name` | Inference circuit breaker: 0 closed, 1 open, 2 half-open. |

`endpoint` is the FastAPI route template (`/v1/chat/completions`,
`/indexer/partition/{partition}/file/{file_id}`), never the raw URL, so label
cardinality stays bounded. Probe and documentation paths (`/health_check`,
`/metrics`, `/docs`, `/openapi.json`, `/redoc`) are not recorded. The standard
`process_*` and `python_*` series from the Prometheus client are exposed too.

## Access control

`/metrics` bypasses the regular authentication middleware: a scraper never
needs a user or admin token. Access is governed by one setting:

| Variable | Default | Effect |
| --- | --- | --- |
| `METRICS_TOKEN` | unset | Unset: the endpoint is open to anyone who can reach the API port. Set: the scraper must send `Authorization: Bearer <METRICS_TOKEN>`; any other credential, including an admin token, gets `403`. |

Leaving it unset is the usual posture when the API port is only reachable
from an internal network (a Compose network, a Kubernetes cluster, a private
VLAN). Set it whenever the API is reachable through a public reverse proxy or
Ingress, otherwise route names and traffic volumes become readable by anyone.
The metrics contain no request payloads, user data or secrets.

Check the endpoint from the host:

```bash
curl -fsS http://localhost:8080/metrics | head
# with a token:
curl -fsS -H "Authorization: Bearer $METRICS_TOKEN" http://localhost:8080/metrics | head
```

## Scraping from a VM deployment

With Docker Compose, `/metrics` is served on `APP_PORT` (8080 by default),
which the stack already publishes. Add a job to the external Prometheus:

```yaml
scrape_configs:
  - job_name: "openrag"
    metrics_path: "/metrics"
    scheme: https            # http if the API is not behind TLS
    static_configs:
      - targets: ["openrag.example.com:443"]
    # Only when METRICS_TOKEN is set on the OpenRAG side. Keep the token in a
    # file (mode 0400, owned by the Prometheus user), never inline.
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/openrag_metrics_token
```

If the Prometheus server cannot reach the VM directly, run an agent next to
OpenRAG (Prometheus in agent mode, or Grafana Alloy) that scrapes
`localhost:8080/metrics` and forwards the samples with `remote_write`.

## Scraping in Kubernetes

The Helm chart (`infra/charts/openrag-stack`) offers both discovery
mechanisms; pick the one your Prometheus uses.

**Prometheus Operator / kube-prometheus-stack.** Enable the `ServiceMonitor`
and label it so the operator's `serviceMonitorSelector` picks it up:

```yaml
openrag:
  metrics:
    serviceMonitor:
      enabled: true
      labels:
        release: kube-prometheus-stack
      interval: 30s
```

**Annotation-based discovery.** The API pod carries `prometheus.io/scrape`,
`prometheus.io/path` and `prometheus.io/port` annotations by default
(`openrag.metrics.prometheusAnnotations`), for a plain Prometheus configured
with the usual `kubernetes_sd_configs` relabeling.

**With a token.** Put it in the chart env Secret and tell the ServiceMonitor to
read it from there:

```yaml
env:
  secrets:
    METRICS_TOKEN: "<random secret>"
openrag:
  metrics:
    serviceMonitor:
      enabled: true
      bearerTokenFromSecret: true
```

With `env.existingSecret`, add a `METRICS_TOKEN` key to that Secret instead.
The default NetworkPolicy already admits the API port from outside the
namespace, so a Prometheus in a `monitoring` namespace reaches it without
extra rules.

## Grafana

Point a Prometheus data source at the server that scrapes OpenRAG and query
`openrag_http_requests_total` in Explore. A working setup returns series with
`method`, `endpoint` and `status_code` labels.

The dashboards under `infra/compose/grafana/dashboards/` are written for the
bundled stack: they expect a data source with UID `prometheus`, a scrape job
named `openrag`, and (for the system overview) node-exporter and the NVIDIA
GPU exporter. Import them as a starting point and adjust those three points to
your setup; see [Grafana HTTP dashboard](/openrag/documentation/grafana_http_dashboard/).

## Limitations

- Metrics are per process. With `ENABLE_RAY_SERVE=true` and several replicas,
  each replica answers `/metrics` with its own counters. Keep the default
  single uvicorn worker, or scrape each replica individually.
- Counters reset when the API restarts; use `rate()` and `increase()` rather
  than raw values.
- Indexing, inference and vector-store metrics are not exposed yet; only the
  HTTP layer and the circuit breakers are instrumented.
