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

## Managed PostgreSQL

The chart can run against a database that is provisioned outside OpenRAG, which is the recommended setup on OpenShift or cloud-managed PostgreSQL.

Pre-create the database before installing the release. If `POSTGRES_DATABASE` is not set, OpenRAG uses `partitions_for_collection_<VDB_COLLECTION_NAME>`. The app role does not need `CREATEDB` or superuser rights; it needs to connect to that database and own, or be allowed to create objects in, the target schema.

In `values.yaml`, disable the bundled PostgreSQL chart, set `postgresProvisioning.autoCreateDatabase` to `false`, set `postgresProvisioning.runMigrationsInApp` to `false`, and enable `postgresProvisioning.migrationJob`. Then provide the managed database connection through `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, and optionally `POSTGRES_DATABASE`.

The migration Job (`templates/postgres-migration-job.yaml`) is a Helm hook, annotated with `helm.sh/hook: pre-install,pre-upgrade`. You never invoke it directly: Helm runs it automatically as part of each `helm install` and `helm upgrade`, before it creates or updates the OpenRAG Deployment, and waits for it to finish. It applies the Alembic migrations against the pre-created database (it migrates the schema but does not create the database). The OpenRAG API then starts against an already-migrated schema.

When `postgresProvisioning.migrationJob` is disabled (the default), the Job is not rendered at all and the application runs migrations itself at startup instead.

## Monitoring Ray, Postgres and Milvus

The chart does not deploy Prometheus. It wires the stack's three dependencies
into one that already runs with the Prometheus Operator (kube-prometheus-stack
or a standalone operator), and every part of it is off by default:

```yaml
networkPolicy:
  metricsFrom:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: monitoring   # the namespace Prometheus runs in
ray:
  metrics:
    podMonitor:
      enabled: true                                  # requires ray.enabled=true
      labels: { release: <Prometheus release name> }
postgresql:
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
exports its own metrics. Ray prefixes the metrics OpenRAG records inside its
workers with `ray_`. The `PodMonitor` strips that prefix, so these metrics are
stored under the same `openrag_*` names the API's `/metrics` uses, and the alert
rules match them. Ray's own `ray_*` metrics keep their names. Milvus already exports from all five components (proxy,
mixcoord, datanode, querynode, streamingnode); only its `ServiceMonitor` is new.
None of the three endpoints authenticates, so none is routed through the Ingress.

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
  NetworkPolicy for this: it admitted any source on every port it listed,
  5432 included. Postgres is now reachable only from the release namespace, so
  a client in another namespace needs its own NetworkPolicy to reach 5432.
- **Ray pods created before this change.** KubeRay does not recreate Ray pods
  when the `RayCluster` changes, so after upgrading an existing release the
  workers keep exporting on 8080, which `networkPolicy.externalPorts` opens to
  every source, and the head target is down. Recreate them once:
  `kubectl delete pod -n <release namespace> -l ray.io/cluster=<RayCluster name>`.
  The selector must name your own `RayCluster`, or it matches no pods and
  nothing is recreated. The name is `<fullname>-raycluster`, which is
  `openrag-raycluster` with the default `fullnameOverride`; if you have set
  another `fullnameOverride`, or left it empty so that Helm derives the name
  from the release, `kubectl get raycluster -n <release namespace>` prints it.

Once enabled, this should return 1 for every Ray node, the Postgres pod and the
five Milvus pods:

```promql
up{namespace="<release namespace>", job=~".*(raycluster|postgresql|milvus).*"}
```

### What to watch

| Dependency | Query | Signal |
|---|---|---|
| Ray | `ray_tasks{State="PENDING_NODE_ASSIGNMENT"}` | Tasks no node has the resources to run |
| Ray | `ray_actors{State="RESTARTING"}` | Actors being restarted. This gauge is sampled, so it catches a crash loop but can miss a single fast restart |
| Ray | `ray_resources{Name="GPU"}` by `State` (`USED`, `AVAILABLE`) | GPU allocation per node |
| Ray | `ray_node_mem_used`, `ray_node_cpu_utilization`, `ray_object_store_memory` | Node resources |
| Postgres | `sum by (instance) (pg_stat_activity_count{namespace="<release namespace>"}) / sum by (instance) (pg_settings_max_connections{namespace="<release namespace>"})` | Connections against the server limit, per server |
| Postgres | `pg_stat_activity_max_tx_duration` | Longest open transaction |
| Postgres | `pg_database_size_bytes`, `pg_locks_count` | Database size, lock contention |
| Milvus | `milvus_proxy_req_latency`, `milvus_proxy_sq_latency` | Request and search/query latency |
| Milvus | `milvus_proxy_insert_vectors_count`, `milvus_proxy_search_vectors_count` | Insert and search throughput |
| Milvus | `milvus_querycoord_collection_num` | Loaded collections |
| Milvus | `milvus_datacoord_segment_num` by `segment_state`, `milvus_datacoord_compaction_task_num` | Segment and compaction backlog |
| Milvus | `process_resident_memory_bytes` by `component` | Memory per component |

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
- **Embedded Ray** (`ray.enabled=false`), which exports on no fixed port.
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
