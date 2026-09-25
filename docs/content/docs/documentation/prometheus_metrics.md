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
| `openrag_circuit_breaker_state` | gauge | `name` | Inference circuit breaker, as seen by the API process: 0 closed, 1 open, 2 half-open, -1 unknown. |
| `openrag_ingest_tasks` | gauge | `state` | Indexing tasks in flight (`QUEUED`, `SERIALIZING`), sampled at scrape time. The same cluster-wide number on every replica: aggregate with `max`, not `sum`. |
| `openrag_inference_requests_total` | counter | `provider`, `operation`, `outcome` | Calls to inference endpoints made by the API process. |
| `openrag_inference_duration_seconds` | histogram | `provider`, `operation` | Latency of those calls. |
| `openrag_llm_tokens_total` | counter | `operation`, `kind` | Tokens reported by the LLM for those calls. |

The inference metrics and breaker states here cover only the calls the API
process makes — embedding each query, answering it with the LLM, reranking.
The indexing workers make their own calls to the same endpoints (embedding,
captioning, contextualization, topic tagging); those, and every other metric
produced inside a Ray actor, are exported by Ray's metrics agent, not by this
endpoint. Under `ENABLE_RAY_SERVE=true` the API is itself a Ray actor, and only
the HTTP metrics and `openrag_ingest_tasks` remain here. The
[metrics reference](/openrag/documentation/metrics_reference/) lists every
metric, which target exports it, and how to query both together.

`endpoint` is the FastAPI route template (`/v1/chat/completions`,
`/indexer/partition/{partition}/file/{file_id}`), never the raw URL, so label
cardinality stays bounded. Probe and documentation paths (`/health_check`,
`/metrics`, `/docs`, `/openapi.json`, `/redoc`) are not recorded. The standard
`process_*` and `python_*` series from the Prometheus client are exposed too.

## Access control

`/metrics` bypasses the regular authentication middleware: a scraper never
needs a user or admin token, and admin tokens are not accepted there. The
route fails closed and is governed by two settings:

| Variable | Default | Effect |
| --- | --- | --- |
| `METRICS_TOKEN` | unset | The bearer a scraper must send as `Authorization: Bearer <METRICS_TOKEN>`. Any other credential, including an admin token, gets `403`. |
| `METRICS_ALLOW_UNAUTHENTICATED` | `false` | `true` serves the endpoint to anyone who can reach the API port when no token is set. |

| `METRICS_TOKEN` | `METRICS_ALLOW_UNAUTHENTICATED` | `GET /metrics` |
| --- | --- | --- |
| unset | `false` | `403` for everyone (the default). The API logs a warning at startup. |
| set | any | `200` with the bearer, `403` otherwise. |
| unset | `true` | `200` for anyone reaching the port. |

