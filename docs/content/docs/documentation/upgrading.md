---
title: Upgrading OpenRAG
tableOfContents:
  maxHeadingLevel: 4
---

Each upgrade has a section for what changes in every deployment, followed by the
procedure for Kubernetes and for Docker Compose. Read all of it for the version you
are moving to before you start, and skip none of the versions in between.

## Changes in 2.3.0 for every deployment

These apply to an upgrade from OpenRAG 2.2.1 or 2.2.2, whichever way it is
deployed. The [Kubernetes](#22x-to-230-on-kubernetes) and
[Docker Compose](#22x-to-230-with-docker-compose) procedures below include them.

The upgrade needs a **maintenance window**: the Milvus collection must be migrated
while nothing writes to it, and searches fail until it is.

### What changes

| Change | Impact on an existing deployment |
|---|---|
| Milvus schema version 3: one vector field per embedder | Manual migration, run once. Until it runs, every search answers `503` while OpenRAG reports ready, and uploads fail. The old `vector` field is dropped. |
| New PostgreSQL migrations | Applied automatically when OpenRAG 2.3.0 starts. |
| Chat completion response: `extra` and its source entries | `extra` is a JSON object instead of a JSON-encoded string, and each document source puts the chunk's metadata under `chunk`. Clients must be updated. |
| `GET /metrics` | The admin token is refused (`403`); scrapers need `METRICS_TOKEN`. |
| Secret checks | Secrets shorter than 12 characters, or values the project publishes as examples, stop OpenRAG at startup. |
| Ray | Indexer actors change protocol: tasks running when the old version stops are lost. |
| Readiness | `GET /ready` also fails while PostgreSQL, Milvus or Ray is unreachable. |
| vLLM embedder | Runs on vLLM `v0.30.0` with `--runner pooling --convert embed`. |
| Logs | Written to stderr only; the `logs` volume is no longer mounted. |

### Update API clients

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

### Metrics scraping

`GET /metrics` no longer accepts the admin token. Set `METRICS_TOKEN` and give the
same value to your scraper as a bearer token. See
[Prometheus metrics](/openrag/documentation/prometheus_metrics/).

### Secrets

`AUTH_TOKEN`, `POSTGRES_PASSWORD`, `CHAINLIT_AUTH_SECRET`, `MINIO_SECRET_KEY` and
`GRAFANA_ADMIN_PASSWORD` need at least 12 characters, and no secret may be a value
the project publishes as an example, such as the defaults of the 2.2.x
`.env.example`. OpenRAG refuses to start otherwise, with an error naming the
variable. The rules are in
[Environment variables — What will be refused](/openrag/documentation/env_vars/#what-will-be-refused).

Changing a secret of a deployment that already holds data needs care:

- **PostgreSQL password.** The bundled PostgreSQL sets its password only when it
  initialises an empty data directory. Change the role's password in the database
  first (`ALTER ROLE <user> WITH PASSWORD '…'`), then the configuration; changing
  the configuration alone leaves OpenRAG unable to log in.
- **`AUTH_TOKEN`** is the admin user's token: clients using it need the new value.
- **`CHAINLIT_AUTH_SECRET`**: changing it signs out every chat session.

### Check the default embedder

A PostgreSQL migration pins partitions that follow the `default` embedder to the
endpoint marked as default. It stops, and OpenRAG answers `503`, if there is not
exactly one. Run this query in OpenRAG's database before the window (the
commands for each deployment are below):

```sql
SELECT name FROM model_endpoints WHERE model_type = 'embedder' AND is_default;
```

One row is expected; otherwise mark one embedder endpoint as the default, in the
admin UI or through the admin API, first.

### If you ran a development build

A deployment of the development branch between 21 and 28 September 2026 may
already have migrated its collection to version 3 with a copy that rounded chunk
section IDs, which breaks neighbour-chunk expansion. The migration does not repair
them. After upgrading, re-index the files that were indexed before that migration.
The migration's dry run prints the collection's current version
(`Current schema version: 3` for such a deployment, `2` for 2.2.1 and 2.2.2).

## 2.2.x to 2.3.0 on Kubernetes

This section upgrades a running Helm release of OpenRAG to 2.3.0. It applies to a
release installed from chart `0.6.4` (OpenRAG 2.2.1) or `0.6.5` (OpenRAG 2.2.2),
and to a release installed from a `-dev` chart built from the development branch
before 28 September 2026. Run it on a staging copy of your release before
production.

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

Besides the [changes for every deployment](#changes-in-230-for-every-deployment),
the chart changes:

- **Probes.** Readiness moves from `/health_check` to `/ready`, which only 2.3.0
  images serve: upgrade the chart and the images together.
- **KubeRay.** A KubeRay cluster keeps its old pods until they are deleted.
- **vLLM engines** are pinned to `v0.30.0-cu129`.
- **Logs.** The `<FULLNAME>-logs` volume is no longer mounted.

### Before the maintenance window

None of this interrupts the running release.

#### Render the new chart offline

```bash
helm template "$RELEASE" oci://ghcr.io/linagora/openrag-stack \
  --version "$CHART_VERSION" -n "$NS" -f "$VALUES" > /dev/null
```

A failure here would also fail `helm upgrade`. The ones an existing release can hit:

- **Secrets** that break the [rules above](#secrets). With the bundled
  PostgreSQL, the chart reads `POSTGRES_PASSWORD` from `postgresql.auth.password`.
- **`ray.enabled: true` without Ray Serve.** The API must be pointed at the
  cluster with `env.config.RAY_ADDRESS`, even when `env.existingSecret` already
  carries it: the chart cannot read that Secret. The error prints the address.
- **`monitoring.bundled: true`** needs `env.secrets.METRICS_TOKEN`.

If your secrets come from `env.existingSecret` or an external secrets provider, the
chart cannot check them, and OpenRAG does instead, at startup. Check those values
against the same rules before the window.

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

#### Run the default-embedder check

With the bundled PostgreSQL (`root` is the chart's default
`postgresql.auth.username`):

```bash
kubectl exec -n "$NS" "$FULLNAME-postgresql-0" -- sh -c \
  'PGPASSWORD="$(cat /opt/bitnami/postgresql/secrets/password)" psql -U root -d <database> -c \
   "SELECT name FROM model_endpoints WHERE model_type = '"'"'embedder'"'"' AND is_default"'
```

`<database>` is your `POSTGRES_DATABASE`, or `partitions_for_collection_<VDB_COLLECTION_NAME>`
when it is unset (`partitions_for_collection_vdb_test` with the defaults). With an
external PostgreSQL, run the query with your usual client.

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
bring them back on 2.2.x), wait for them to be Ready, then restart OpenRAG so it
attaches to the new cluster:

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
while it copies, so keep traffic stopped. It is safe to run again after a failure.
Only its last step, dropping the old `vector` field, cannot be undone.

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

### Rolling back a Kubernetes upgrade

Restore the backup set from step 2, then roll the release back:

1. Scale OpenRAG to zero.
2. Restore PostgreSQL from the dump, and Milvus's etcd and object storage from their snapshots.
3. `helm rollback "$RELEASE" <previous revision> -n "$NS"`, and with `ray.enabled`,
   delete the Ray pods again so they come back on the old image.

Rolling back the images alone is not enough: a 2.2.x image does not know the
newer PostgreSQL migrations and fails to start against them, and its searches
expect the `vector` field that step 5 dropped.

## 2.2.x to 2.3.0 with Docker Compose

This section upgrades a deployment started from `infra/compose` in a checkout of
this repository. Run every `docker compose` command from `infra/compose`, with the
same options you use to start the stack (`--profile cpu` and the `openrag-cpu`
service on a CPU host, `-p <project>` if you set a project name, the overlays you
add with `-f`).

Besides the [changes for every deployment](#changes-in-230-for-every-deployment),
the Compose files change:

- **vLLM** moves to `v0.30.0`. The GPU image is a CUDA 13 build, which needs an
  NVIDIA driver 580 or newer on the host. The CPU embedder now uses the official
  `vllm/vllm-openai-cpu` image instead of one built from `extern/vllm`.
- **The default embedder model** is `Qwen/Qwen3-Embedding-0.6B` instead of
  `jinaai/jina-embeddings-v3`.
- **Logs.** The `logs` directory is no longer mounted; `docker compose logs`
  shows them.

### Before the maintenance window

#### Check the GPU driver

```bash
nvidia-smi --query-gpu=driver_version --format=csv,noheader
```

Below 580, update the driver before upgrading: the bundled vLLM containers would
not start.

#### Update `.env`

Your `.env` is not tracked, so checking out 2.3.0 keeps it. Compare it with the
2.3.0 `infra/compose/.env.example` and change:

- **Secrets** that break the [rules above](#secrets). The 2.2.x `.env.example`
  shipped example values for `AUTH_TOKEN`, `POSTGRES_PASSWORD`, `MINIO_ACCESS_KEY`,
  `MINIO_SECRET_KEY` and `CHAINLIT_AUTH_SECRET`; OpenRAG 2.3.0 refuses them.
  - `POSTGRES_PASSWORD`: change the role's password in the database first,
    while 2.2.x still runs:
    `docker compose exec rdb psql -U <POSTGRES_USER, root by default> -d postgres -c "ALTER ROLE <user> WITH PASSWORD '<new password>'"`,
    then put the new value in `.env`.
  - `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY`: MinIO and Milvus both read them
    from `.env`, and MinIO takes the new pair at its next start. Change them in
    `.env` only, and restart both together, which the upgrade does.
- **`EMBEDDER_MODEL_NAME`.** If your `.env` does not set it, the embedder switches
  to `Qwen/Qwen3-Embedding-0.6B`, and the documents you indexed no longer match:
  set it to the model your data was indexed with, `jinaai/jina-embeddings-v3`
  unless you chose another. See the
  [`EMBEDDER_MODEL_NAME` row](/openrag/documentation/env_vars/).
- **`METRICS_TOKEN`**, if you scrape `GET /metrics` or run the monitoring overlay,
  which refuses to start without it.

#### Run the default-embedder check

```bash
docker compose exec rdb psql -U <POSTGRES_USER, root by default> -d <database> -c \
  "SELECT name FROM model_endpoints WHERE model_type = 'embedder' AND is_default"
```

`<database>` is your `POSTGRES_DATABASE`, or `partitions_for_collection_<VDB_COLLECTION_NAME>`
when it is unset (`partitions_for_collection_vdb_test` with the defaults).

### During the maintenance window

#### 1. Let indexing finish

Stop the clients that upload or chat, then wait until no indexing task is active:

```bash
curl -s -H "Authorization: Bearer <AUTH_TOKEN>" \
  "http://localhost:<APP_PORT>/queue/tasks?task_status=active"
# {"tasks": []}
```

Ray runs inside the `openrag` container: tasks still running when it stops are
lost.

#### 2. Stop the stack and back up

```bash
docker compose down
```

`down` keeps the data, which lives in bind-mounted directories. Copy them as one
set, preserving ownership (for example with `sudo cp -a`):

- PostgreSQL: `DB_VOLUME` (`db/` at the repository root by default);
- Milvus, etcd and MinIO: `MILVUS_VOLUME_DIRECTORY` (`infra/compose/volumes/` by default);
- uploaded files: `DATA_VOLUME` (`data/` at the repository root by default).

If you run Milvus with named volumes (`MILVUS_COMPOSE=milvus/milvus.named-volumes.yaml`),
back those volumes up instead. [Backup and restore](/openrag/documentation/backup_restore/)
also exports individual partitions.

#### 3. Check out 2.3.0

```bash
git fetch --tags
git checkout v2.3.0
docker compose pull
```

`pull` fetches the released images. If you build them yourself, add `--build` to
the `up` and `run` commands below instead.

#### 4. Start 2.3.0 once

Start the stack, so that OpenRAG applies the PostgreSQL migrations, which the
Milvus migration needs:

```bash
docker compose up -d
docker compose logs -f openrag   # until the API reports it is serving
```

Searches answer `503` with `VDB_SCHEMA_MIGRATION_REQUIRED` until the next step. Then
stop OpenRAG, leaving PostgreSQL and Milvus running:

```bash
docker compose stop openrag
```

#### 5. Run the Milvus migration

A dry run first, which changes nothing and lists the pending migrations:

```bash
docker compose run --no-deps --rm --entrypoint "" openrag \
  uv run python services/persistence/migrations/milvus/migrate.py --dry-run
```

Check that the plan routes every partition to an embedder field, then apply it:

```bash
docker compose run --no-deps --rm --entrypoint "" openrag \
  uv run python services/persistence/migrations/milvus/migrate.py
```

It is safe to run again after a failure; only its last step, dropping the old
`vector` field, cannot be undone. What it does to the data is described in
[Milvus migrations — Version 3](/openrag/documentation/milvus_migration/#version-3--one-vector-field-per-embedder).

#### 6. Start OpenRAG and verify

```bash
docker compose up -d
```

- `GET /ready` returns `200`, with every check `ok`.
- A search on an existing partition returns results, not `503`.
- A chat completion returns sources in the new shape.
- Uploading a small file completes.
- `GET /metrics` answers with `Authorization: Bearer <METRICS_TOKEN>`.

Then let traffic back in.

#### 7. Remove what is no longer used

- The `logs/` directory at the repository root (`LOG_VOLUME`), once you have kept
  what you need from it.
- On a CPU host, the locally built `openrag-vllm-openai-cpu` image:
  `docker image rm openrag-vllm-openai-cpu`.

### Rolling back a Compose upgrade

1. `docker compose down`.
2. Restore the directories copied in step 2.
3. `git checkout` the version you ran before, restore its `.env` if you changed
   it, and `docker compose up -d`.

Rolling back the checkout alone is not enough: 2.2.x does not know the newer
PostgreSQL migrations and fails to start against them, and its searches expect
the `vector` field that step 5 dropped.
