---
title: Upgrading OpenRAG
tableOfContents:
  maxHeadingLevel: 4
---

Each upgrade has a section for what changes in every deployment, followed by the
procedure for Kubernetes and for Docker Compose. Read all of it for the version you
are moving to before you start. The 2.3.0 sections start from 2.2.1 or 2.2.2; from
an earlier release, upgrade to 2.2.2 first.

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
| Chat and text completion responses: `extra` and its source entries | `extra` is a JSON object instead of a JSON-encoded string, and each document source puts the chunk's metadata under `chunk`. Clients must be updated. |
| `GET /metrics` | The admin token is refused (`403`); scrapers need `METRICS_TOKEN`. |
| Secret checks | Secrets shorter than 12 characters, or values the project publishes as examples, stop OpenRAG at startup. |
| Ray | Indexer actors change protocol: tasks running when the old version stops are lost. |
| Readiness | `GET /ready` is new: it fails while PostgreSQL, Milvus or Ray is unreachable, and reports the model endpoints. |
| vLLM embedder | Runs on vLLM `v0.30.0` with `--runner pooling --convert embed`. |
| Logs | Written to stderr only; the `logs` volume is no longer mounted. |

### Update API clients

In chat and text completion responses, streamed or not, `extra` is now a JSON
object. Up to 2.2.2 it was a JSON-encoded string that clients had to parse with
`json.loads`. Its keys are unchanged; they are listed in
[API — Response: the `extra` field](/openrag/documentation/api/#response-the-extra-field).

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

A PostgreSQL migration pins the partitions that hold files and follow the
`default` embedder to the endpoint marked as default. If such partitions exist and
there is not exactly one default embedder, it stops: OpenRAG answers `503`, or,
with the Helm migration Job, `helm upgrade` fails. Run this query in OpenRAG's database before the window (the
commands for each deployment are below):

```sql
SELECT name FROM model_endpoints WHERE model_type = 'embedder' AND is_default;
```

One row is expected; otherwise mark one embedder endpoint as the default, in the
admin UI or through the admin API, first. The pinning is kept if you roll back.

### If you ran a development build

A collection migrated to version 3 by a build of the development branch from 22
to 28 September 2026 went through a copy that rounded chunk section IDs, which
breaks neighbour-chunk expansion. The migration does not repair them: after
upgrading, re-index the files that were indexed before that migration. The
migration's dry run prints the collection's current version, `2` for 2.2.1 and
2.2.2; a `3` means a development build already migrated it, and whether that copy
rounded the IDs depends on the date it ran.

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
PG_POD=openrag-postgresql-0   # the bundled PostgreSQL pod: <postgresql.fullnameOverride>-0
```

Besides the [changes for every deployment](#changes-in-230-for-every-deployment),
the chart changes:

- **Probes.** Readiness moves from `/health_check` to `/ready`, which only 2.3.0
  images serve: upgrade the chart and the images together.
- **KubeRay.** A KubeRay cluster keeps its old pods until they are deleted.
- **vLLM engines.** The embedder and LLM engines are pinned to `v0.30.0-cu129`.
- **Logs.** The `<FULLNAME>-logs` volume is no longer mounted.

### Before the maintenance window

None of this interrupts the running release.

#### Render the new chart offline

```bash
helm template "$RELEASE" oci://ghcr.io/linagora/openrag-stack \
  --version "$CHART_VERSION" -n "$NS" -f "$VALUES" > /dev/null
```

Pass the same `--set` flags or secret values your install used: `-f "$VALUES"`
alone does not carry a `--set postgresql.auth.password=…` given at install time,
and the render then fails on the missing secret. The same applies to the
`helm upgrade` in step 4. A failure here would also fail `helm upgrade`. The ones
an existing release can hit:

- **Secrets** that break the [rules above](#secrets). With the bundled
  PostgreSQL, the chart reads `POSTGRES_PASSWORD` from `postgresql.auth.password`.
- **`ray.enabled: true` without Ray Serve.** The API must be pointed at the
  cluster with `env.config.RAY_ADDRESS`, even when `env.existingSecret` already
  carries it: the chart cannot read that Secret. The error prints the address.
- **`monitoring.bundled: true`** needs `env.secrets.METRICS_TOKEN` when the chart
  renders the Secret itself; with `env.existingSecret` or an external provider,
  that Secret must carry it.

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
kubectl exec -n "$NS" "$PG_POD" -- sh -c \
  'PGPASSWORD="$(cat /opt/bitnami/postgresql/secrets/password)" psql -U root -d <database> -c \
   "SELECT name FROM model_endpoints WHERE model_type = '"'"'embedder'"'"' AND is_default"'
```

`<database>` is your `POSTGRES_DATABASE`, or `partitions_for_collection_<VDB_COLLECTION_NAME>`
when it is unset (`partitions_for_collection_vdb_test` with the defaults). The
password file is the bundled chart's default; with `postgresql.auth.existingSecret`
and a custom key, adjust its name. With an external PostgreSQL, run the query
with your usual client.

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
  `kubectl exec -n "$NS" "$PG_POD" -- sh -c 'PGPASSWORD="$(cat /opt/bitnami/postgresql/secrets/password)" pg_dump -U root -Fc <database>' > openrag.dump`
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