The token is the normal setup. The API port is the one a public reverse
proxy or Ingress forwards, so an open endpoint is readable wherever the API
is: route names, status-code distributions, traffic volumes and the
circuit-breaker gauge are useful reconnaissance even though the metrics
contain no request payloads, user data or secrets. Reserve
`METRICS_ALLOW_UNAUTHENTICATED=true` for a deployment that blocks `/metrics`
at the edge and scrapes the service from inside the network; see
[Opening the endpoint](#opening-the-endpoint-without-a-token).

The admin UI's **System > Metrics** tab does not read `/metrics`: it calls
`GET /monitoring/metrics`, the same exposition behind the ordinary admin
session, so it keeps working whatever the scrape settings are and an admin
never needs the scrape secret.

The bearer travels in clear on plain HTTP. Scrapes over a Compose network or
between pods carry it like every other request on that network; from outside,
scrape through TLS (the reverse proxy or Ingress that already terminates it).

Check the endpoint from the host:

```bash
curl -fsS -H "Authorization: Bearer $METRICS_TOKEN" http://localhost:8080/metrics | head
```

## Scraping from a VM deployment

With Docker Compose, `/metrics` is served on `APP_PORT` (8080 by default),
which the stack already publishes. Set `METRICS_TOKEN` in the OpenRAG `.env`
and add a job to the external Prometheus:

```yaml
scrape_configs:
  - job_name: "openrag"
    metrics_path: "/metrics"
    scheme: https            # the bearer travels in clear: keep TLS in front
    static_configs:
      - targets: ["openrag.example.com:443"]
    # Keep the token in a file (mode 0400, owned by the Prometheus user),
    # never inline.
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/openrag_metrics_token
```

The admin UI proxy (`ADMIN_UI_PORT`) answers `404` on `/metrics`: scrape the
API port, not the front door.

If the Prometheus server cannot reach the VM directly, run an agent next to
OpenRAG (Prometheus in agent mode, or Grafana Alloy) that scrapes
`localhost:8080/metrics` with the same bearer and forwards the samples with
`remote_write`.

The bundled monitoring overlay (`monitoring.docker-compose.yaml`) needs no
extra step: it writes `METRICS_TOKEN` into the Prometheus container as the
`credentials_file` of its `openrag` job, and refuses to start when the
variable is missing from `.env`.

## Scraping in Kubernetes

The Helm chart (`infra/charts/openrag-stack`) offers both discovery
mechanisms; pick the one your Prometheus uses. On a cluster without a
Prometheus, `monitoring.bundled` installs one with Grafana and configures the
scrape below itself; see [Kubernetes monitoring](/openrag/documentation/kubernetes/#monitoring).

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
with the usual `kubernetes_sd_configs` relabeling. The annotations only say
"scrape me": the job behind them must send the bearer
(`authorization.credentials_file` in that job, or a mounted Secret), or every
scrape gets `403`.

**The token.** Put it in the chart env Secret and tell the ServiceMonitor to
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
extra rules. The ServiceMonitor scrapes the Service on port 8080 inside the
cluster, plain HTTP like the rest of the pod-to-pod traffic; the Ingress TLS
is not involved.

## Opening the endpoint without a token

`METRICS_ALLOW_UNAUTHENTICATED=true` (compose `.env`, or
`env.config.METRICS_ALLOW_UNAUTHENTICATED: "true"` in Helm) serves
`/metrics` to anyone who can reach the API port. Use it only when that port
is not exposed as-is:

- **Compose.** The admin UI proxy already returns `404` on `/metrics`, so
  the exposure is `APP_PORT` itself. Bind it to the host or a private
  interface (`APP_PORT=127.0.0.1:8080` in `.env` keeps it off the public
  interfaces) or firewall it, and scrape from that network. The bundled
  overlay still needs `METRICS_TOKEN` set: it always sends the bearer.
- **Kubernetes.** Block `/metrics` at the Ingress, with whatever your
  controller offers for a path-level deny (a `location = /metrics { return
  404; }` server snippet on ingress-nginx, a `Route` rule on OpenShift), and
  scrape the Service from inside the cluster. Port 8080 is the one
  `networkPolicy.externalPorts` opens to the Ingress controller, so without
  that rule the open endpoint is reachable wherever the API is.

## Grafana

Point a Prometheus data source at the server that scrapes OpenRAG and query
`openrag_http_requests_total` in Explore. A working setup returns series with
`method`, `endpoint` and `status_code` labels.

The dashboards under `infra/charts/openrag-stack/dashboards/` load unchanged into
any Grafana. It is their only copy: the Compose overlay provisions them from
there, and the Helm chart renders them as ConfigMaps for a Grafana dashboard
sidecar ([Kubernetes monitoring](/openrag/documentation/kubernetes/#monitoring)).

| Dashboard | UID | Shows |
| --- | --- | --- |
| OpenRAG HTTP Metrics | `openrag-http` | Request rate, errors and latency per route ([guide](/openrag/documentation/grafana_http_dashboard/)) |
| OpenRAG Service | `openrag-service` | Indexing, inference and catalog drift ([guide](/openrag/documentation/grafana_service_dashboard/)) |
| Infrastructure Overview | `system-overview` | Host CPU, memory, disk and GPU; needs node-exporter and a GPU exporter |

Every panel reads the **Data source** variable (`DS_PROMETHEUS`), which defaults
to Grafana's default Prometheus data source and can be switched from the top of
the dashboard; no data source UID is written into the JSON. Panels that need
the API's scrape job find it from the series the API exports (`openrag` on
Compose, the ServiceMonitor's Service name on Kubernetes), so no job name is
written in either. The Service dashboard finds Ray's job the same way, from a
counter only the Ray actors export.

To load them into your own Grafana, import each file through **Dashboards → New
→ Import**, provision them from disk, or on Kubernetes enable
`monitoring.dashboards`. Keep the files as they are in the
repository rather than re-exporting them with **Export for sharing externally**:
that option adds an `__inputs` section, which only the import dialog resolves.
File provisioning and a ConfigMap sidecar load the JSON as-is and would leave
it unresolved, so a unit test rejects such an export.

## Limitations

- Metrics are per process. With `ENABLE_RAY_SERVE=true` and several replicas,
  each replica answers `/metrics` with its own counters behind one
  load-balancing proxy, so a scrape returns a random replica, and replicas
  cannot be addressed individually over HTTP. Keep the default single uvicorn
  worker.
- The Helm discovery (`openrag.metrics.*`) covers the uvicorn topology only.
  With `ray.enabled=true` and `ENABLE_RAY_SERVE=true` the API is served by the
  RayCluster head Service, not by the `openrag` Service on port 8080: the
  chart then renders no `prometheus.io/*` annotations, and enabling the
  ServiceMonitor fails the install with a message saying so. Scraping Ray
  Serve replicas needs a per-replica target (a PodMonitor on the Ray pods with
  a dedicated metrics port) and is not implemented yet.
- Counters reset when the API restarts; use `rate()` and `increase()` rather
  than raw values.
- Vector-store metrics are not exposed yet. Indexing and worker-side
  inference metrics are exported on Ray's metrics agent, which the chart
  scrapes through `ray.metrics.podMonitor` (off by default) and the Compose
  overlay through its `openrag-ray` job (see the
  [metrics reference](/openrag/documentation/metrics_reference/)).
