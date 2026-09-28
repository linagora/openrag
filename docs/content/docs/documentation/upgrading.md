---
title: Upgrading OpenRAG
tableOfContents:
  maxHeadingLevel: 4
---

Each section below covers one upgrade. Read the section for the version you are
moving to before you start, and skip none of the versions in between.

## 2.2.x to 2.3.0 on Kubernetes

This section upgrades a running Helm release of OpenRAG to 2.3.0. It applies to a
release installed from chart `0.6.4` (OpenRAG 2.2.1) or `0.6.5` (OpenRAG 2.2.2),
and to a release installed from a `-dev` chart built from the development branch
before 28 September 2026.

The upgrade needs a **maintenance window**: the Milvus collection must be migrated
while nothing writes to it, and searches fail until it is. Read the whole section
first, and run it on a staging copy of your release before production.

:::caution
The steps were derived from the 2.3.0 chart and code. The in-pod Milvus migration
(step 5) has not yet been rehearsed on a production-sized collection; its duration
grows with the number of chunks.
:::

The commands below use these variables:

```bash
NS=openrag                 # the release namespace
RELEASE=openrag            # the Helm release name
FULLNAME=openrag           # fullnameOverride; `kubectl get deploy -n $NS` shows it as <FULLNAME>-openrag
CHART_VERSION=<2.3.0 chart version, from the release notes>
VALUES=my-values.yaml      # the values file your release uses
ADMIN_TOKEN=<an admin API token, e.g. AUTH_TOKEN>
```

### What changes

