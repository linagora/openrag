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
dashboard ConfigMaps, OpenRAG's alert rules and the API `ServiceMonitor`:

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