The new OpenRAG pod applies the PostgreSQL migrations when it starts. With
`postgresProvisioning.runMigrationsInApp: false`, the `pre-upgrade` migration Job
(`postgresProvisioning.migrationJob`) runs them before the pods roll instead, and
a failing migration fails `helm upgrade`. It then becomes Ready, but
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
so keep traffic stopped: after copying, the migration counts the rows again and,
if the count moved, stops before dropping the old `vector` field. A failed run can
be run again. Until that last step, the original vectors stay in place, but the
copy has already rewritten each chunk's metadata, as described below.

What the migration does to the data:

- Each chunk's vector moves to the field of the embedder its partition uses.
  Rows of a partition that PostgreSQL does not know are already unreachable,
  and lose their vector.
- Integers above 2^53 in a chunk's metadata come back rounded, except section
  IDs, which the copy folds below 2^53 so that each chunk stays linked to its
  neighbours. PostgreSQL keeps each file's original upload metadata.

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

Rolling back the images alone is not enough: 2.2.x starts, but every call fails,
because it does not know the newer PostgreSQL schema and its searches expect the
`vector` field that step 5 dropped. Restore the backup set from step 2 instead,
which is also the only way to recover the chunk metadata step 5 rewrote (rounded
integers, folded section IDs):

1. Scale OpenRAG to zero. With `ray.enabled: true`, also delete the Ray cluster,
   whose pods mount the same Python-packages volume as OpenRAG:
   `kubectl delete raycluster -n "$NS" "$FULLNAME-raycluster"` (the rollback
   recreates it).
2. Restore PostgreSQL from the dump, and Milvus's etcd and object storage from their snapshots.
3. Delete the `<FULLNAME>-venv` PVC, which holds the Python packages 2.3.0
   installed, and wait until `kubectl get pvc -n "$NS"` no longer lists it. The
   rollback recreates it empty, and 2.2.x installs its own at startup.
4. `helm rollback "$RELEASE" <previous revision> -n "$NS"`.

## 2.2.x to 2.3.0 with Docker Compose

This section upgrades a deployment started from `infra/compose` in a checkout of
this repository. Run the commands from `infra/compose`, after setting these two
variables to match how you start the stack:

```bash
# bash; in zsh, run `setopt sh_word_split` first so that $DC splits into words
DC="docker compose"   # add -p <project>, your -f overlays, and --profile cpu on a CPU host
SVC=openrag           # openrag-cpu on a CPU host
```

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

This applies to GPU hosts that run the bundled vLLM. Below 580, update the driver
before upgrading: the bundled vLLM containers would not start.

#### Update `.env`

Your `.env` is not tracked, so checking out 2.3.0 keeps it. Copy it aside first
(`cp .env .env.2.2.x`), which the rollback needs, then compare it with the 2.3.0
`infra/compose/.env.example` and change:

- **Secrets** that break the [rules above](#secrets). The 2.2.x `.env.example`
  shipped example values for `AUTH_TOKEN`, `POSTGRES_PASSWORD`, `MINIO_ACCESS_KEY`,
  `MINIO_SECRET_KEY` and `CHAINLIT_AUTH_SECRET`; OpenRAG 2.3.0 refuses them.
  - `POSTGRES_PASSWORD`: change the role's password in the database first,
    while 2.2.x still runs:
    `$DC exec rdb psql -U <POSTGRES_USER, root by default> -d postgres -c "ALTER ROLE <user> WITH PASSWORD '<new password>'"`,
    then put the new value in `.env`.
  - `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY`: MinIO and Milvus both read them
    from `.env`, and MinIO takes the new pair at its next start. Change them in
    `.env` only, and restart both together, which the upgrade does.
- **`EMBEDDER_MODEL_NAME`.** The bundled vLLM reads only this variable, and its
  default is now `Qwen/Qwen3-Embedding-0.6B`. If your `.env` does not set it, set
  it to the model your data was indexed with, `jinaai/jina-embeddings-v3` unless
  you chose another; if you also set the legacy `EMBEDDING_MODEL`, give both the
  same value. Otherwise vLLM serves Qwen while OpenRAG's saved endpoint still asks
  for the model you indexed with: every embedding call fails, and `/ready` reports
  `checks.embedder: unavailable`. See the
  [`EMBEDDER_MODEL_NAME` row](/openrag/documentation/env_vars/).
- **`METRICS_TOKEN`**, if you scrape `GET /metrics` or run the monitoring overlay,
  which refuses to start without it.

#### Run the default-embedder check

```bash
$DC exec rdb psql -U <POSTGRES_USER, root by default> -d <database> -c \
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
$DC down
```

`down` keeps the data, which lives in bind-mounted directories. Copy them as one
set, preserving ownership (for example with `sudo cp -a`):

- PostgreSQL: `DB_VOLUME` (`db/` at the repository root by default);
- Milvus, etcd and MinIO: `MILVUS_VOLUME_DIRECTORY` (`infra/compose/milvus/volumes/`
  by default; a relative value is resolved from `infra/compose/milvus/`);
- uploaded files: `DATA_VOLUME` (`data/` at the repository root by default).

If you run Milvus with named volumes (`MILVUS_COMPOSE=milvus/milvus.named-volumes.yaml`),
back those volumes up instead. [Backup and restore](/openrag/documentation/backup_restore/)
also exports individual partitions.

#### 3. Check out 2.3.0

```bash
git fetch --tags
git checkout v2.3.0
$DC pull
```

`pull` fetches the released images. If you build them yourself, add `--build` to
the `up` and `run` commands below instead.

#### 4. Start 2.3.0 once

Start the stack, so that OpenRAG applies the PostgreSQL migrations, which the
Milvus migration needs:

```bash
$DC up -d
$DC logs -f "$SVC"   # until the API reports it is serving
```

Searches answer `503` with `VDB_SCHEMA_MIGRATION_REQUIRED` until the next step. Then
stop OpenRAG, leaving PostgreSQL and Milvus running:

```bash
$DC stop "$SVC"
```

#### 5. Run the Milvus migration

A dry run first, which changes nothing and lists the pending migrations:

```bash
$DC run --no-deps --rm --entrypoint "" "$SVC" \
  uv run --no-dev python services/persistence/migrations/milvus/migrate.py --dry-run
```

Check that the plan routes every partition to an embedder field, then apply it:

```bash
$DC run --no-deps --rm --entrypoint "" "$SVC" \
  uv run --no-dev python services/persistence/migrations/milvus/migrate.py
```

A failed run can be run again: until its last step drops the old `vector` field,
the original vectors stay in place. What it does to the data is described in
[Milvus migrations — Version 3](/openrag/documentation/milvus_migration/#version-3--one-vector-field-per-embedder).

#### 6. Start OpenRAG and verify

```bash
$DC up -d
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
- On a CPU host, once you no longer need to roll back, the locally built
  `openrag-vllm-openai-cpu` image: `docker image rm openrag-vllm-openai-cpu`. A
  rollback to 2.2.x would otherwise rebuild it from `extern/vllm`.

### Rolling back a Compose upgrade

Rolling back the checkout alone is not enough: 2.2.x starts, but every call fails,
because it does not know the newer PostgreSQL schema and its searches expect the
`vector` field that step 5 dropped. Either way below also removes the
`openrag_venv` volume, which holds the Python packages 2.3.0 installed; 2.2.x
installs its own at startup. Its name is `<project>_openrag_venv`, where the
project is `compose` unless you set one (`docker volume ls` lists it).

**From the backups of step 2:**

1. `$DC down`.
2. Restore the directories copied in step 2, and remove the `openrag_venv` volume.
3. `git checkout` the version you ran before, restore the `.env` you copied aside,
   and `$DC up -d`.

**Without the backups**, by undoing the migrations with 2.3.0 still checked out.
This way works only when `POSTGRES_DATABASE` is unset: the Alembic command always
targets `partitions_for_collection_<VDB_COLLECTION_NAME>`. It also needs every
check below to pass **before** you start, since a refusal halfway leaves Milvus
back at version 2 and PostgreSQL still at 2.3.0, which neither version runs on:

- no workspace ID shared by two partitions, which 2.3.0 allows and the PostgreSQL
  downgrade refuses:
  `$DC exec rdb psql -U <POSTGRES_USER> -d <database> -c "SELECT workspace_id FROM workspaces GROUP BY workspace_id HAVING COUNT(*) > 1"`
  returns no row;
- every embedder field in use has the same dimension;
- every partition holding files still resolves, through PostgreSQL, to an
  embedder field: no embedder deleted and no default changed since the upgrade;
- the collection has fewer than 10 vector fields, since the downgrade adds
  `vector` back.

Then, in this order (the Milvus downgrade reads the PostgreSQL schema that the
second command removes):

```bash
$DC stop "$SVC"
$DC run --no-deps --rm --entrypoint "" "$SVC" \
  uv run --no-dev python services/persistence/migrations/milvus/migrate.py --downgrade --target 2
$DC run --no-deps --rm --entrypoint "" "$SVC" \
  uv run --no-dev alembic -c /app/openrag/services/persistence/migrations/alembic/alembic.ini downgrade b9c0d1e2f3a4
$DC down
docker volume rm <project>_openrag_venv
```

Then `git checkout` the version you ran before, restore the `.env` you copied
aside, and `$DC up -d`. `b9c0d1e2f3a4` is the last PostgreSQL revision of 2.2.1 and
2.2.2. Files indexed after the upgrade are kept. The chunk metadata the upgrade
rewrote is not restored: integers above 2^53 stay rounded and section IDs stay
folded. To recover it, roll back from the backups instead.