| Change | Impact on an existing release | Where it is handled |
|---|---|---|
| Milvus schema version 3: one vector field per embedder | Manual migration, run once. Until it runs, every search answers `503` while the pods report Ready. The old `vector` field is dropped. | [Step 5](#5-migrate-the-milvus-collection) |
| New PostgreSQL migrations | Applied automatically when the new pod starts (or by the pre-upgrade Job, if you enabled it). | [Step 4](#4-upgrade-the-release) |
| Chat completion response: `extra` and its source entries | `extra` is a JSON object instead of a JSON-encoded string, and each document source puts the chunk's metadata under `chunk`. Clients must be updated. | [Before the window](#update-api-clients) |
| `GET /metrics` | The admin token is refused (`403`); scrapers need `METRICS_TOKEN`. | [Before the window](#metrics-scraping) |
| Secret checks | Secrets shorter than 12 characters, or published example values, fail the render or stop the pod at startup. | [Before the window](#render-the-new-chart-offline) |
| Ray | Indexer actors change protocol. A KubeRay cluster keeps its old pods until they are deleted. | [Step 3](#3-plan-the-ray-restart) and [step 4](#4-upgrade-the-release) |
| Probes | Readiness moves from `/health_check` to `/ready`, which exists only in 2.3.0 images, and now also fails while PostgreSQL, Milvus or Ray is unreachable. | Upgrade the chart and the images together |
| vLLM engines | Pinned to `v0.30.0-cu129`; the embedder runs with `runner: pooling` and `--convert embed`. | [Before the window](#vllm-engine-overrides) |
| Logs | The `<FULLNAME>-logs` volume is no longer mounted; logs go to stderr only (JSON). | [Step 7](#7-clean-up) |

### Before the maintenance window

None of this interrupts the running release.

#### Render the new chart offline

```bash
helm template "$RELEASE" oci://ghcr.io/linagora/openrag-stack \
  --version "$CHART_VERSION" -n "$NS" -f "$VALUES" > /dev/null
```

A failure here would also fail `helm upgrade`. The ones an existing release can hit:

- **Secrets.** `AUTH_TOKEN`, `POSTGRES_PASSWORD` (read from
  `postgresql.auth.password` with the bundled PostgreSQL), `CHAINLIT_AUTH_SECRET`,
  `MINIO_SECRET_KEY` and `GRAFANA_ADMIN_PASSWORD` need at least 12 characters, and
  no secret may be a value the project publishes as an example. The list is in
  [Environment variables — What will be refused](/openrag/documentation/env_vars/#what-will-be-refused).
- **`ray.enabled: true` without Ray Serve.** The API must be pointed at the
  cluster with `env.config.RAY_ADDRESS`, even when `env.existingSecret` already
  carries it: the chart cannot read that Secret. The error prints the address.
- **`monitoring.bundled: true`** needs `env.secrets.METRICS_TOKEN`.

If your secrets come from `env.existingSecret` or an external secrets provider, the
chart cannot check them, and the application does instead: a refused value stops
the pod at startup with an error naming the variable. Check those values against
the same rules before the window.

To change the password of the **bundled** PostgreSQL, change the role's password
in the database first. The bundled chart sets it only when it initialises an empty
volume, so changing `postgresql.auth.password` alone leaves OpenRAG unable to log in.

#### Image tags

If your values pin `openrag.image.tag`, `adminUi.image.tag` or `ray.image.tag`, set
all three to `v2.3.0`, or remove them to take the chart's default. The 2.3.0
chart's readiness probe calls `/ready`, which 2.2.x images do not serve: a 2.2.x
image under the new chart never becomes Ready, and the rollout stops.

#### vLLM engine overrides

If your values set `vllm.servingEngineSpec.modelSpec`, that list replaces the
chart's entries entirely, so none of the chart's engine changes reach your
release. In particular an embedder entry copied from an older chart keeps
`tag: latest` and `--task embed`, which current vLLM releases reject. Rebuild your
override from the 2.3.0 chart's `values.yaml`.

The bundled engines run CUDA 12.9 builds: check the NVIDIA driver on your GPU
nodes (see [GPU prerequisites](/openrag/documentation/kubernetes/)). Rolling a
vLLM engine starts its new pod before stopping the old one, so it needs a free GPU
while it rolls.

#### Metrics scraping

`GET /metrics` no longer accepts the admin token. Set `env.secrets.METRICS_TOKEN`
and give the same value to your scraper as a bearer token. See
[Prometheus metrics](/openrag/documentation/prometheus_metrics/).

#### Update API clients

In a chat completion response, `extra` is now a JSON object. Up to 2.2.2 it was a
JSON-encoded string that clients had to parse with `json.loads`. Its keys are
unchanged (`sources`, `presented_sources`, `cited_sources`, `citations_reported`).

Each document entry in those lists now nests the chunk's metadata:

```json
{
  "source_type": "document",
  "chunk": { "filename": "report.pdf", "file_id": "…", "partition": "…", "…": "…" },
  "rerank_score": 0.646,
  "chunk_url": "https://<host>/extract/<chunk id>",
  "file_url": "https://<host>/static/<chunk id>"
}
```

A client that read `sources[i].filename` reads `sources[i].chunk.filename` now.
`rerank_score` is present only when a reranker ran. Web entries
(`source_type: "web"`) are unchanged.

#### Check the default embedder

A PostgreSQL migration pins partitions that follow the `default` embedder to the
endpoint marked as default. It stops, and the new pod reports `503`, if there is
not exactly one. Check before the window:

```bash
kubectl exec -n "$NS" "$FULLNAME-postgresql-0" -- sh -c \
  'PGPASSWORD="$(cat /opt/bitnami/postgresql/secrets/password)" psql -U root -d <database> -c \
   "SELECT name FROM model_endpoints WHERE model_type = '"'"'embedder'"'"' AND is_default"'
```

`root` is the chart's default `postgresql.auth.username`. `<database>` is your
`POSTGRES_DATABASE`, or `partitions_for_collection_<VDB_COLLECTION_NAME>` when it is
unset (`partitions_for_collection_vdb_test` with the defaults). With an
external PostgreSQL, run the same query with your usual client. One row is
expected; otherwise mark one embedder endpoint as the default, in the admin UI or through the admin API, first.

### During the maintenance window

#### 1. Stop traffic and let indexing finish

Stop the clients that upload or chat, then wait until no indexing task is active:

```bash
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  "https://<openrag host>/queue/tasks?task_status=active"
# {"tasks": []}
```

Tasks still running when the pods restart are lost.

#### 2. Back up

Take the backups as one set, after step 1:

- PostgreSQL, for example
  `kubectl exec -n "$NS" "$FULLNAME-postgresql-0" -- sh -c 'PGPASSWORD="$(cat /opt/bitnami/postgresql/secrets/password)" pg_dump -U root -Fc <database>' > openrag.dump`
- Milvus: snapshot the volumes of its etcd and its object storage (MinIO or your
  S3 bucket) with your storage's snapshot mechanism.

This set is your rollback. Step 5 drops the old vector field, and the new
PostgreSQL migrations cannot be undone by an older image.

#### 3. Plan the Ray restart

Which case applies depends on your values:

- **`ray.enabled: false`** (the default): Ray runs inside the OpenRAG pod and
  restarts with it. Nothing to do.
- **`ray.enabled: true`** (a KubeRay cluster, with or without Ray Serve): KubeRay
  does not recreate Ray pods when the chart changes, so they would keep running
  2.2.x. You will delete them in step 4, which restarts the whole Ray cluster and
  removes the old indexer actors with it.
- **An external Ray cluster** you manage yourself: retire the old indexer
  generation before starting 2.3.0, as described in
  [Retire an old indexer actor generation](/openrag/documentation/deploy_ray_cluster/#retire-an-old-indexer-actor-generation).

#### 4. Upgrade the release

Scale OpenRAG to zero first, so that the old pod does not keep writing while the
new one migrates the PostgreSQL schema. Under Ray Serve the API runs on the Ray
pods, which keep running 2.2.x until you delete them below; with traffic stopped
they stay idle.

```bash
kubectl scale -n "$NS" deploy/"$FULLNAME-openrag" --replicas=0
helm upgrade "$RELEASE" oci://ghcr.io/linagora/openrag-stack \
  --version "$CHART_VERSION" -n "$NS" -f "$VALUES"
```

If `kubectl get deploy -n "$NS" "$FULLNAME-openrag"` still shows zero replicas
afterwards, scale it back to your usual count.

With `ray.enabled: true`, once `helm upgrade` has returned, delete every Ray pod
so KubeRay recreates them from the 2.3.0 template (deleting them earlier would
bring them back on 2.2.x), wait for them to be Ready, then restart OpenRAG so it attaches to
the new cluster:

```bash
kubectl delete pod -n "$NS" -l ray.io/cluster="$FULLNAME-raycluster"
kubectl get pod -n "$NS" -l ray.io/cluster="$FULLNAME-raycluster" -w   # until the new pods are Running and Ready
kubectl rollout restart -n "$NS" deploy/"$FULLNAME-openrag"
```

`kubectl get raycluster -n "$NS"` prints the cluster's name if you changed
`fullnameOverride`. A wrong name selects no pods, and Ray stays on 2.2.x.

The new OpenRAG pod applies the PostgreSQL migrations when it starts, unless you
run them through `postgresProvisioning.migrationJob`. It then becomes Ready, but
searches answer `503` with `VDB_SCHEMA_MIGRATION_REQUIRED` and uploads fail until
step 5: readiness does not check the Milvus schema version.

#### 5. Migrate the Milvus collection

Run the migration from the new OpenRAG pod, which has the image, the
configuration and access to both PostgreSQL and Milvus. First a dry run, which
changes nothing and lists the pending migrations:

```bash
kubectl exec -n "$NS" deploy/"$FULLNAME-openrag" -- \
  uv run --no-dev --no-sync python services/persistence/migrations/milvus/migrate.py --dry-run
```

Check that the plan routes every partition to an embedder field, then apply it:

```bash
kubectl exec -n "$NS" deploy/"$FULLNAME-openrag" -- \
  uv run --no-dev --no-sync python services/persistence/migrations/milvus/migrate.py
```

The pod can stay up while this runs: until the collection reaches version 3, the
application cannot index into it. Deleting a file still reaches Milvus, though,
and the migration aborts, without changing anything, if the number of rows moves
while it copies, so keep traffic stopped.
It is safe to run again after a failure. Only its last step, dropping the old
`vector` field, cannot be undone.

What the migration does to the data:

- Each chunk's vector moves to the field of the embedder its partition uses.
  Rows of a partition that PostgreSQL does not know are already unreachable,
  and lose their vector.
- Integers above 2^53 in a chunk's metadata come back rounded. PostgreSQL keeps
  each file's original upload metadata.

Searches and uploads recover on their own once it finishes; no restart is needed.

#### 6. Verify

- `GET /ready` returns `200`, with every check `ok`.
- A search on an existing partition returns results, not `503`.
- A chat completion returns sources in the new shape.
- Uploading a small file completes (`GET /queue/tasks?task_status=active` empties again).
- `GET /metrics` answers with `Authorization: Bearer <METRICS_TOKEN>`.

Then let traffic back in.

#### 7. Clean up

The `<FULLNAME>-logs` volume is no longer mounted. The chart keeps it
(`helm.sh/resource-policy: keep`), so delete it once you have kept what you need
from it:

```bash
kubectl delete pvc -n "$NS" "$FULLNAME-logs"
```

Logs now go to stderr only; see [Logs with Loki](/openrag/documentation/loki_logs/)
to collect them.

### If you installed a development build

A release installed from a `-dev` chart built between 21 and 28 September 2026
may already have migrated its collection to version 3 with a copy that rounded
chunk section IDs, which breaks neighbour-chunk expansion. The migration does not
repair them. After upgrading, re-index the files that were indexed before that
migration. The dry run in step 5 prints the collection's current version
(`Current schema version: 3` for such a release, `2` for a release installed from
chart `0.6.4` or `0.6.5`).

### Rolling back

Restore the backup set from step 2, then roll the release back:

1. Scale OpenRAG to zero.
2. Restore PostgreSQL from the dump, and Milvus's etcd and object storage from their snapshots.
3. `helm rollback "$RELEASE" <previous revision> -n "$NS"`, and with `ray.enabled`,
   delete the Ray pods again so they come back on the old image.

Rolling back the images alone is not enough: a 2.2.x image does not know the
newer PostgreSQL migrations and fails to start against them, and its searches
expect the `vector` field that step 5 dropped.
