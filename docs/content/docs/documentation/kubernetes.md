---
title: Deploying OpenRAG on Kubernetes
---

This guide explains how to deploy the **OpenRAG** stack on a Kubernetes cluster using Helm.

---

## Prerequisites

- A **Kubernetes cluster** with **GPU nodes** available (NVIDIA runtime) and nvidia-gpu-operator installed.
- A **StorageClass** that supports **ReadWriteMany** (`RWX`) access mode.  
  This is required because the Ray cluster workers and the OpenRAG app need to access the same shared volumes (e.g. for `.venv`, model weights, logs, data).
- If using ingress, the ingress-nginx controller needs to be installed on the cluster.

---

## Steps

1. **Create a `values.yaml` file**:

   - Copy or create a new `values.yaml` at the root of your repo.
   - You can see the full example file inside the chart:
     [values.yaml](https://github.com/linagora/openrag/blob/dev/infra/charts/openrag-stack/values.yaml)
   - Customize the values you need (e.g., image tags, resources, ingress host, storage class, environment variables, secrets).

2. **Set environment and secrets**:

   - Edit the `env.config` and `env.secrets` sections in your `values.yaml`.
   - Secrets (API keys, tokens, Hugging Face credentials, etc.) will be mounted into the cluster as Kubernetes secrets.
   - For managed PostgreSQL, point the `POSTGRES_*` values at the external database and disable database auto-creation.

3. **Install or upgrade the release from GHCR**:

   ```bash
   helm upgrade\
      --install openrag oci://ghcr.io/linagora/openrag-stack\
      -f ./values.yaml\
      --version 0.6.0
   ```

   - `openrag` is the Helm release name.
   - `oci://ghcr.io/linagora/openrag-stack` is the remote chart location.
   - `-f ./values.yaml` specifies your custom configuration.
   - `--version 0.6.0` ensures you deploy a specific chart version — check `Chart.yaml` for the current version before installing.

---

## Upgrading to chart 0.6.0

Chart 0.6.0 renames the PVCs, ConfigMap and Secret from fixed `rag-*` names to
`{{ fullname }}-*`, so they follow the release instead of colliding between two
installs in one namespace. With the default `fullnameOverride: "openrag"`:

| Before | After |
|---|---|
| `rag-model-weights`, `rag-data`, `rag-logs`, `rag-venv` | `openrag-model-weights`, `openrag-data`, `openrag-logs`, `openrag-venv` |
| `rag-env` | `openrag-env` |
| `rag-env-secrets` | `openrag-env-secrets` |

The old PVCs carry `helm.sh/resource-policy: keep`, so **the upgrade does not
delete them — but it does not mount them either**. It provisions new, empty ones
under the new names, and the release comes up as if it had no indexed data. Pick
one before upgrading:

- **Keep the existing volumes.** Set `fullnameOverride: "rag"`, which reproduces
  the old names exactly. Also set `postgresql.fullnameOverride`,
  `milvus.fullnameOverride` and `vllm.hfTokenSecretName` to match (they are kept
  in sync by hand — `values.yaml` explains why, and `NOTES.txt` warns on an
  HF_TOKEN secret-name mismatch).
- **Migrate to the new names.** Copy the data across (e.g. a Job mounting both
  PVCs), then delete the old ones once the release is healthy.

## Upgrading to chart 0.6.7

Two changes apply to every release, whether or not it scrapes anything:

- **Ray workers move their metrics off port 8080.** Workers exported on
  KubeRay's default 8080, which `networkPolicy.externalPorts` opens to every
  source, so their unauthenticated metrics were public. They now export on 8090,
  like the head. KubeRay does not recreate Ray pods when the `RayCluster`
  changes, so existing workers stay on 8080 until they are recreated. Delete
  them once after the upgrade, at a time when no indexing is running, since
  anything running on them is interrupted:

  ```bash
  kubectl delete pod -n <release namespace> \
    -l ray.io/cluster=<RayCluster name>,ray.io/node-type=worker
  ```

  The name is `<fullname>-raycluster`, which is `openrag-raycluster` with the
  default `fullnameOverride`. If you have set another `fullnameOverride`, or
  left it empty so that Helm derives the name from the release,
  `kubectl get raycluster -n <release namespace>` prints it. The upgrade notes
  print the whole command with the name filled in. A wrong name selects no pods,
  and the workers stay on 8080.

  Leave the head out. The chart configures no GCS fault tolerance, so deleting
  the head restarts the whole Ray cluster, and every actor on it is lost. The
  head already exported on 8090, so recreating the workers closes the exposure.
  The head needs recreating only to bring up its own scrape target (see
  [Monitoring Ray, Postgres and Milvus](#monitoring-ray-postgres-and-milvus)).
- **Postgres no longer accepts connections from other namespaces.** The bitnami
  sub-chart rendered its own NetworkPolicy, which admitted any source on 5432.
  The chart now turns it off, along with the read replicas' policy under
  `postgresql.architecture: replication`, so Postgres gets the same rules as
  every other pod. With `networkPolicy.enabled` (the default), only the release
  namespace reaches it, and a client in another namespace needs its own
  NetworkPolicy.

The upgrade notes print each step that applies (the Ray one with `ray.enabled`,
the Postgres one with `postgresql.enabled` and `networkPolicy.enabled`) on the
upgrade from chart 0.6.6 or earlier, whichever version it moves to, and on no
later upgrade: if you skip the worker delete then, nothing reminds you. The
notes find that version in the `helm.sh/chart` label of the
`<fullname>-default-deny` NetworkPolicy. When they cannot, with
`networkPolicy.enabled: false` or under `--dry-run=client`, they print the steps
that apply on every upgrade.

## Notes

For the default direct-API deployment, startup and liveness probes use
`/health_check`, while the readiness probe uses `/ready`. When
`ENABLE_RAY_SERVE=true`, the chart automatically uses exec probes against the
Ray head because the Ray Serve HTTP proxy does not run on the API pod. Ray
Serve requires `ray.enabled=true`; Helm rejects that invalid combination.
Readiness returns 503 when startup is incomplete or PostgreSQL, Milvus, or Ray is
unavailable. Model checks are reported in the response but do not gate the whole
API, so optional VLM/STT and partition-specific model endpoints do not remove
healthy replicas from service. Checks use short timeouts and results are cached
for two seconds. Model probes check availability without running inference; they
do not guarantee every request will succeed. Use an application image that
includes `/ready` with these probes.

Readiness uses the configured model endpoints and API keys, just like inference.
HTTP endpoints do not encrypt those credentials; configure HTTPS when transport
encryption is required.

Prometheus exposes aggregate endpoint state through
`openrag_model_endpoint_ready{provider,kind}` and discovery health through
`openrag_model_endpoint_discovery_up`. These labels are intentionally bounded,
and the public readiness endpoint reports aggregate configuration-reference
counts without exposing partition or preset names.

- If using a public IP instead of a hostname, you can leave `ingress.host` empty in your `values.yaml`.  
  The ingress will then match all hosts.

- If you later configure a hostname + TLS (via cert-manager), just update `ingress.host` and redeploy.

- Ensure your GPU nodes have the correct NVIDIA drivers and `nvidia` `RuntimeClass` configured.

## Monitoring

The chart ships OpenRAG's Grafana dashboards and a `ServiceMonitor` for the
API's `GET /metrics`. The dashboards exist once, in
`infra/charts/openrag-stack/dashboards/`, and the Docker Compose monitoring
overlay loads the same files. They reach a Grafana in one of three ways:

| Deployment | Prometheus and Grafana | What the chart renders |
| --- | --- | --- |
| Kubernetes, next to a platform's monitoring | the platform's | a ConfigMap per dashboard for its Grafana sidecar, and the API `ServiceMonitor` |
| Kubernetes, standalone | kube-prometheus-stack, bundled by `monitoring.bundled` | the same objects, plus that stack |
| Docker Compose | the overlay's containers | nothing: the overlay mounts `dashboards/` and Grafana provisions it from disk |

Every dashboard reads its data source through a variable that starts on
Grafana's default Prometheus, so the files load unchanged in any of them (see
[Prometheus metrics](/openrag/documentation/prometheus_metrics/#grafana)).

### Next to an existing Prometheus and Grafana

Leave `monitoring.bundled` off and turn on the objects, labelled the way the
platform selects them:

```yaml
monitoring:
  dashboards:
    enabled: true
openrag:
  metrics:
    serviceMonitor:
      enabled: true
      bearerTokenFromSecret: true
      labels:
        release: kube-prometheus-stack   # what the platform's Prometheus selects
env:
  secrets:
    METRICS_TOKEN: "<openssl rand -hex 16>"
```

Ask whoever runs the platform's monitoring, and set:

| Question | Value |
| --- | --- |
| Which label does your Prometheus select `ServiceMonitor` objects on, and does it watch this namespace? | `openrag.metrics.serviceMonitor.labels`. The monitor is created in the release namespace. |
| Does your Grafana run the dashboard sidecar, and which label does it watch? | `monitoring.dashboards.label` and `labelValue`, by default `grafana_dashboard: "1"` (kube-prometheus-stack's default). |
| Which namespaces does the sidecar search? | Nothing to set if it searches all of them (kube-prometheus-stack's default). If it only watches its own, set `monitoring.dashboards.namespace` to the Grafana namespace. |
| Which Grafana folder? | `monitoring.dashboards.folder` (default `OpenRAG`), written into the annotation named by `monitoring.dashboards.folderAnnotation` (default `grafana_folder`). It must match the sidecar's `folderAnnotation`, which also needs `provider.foldersFromFilesStructure`. A sidecar without one uses its default folder. |

A ConfigMap or monitor with the wrong label is created and then ignored, which
looks exactly like one that works. After installing, check that the dashboards
appear in Grafana and that the target is up in Prometheus (**Status → Targets**).
Without the Prometheus Operator, the API pod's `prometheus.io/*` annotations
serve annotation-based discovery instead; see
[Scraping in Kubernetes](/openrag/documentation/prometheus_metrics/#scraping-in-kubernetes).

### Standalone: the bundled stack

On a cluster with no monitoring of its own, `monitoring.bundled: true` installs
kube-prometheus-stack in the release: Prometheus Operator, Prometheus,
Alertmanager, Grafana, node-exporter and kube-state-metrics. It also turns on the
dashboard ConfigMaps, the API `ServiceMonitor` and the Ray `PodMonitor`:

```yaml
monitoring:
  bundled: true
env:
  secrets:
    METRICS_TOKEN: "<openssl rand -hex 16>"
```

- **`METRICS_TOKEN` is required.** The bundled Prometheus always sends it as the
  bearer on `GET /metrics`, like the Compose overlay, and the chart refuses to
  render without it. With `env.existingSecret` or an external secrets provider,
  that Secret must carry the key, since the chart cannot check it.
- **Every monitor is scraped.** The bundled Prometheus selects all
  `ServiceMonitor`, `PodMonitor` and `PrometheusRule` objects in the cluster, not
  only those labelled with its release. A third-party monitor, such as the GPU
  Operator's DCGM exporter, needs no `release` label.
- **The control plane is not scraped.** On a managed cluster (EKS, GKE, AKS and
  the like) the controller manager, scheduler and etcd run out of reach, and
  some clusters replace kube-proxy. kube-prometheus-stack would still look for
  them, and its `KubeControllerManagerDown`, `KubeSchedulerDown` and
  `KubeProxyDown` alerts would fire forever, so the chart turns the four off,
  with their rules and dashboards. On a self-managed control plane, turn them
  back on:

  ```yaml
  kubePrometheusStack:
    kubeControllerManager: { enabled: true }
    kubeScheduler: { enabled: true }
    kubeEtcd: { enabled: true }
    kubeProxy: { enabled: true }
  ```

  Each component must also serve its metrics on an address Prometheus can
  reach. kubeadm binds all four to `127.0.0.1`, where the scrape is refused and
  the same alerts fire.
- **History survives a restart.** Prometheus keeps 30 days, capped at 18 GB, on a
  20 Gi `ReadWriteOnce` volume from the default StorageClass. Tune it under
  `kubePrometheusStack.prometheus.prometheusSpec`.
- **Every workload has requests and a memory limit.** Upstream sets none, which
  would leave them BestEffort: nothing reserved, and evicted first under memory
  pressure. The values come from a one-node kind cluster under the queries of
  every bundled dashboard, with room above what each used there. Prometheus is
  the one that grows, with the number of series rather than the retention: its
  1 Gi request holds about 190k series, and `values.yaml` shows how to estimate
  yours from the node, pod and API server counts. Watch
  `prometheus_tsdb_head_series` and raise
  `kubePrometheusStack.prometheus.prometheusSpec.resources` with it.
- **Upstream values go under `kubePrometheusStack`.** The sub-chart is aliased,
  so an upstream `kube-prometheus-stack.x.y` key is `kubePrometheusStack.x.y`
  here.
- **The operator's admission webhook is let through the NetworkPolicy.** It
  validates `PrometheusRule` and `AlertmanagerConfig` objects before they are
  stored, and its caller is the API server — not a pod, and on a managed
  control plane not on the cluster network at all, so no selector can name it.
  The chart opens that one port, to any source, on the webhook's own pod: it
  runs in a separate deployment
  (`kubePrometheusStack.prometheusOperator.admissionWebhooks.deployment`),
  which serves the webhook, `/metrics` and `/healthz` only, because the
  operator's listener would also expose `/debug/pprof/` on the same port. Give
  `networkPolicy.webhookFrom` a peer to narrow it where the control plane's
  address is known, and do narrow it if you turn that deployment off. Without
  the opening the call is dropped wherever the API server is off-node, and
  kube-prometheus-stack's default `failurePolicy` (`Ignore`) then skips
  validation silently.
- **The dashboard label and folder must match the bundled Grafana.** Its
  sidecar is configured under `kubePrometheusStack.grafana.sidecar.dashboards`,
  which cannot follow `monitoring.dashboards`, so the chart refuses to render
  when the label, label value or folder annotation differ. Change them on
  both sides together.
- **No API scrape under Ray Serve.** With `ray.enabled=true` and
  `ENABLE_RAY_SERVE=true`, no `ServiceMonitor` is rendered for the API, and the
  HTTP dashboard stays empty (see the Limitations in
  [Prometheus metrics](/openrag/documentation/prometheus_metrics/#limitations)).

The install notes print how to reach Grafana:

```bash
kubectl -n <namespace> port-forward svc/openrag-grafana 3000:80
# user and password:
kubectl -n <namespace> get secret openrag-grafana -o jsonpath='{.data.admin-user}' | base64 -d
kubectl -n <namespace> get secret openrag-grafana -o jsonpath='{.data.admin-password}' | base64 -d
```

With `kubePrometheusStack.grafana.admin.existingSecret` set, as under Argo CD
below, Grafana reads them from that Secret instead, under the keys named by
`admin.userKey` and `admin.passwordKey` (`admin-user` and `admin-password` by
default). The notes print those.

The OpenRAG dashboards are in the **OpenRAG** folder, next to
kube-prometheus-stack's own Kubernetes dashboards. To publish Grafana through an
Ingress, enable `kubePrometheusStack.grafana.ingress` and add `3000` to
`networkPolicy.externalPorts`: the default-deny policy admits only `8080` from
outside the namespace.

Alertmanager starts with no receiver. Alerts show as firing in Prometheus and
Alertmanager but notify nobody until `kubePrometheusStack.alertmanager.config`
routes them.

Before turning it on:

- **Not next to another Prometheus Operator.** A second operator competes with
  the first over the same CRDs and objects. If the cluster already runs one, use
  the setup in the previous section.
- **It needs cluster-wide rights, and one release per cluster.** Besides the
  operator's CRDs, it creates cluster-scoped objects (ClusterRoles and their
  bindings, the admission webhook configurations) and Services in `kube-system`
  (CoreDNS's, and the kubelet's, which the operator maintains), so rights over
  the release namespace alone are not enough to install it. Their names are
  fixed (`openrag-monitoring-*`, `openrag-grafana-*`), so a second release with
  `bundled` in the same cluster fails at install: point it at the first one's
  Prometheus and Grafana instead, as in the previous section.
- **node-exporter needs host access.** It runs on every node with `hostNetwork`,
  `hostPID` and the node's `/`, `/proc` and `/sys` mounted. A namespace that
  enforces the Pod Security `baseline` or `restricted` level rejects its pods.
  On a node that already runs a node-exporter, the two need the same host port,
  9100, and the second one stays `Pending`. In either case set
  `kubePrometheusStack.nodeExporter.enabled: false`. The host panels of the
  Infrastructure Overview dashboard (CPU, memory, disk, network and load) then
  stay empty, unless this Prometheus scrapes the existing node-exporter, for
  example through its `ServiceMonitor`.
- **An existing release needs the CRDs first.** Helm installs CRDs on the first
  install only, so an upgrade that turns `bundled` on fails with
  `no matches for kind "Alertmanager" in version "monitoring.coreos.com/v1"`.
  Helm never upgrades CRDs either, so
  repeat this after a chart upgrade that moves kube-prometheus-stack:

  ```bash
  helm show crds oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack \
    --version 91.4.1 | kubectl apply --server-side -f -
  ```

  `--server-side` is required: the Prometheus CRD is over 1 MB, past the 256 KiB
  a client-side apply can record.
- **Argo CD needs two settings.** Sync with `ServerSideApply=true`, for the same
  reason. Argo CD also renders without cluster lookups, so the generated Grafana
  password would change on every sync. Point
  `kubePrometheusStack.grafana.admin.existingSecret` at a Secret you manage
  instead.
- **Under Argo CD, rule validation starts once the whole Application is
  Healthy.** The admission webhook's certificate comes from two Helm-hook Jobs,
  which Argo CD runs as sync hooks: `openrag-monitoring-admission-create` before
  the sync, and `openrag-monitoring-admission-patch`, which gives the webhook
  configurations their CA, after it. Argo CD runs that second hook only once
  every resource in the Application is Healthy, OpenRAG's included. Until then
  the API server cannot verify the webhook, and under the default
  `failurePolicy` (`Ignore`) it stores `PrometheusRule` and `AlertmanagerConfig`
  objects unvalidated. Later syncs keep the certificate and the CA. Where
  cert-manager runs, set
  `kubePrometheusStack.prometheusOperator.admissionWebhooks.certManager.enabled: true`:
  cert-manager issues the certificate instead of the hooks, and validation
  works from the first sync.

## Managed PostgreSQL

The chart can run against a database that is provisioned outside OpenRAG, which is the recommended setup on OpenShift or cloud-managed PostgreSQL.

Pre-create the database before installing the release. If `POSTGRES_DATABASE` is not set, OpenRAG uses `partitions_for_collection_<VDB_COLLECTION_NAME>`. The app role does not need `CREATEDB` or superuser rights; it needs to connect to that database and own, or be allowed to create objects in, the target schema.

In `values.yaml`, disable the bundled PostgreSQL chart, set `postgresProvisioning.autoCreateDatabase` to `false`, set `postgresProvisioning.runMigrationsInApp` to `false`, and enable `postgresProvisioning.migrationJob`. Then provide the managed database connection through `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, and optionally `POSTGRES_DATABASE`.

The migration Job (`templates/postgres-migration-job.yaml`) is a Helm hook, annotated with `helm.sh/hook: pre-install,pre-upgrade`. You never invoke it directly: Helm runs it automatically as part of each `helm install` and `helm upgrade`, before it creates or updates the OpenRAG Deployment, and waits for it to finish. It applies the Alembic migrations against the pre-created database (it migrates the schema but does not create the database). The OpenRAG API then starts against an already-migrated schema.

When `postgresProvisioning.migrationJob` is disabled (the default), the Job is not rendered at all and the application runs migrations itself at startup instead.

## GPU metrics

The chart deploys no GPU exporter. GPU metrics come from the DCGM exporter that
the NVIDIA GPU Operator — a prerequisite above — runs on every GPU node
(`dcgmExporter.enabled`, on by default). What is left is getting Prometheus to
scrape it and Grafana to show it.

1. **Scrape the exporter.** With the Prometheus Operator (e.g.
   kube-prometheus-stack), have the GPU Operator create its ServiceMonitor. That
   is the default from GPU Operator v26.7.0; earlier releases ship it disabled:

   ```bash
   # Pin the version you already run: --reuse-values without --version also
   # upgrades the GPU Operator to the latest chart.
   helm upgrade gpu-operator nvidia/gpu-operator -n gpu-operator \
     --version <installed version> --reuse-values \
     --set dcgmExporter.serviceMonitor.enabled=true \
     --set dcgmExporter.serviceMonitor.additionalLabels.release=kube-prometheus-stack
   ```

   The `release` label must be whatever your Prometheus `serviceMonitorSelector`
   matches (kube-prometheus-stack selects its own release name). An unmatched
   ServiceMonitor is created but never scraped. Without the Prometheus Operator,
   the exporter's `nvidia-dcgm-exporter` Service (port 9400) carries the
   `prometheus.io/scrape: "true"` annotation for annotation-based discovery.

2. **Check it arrived.** `DCGM_FI_DEV_GPU_UTIL` should return series in
   Prometheus. Expect one series per GPU — or, when the exporter runs with
   `KUBERNETES_VIRTUAL_GPUS=true` for time-sliced or MPS-shared GPUs, one per pod
   using each GPU. The dashboard counts each GPU once either way.

3. **Get the dashboard in front of you.** The GPU panels of the Infrastructure
   Overview dashboard (`infra/charts/openrag-stack/dashboards/system-overview.json`)
   read the DCGM exporter here and `nvidia_gpu_exporter` under Docker Compose —
   whichever is scraped — so the same file serves both. The chart ships it to
   Grafana for you: see [Monitoring](#monitoring) above. Imported by hand
   instead, it needs no editing — every panel reads a data source variable that
   starts on Grafana's default Prometheus.

The GPU panels aggregate every GPU that Prometheus scrapes, not only the nodes
running OpenRAG, and the host panels likewise need node-exporter
(kube-prometheus-stack ships it) and aggregate every node.

## Monitoring Ray, Postgres and Milvus

The chart wires the stack's three dependencies into a Prometheus that runs the
Prometheus Operator. Every part of it is off by default. `monitoring.bundled`
turns on the Ray `PodMonitor` and nothing else here: OpenRAG's ingestion series
and the worker side of its inference series exist on Ray's endpoint only, and the
alerts built on them cannot fire without it. With the bundled stack, set only the
Postgres and Milvus `enabled` switches and the Postgres password: the bundled Prometheus runs
in the release namespace and selects every monitor, so it needs neither
selector labels nor `networkPolicy.metricsFrom`. Next to an existing Prometheus
(kube-prometheus-stack or a standalone operator), set all of it:

```yaml
networkPolicy:
  metricsFrom:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: monitoring   # the namespace Prometheus runs in
ray:
  metrics:
    podMonitor:
      enabled: true                                  # the RayCluster, or the embedded Ray
      labels: { release: <Prometheus release name> }
postgresql:
  auth:
    postgresPassword: <password>                     # required by metrics.enabled, see below
  metrics:
    enabled: true                                    # adds the exporter sidecar: restarts Postgres
    serviceMonitor:
      enabled: true
      labels: { release: <Prometheus release name> }
milvus:
  metrics:
    serviceMonitor:
      enabled: true
      additionalLabels: { release: <Prometheus release name> }
```

Ray gets a `PodMonitor` rather than a `ServiceMonitor` because every Ray node
exports its own metrics, on port 8090. With `ray.enabled=false` (the default) the
only Ray node is embedded in the `openrag` pod: the chart pins its metrics port to
8090 with `RAY_METRICS_EXPORT_PORT`, declares it as `ray-metrics`, and the
`PodMonitor` selects that pod instead of the RayCluster's. Ray prefixes the metrics OpenRAG records inside its
workers with `ray_`. The `PodMonitor` strips that prefix, so these metrics are
stored under the same `openrag_*` names the API's `/metrics` uses, and the alert
rules match them. Ray's own `ray_*` metrics keep their names. Milvus already exports from all five components (proxy,
mixcoord, datanode, querynode, streamingnode); only its `ServiceMonitor` is new.
None of the three endpoints authenticates, so none is routed through the Ingress.

The Postgres exporter signs in as the `postgres` superuser, with the password in
the sub-chart's Secret. Unless `postgresql.auth.postgresPassword` or
`postgresql.auth.existingSecret` sets that password, the sub-chart generates it
again on every render that cannot read the live Secret (`helm template`,
Argo CD), while the server keeps the first one, and the exporter's login fails
after a later sync. The chart therefore refuses `postgresql.metrics.enabled`
without one of the two. On a new release, any password works. On a running
release, set the one the server already has:
`kubectl get secret -n <release namespace> openrag-postgresql -o jsonpath='{.data.postgres-password}' | base64 -d`.

Three things can go wrong without failing the install:

- **Selector labels.** A monitor without the label its Prometheus selects on is
  created and never scraped. The example assumes kube-prometheus-stack, which
  selects on `release: <its Helm release name>` by default; other setups may
  use another label. Read the selectors with
  `kubectl get prometheus -A -o jsonpath='{..podMonitorSelector}{..serviceMonitorSelector}'`.
  The Postgres sub-chart calls the key `labels`; the Milvus one calls it `additionalLabels`.
- **NetworkPolicy.** The default-deny policy admits only same-namespace traffic.
  Until Prometheus's namespace is listed in `networkPolicy.metricsFrom`, its
  targets report `up == 0`. Each entry opens only the metrics port, and only on
  the pods that export it. The chart turns off the Postgres sub-chart's own
  NetworkPolicy for this: it admitted any source on every port it listed, the
  exporter's 9187 included (see [Upgrading to chart 0.6.7](#upgrading-to-chart-067)).
- **Ray pods created by chart 0.6.6 or earlier.** KubeRay does not recreate Ray
  pods when the `RayCluster` changes. Old workers still export on 8080 and need
  recreating anyway ([Upgrading to chart 0.6.7](#upgrading-to-chart-067)). The
  old head exports on 8090, but it also carries the `metrics: 8080` port that
  KubeRay adds to a container without a port of that name. Its target therefore
  stays down until the head is recreated too. Recreating the head restarts the
  whole Ray cluster, so leave it for a maintenance window:
  `kubectl delete pod -n <release namespace> -l ray.io/cluster=<RayCluster name>,ray.io/node-type=head`.

Once enabled, this should return 1 for every Ray node, the Postgres pod and the
five Milvus pods:

```promql
up{namespace="<release namespace>", job=~".*(raycluster|postgresql|milvus).*"}
```

### What to watch

| Dependency | Query | Signal |
|---|---|---|
| Ray | `ray_tasks{namespace="<release namespace>", State="PENDING_NODE_ASSIGNMENT"}` | Tasks no node has the resources to run |
| Ray | `ray_actors{namespace="<release namespace>", State="RESTARTING"}` | Actors being restarted. This gauge is sampled, so it catches a crash loop but can miss a single fast restart |
| Ray | `ray_resources{namespace="<release namespace>", Name="GPU"}` by `State` (`USED`, `AVAILABLE`) | GPU allocation per node |
| Ray | `ray_node_mem_used{namespace="<release namespace>"}`, and the same for `ray_node_cpu_utilization` and `ray_object_store_memory` | Node resources |
| Postgres | `sum by (instance) (pg_stat_activity_count{namespace="<release namespace>"}) / sum by (instance) (pg_settings_max_connections{namespace="<release namespace>"})` | Connections against the server limit, per server |
| Postgres | `pg_stat_activity_max_tx_duration{namespace="<release namespace>"}` | Longest open transaction |
| Postgres | `pg_database_size_bytes{namespace="<release namespace>"}`, `pg_locks_count{namespace="<release namespace>"}` | Database size, lock contention |
| Milvus | `histogram_quantile(0.99, sum by (le, function_name) (rate(milvus_proxy_req_latency_bucket{namespace="<release namespace>"}[5m])))`, and the same over `milvus_proxy_sq_latency_bucket` by `query_type` | p99 latency in milliseconds, per request type and per search/query type |
| Milvus | `sum(rate(milvus_proxy_insert_vectors_count{namespace="<release namespace>"}[5m]))`, `sum(rate(milvus_proxy_search_vectors_count{namespace="<release namespace>"}[5m]))` | Vectors inserted and searched per second |
| Milvus | `milvus_querycoord_collection_num{namespace="<release namespace>"}` | Loaded collections |
| Milvus | `milvus_datacoord_segment_num{namespace="<release namespace>"}` by `segment_state`, `milvus_datacoord_compaction_task_num{namespace="<release namespace>"}` | Segment and compaction backlog |
| Milvus | `sum by (component) (process_resident_memory_bytes{namespace="<release namespace>", component!=""})` | Memory per component |

### Not covered

- **The API's connection-pool wait.** The pool lives in the OpenRAG process, so
  Postgres cannot see a request queued for a connection. That needs an
  application metric.
- **Slow queries.** These need `pg_stat_statements`, which means a
  `shared_preload_libraries` change on the server plus the exporter's
  `--collector.stat_statements`. Without it, `pg_stat_activity_max_tx_duration`
  still shows long-running transactions.
- **Volume fill.** `pg_database_size_bytes` is the database size, not how full
  its PVC is; use the kubelet's `kubelet_volume_stats_*` series for that.
- **MinIO and etcd**, Milvus's own dependencies.

### Series volume

Milvus and Ray are chatty. Samples per scrape on a single-node install holding
one 5 000-row collection:

| Target | Samples per scrape |
|---|---|
| Milvus, all five components | ~45 000 (the streaming node alone ~20 000) |
| Ray, head and one worker, lightly loaded | ~1 150 |
| Postgres | ~550 |

Where the Prometheus belongs to a platform team, agree on that volume before
enabling Milvus's monitor.
