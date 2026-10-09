---
title: Upgrading OpenRAG
tableOfContents:
  maxHeadingLevel: 4
---

Each upgrade has a section for what changes in every deployment, followed by the
procedure for Kubernetes and for Docker Compose. Read all of it for the version you
are moving to before you start. The 2.3.0 instructions assume you run 2.2.1 or
2.2.2; on an earlier release, upgrade to 2.2.2 first.

The commands are written for bash. In zsh, run `setopt interactive_comments sh_word_split`
first. Without it, zsh does not treat `#` as the start of a comment and does not
split `$DC` into `docker compose`, so the commands below fail.

## Changes in 2.3.0 for every deployment

This section applies to every upgrade from OpenRAG 2.2.1 or 2.2.2, on Kubernetes
or with Docker Compose. The [Kubernetes](#22x-to-230-on-kubernetes) and
[Docker Compose](#22x-to-230-with-docker-compose) procedures below include its
steps, except the changes on the client side: API clients and metrics scrapers.

The upgrade needs a **maintenance window**: the Milvus collection must be migrated
while nothing writes to it, and searches and uploads fail until the migration
finishes. Each procedure has three parts:

1. **Before the maintenance window**, with no downtime: check the new chart or
   `.env`, the secrets and the default embedder.
2. **During the maintenance window**: stop traffic, back up, upgrade, migrate the
   Milvus collection, verify.
3. **Rolling back**, if something goes wrong: restore the backups. Compose can
   also undo the migrations without them, under conditions.

### What changes

| Change | Impact on an existing deployment |
|---|---|
| Milvus schema version 3: one vector field per embedder | Manual migration, run once. Until it runs, every search answers `503` while OpenRAG reports ready, and uploads fail. The old `vector` field is dropped. |
| New PostgreSQL migrations | Applied when OpenRAG 2.3.0 starts, or on Kubernetes by the migration Job if you enabled it. |
| Chat and text completion responses: `extra` and its source entries | `extra` is a JSON object instead of a JSON-encoded string, and each document source puts the chunk's metadata under `chunk`. Clients must be updated. |
| `GET /metrics` | The admin token is refused (`403`); scrapers need `METRICS_TOKEN`, unless unauthenticated scrapes are enabled (see [Metrics scraping](#metrics-scraping)). |
| Secret checks | OpenRAG refuses to start if a secret is too short or still set to a published example value; see [Secrets](#secrets). |
| Ray | Indexer actors change protocol: tasks running when the old version stops are lost. |
| Readiness | `GET /ready` is new: it fails while PostgreSQL, Milvus or Ray is unreachable, and reports the model endpoints. |
| vLLM embedder | Runs on vLLM `v0.30.0` with `--runner pooling --convert embed`. |
| Logs | Written to stderr only; the `logs` volume is no longer mounted. |

### Update API clients

In chat completion responses, streamed or not, and in text completion responses,
`extra` is now a JSON object. Up to 2.2.2 it was a JSON-encoded string that clients had to parse with
`json.loads`; clients must stop doing so. Its keys are unchanged; they are listed in
[API — Response: the `extra` field](/openrag/documentation/api/#response-the-extra-field).

Each document entry in the source lists of `extra` (`sources`, `presented_sources`,
`cited_sources`, …) now nests the chunk's metadata under `chunk`:

```json
{
  "source_type": "document",
  "chunk": { "filename": "report.pdf", "file_id": "…", "partition": "…", "…": "…" },
  "rerank_score": 0.646,
  "chunk_url": "https://<host>/extract/<chunk id>",
  "file_url": "https://<host>/static/<chunk id>"
}
```

A client that read `sources[i].filename` must read `sources[i].chunk.filename` now.
`rerank_score`, new in 2.3.0, is present only when a reranker ran. Web entries
(`source_type: "web"`) are unchanged.

Other requests that 2.2.x accepted, or answered differently:

- **`Content-Type`.** A JSON body sent without `Content-Type: application/json`
  is answered `422`.
- **Uploads.** A file whose content does not match its extension (PDF, images,
  DOCX, PPTX, `.doc`) is refused with `415`. A second `POST` of a file that is
  still indexing is refused with `409 DOCUMENT_INDEXING_IN_PROGRESS`, which gives
  the running task's status URL; `PUT` is unchanged. A document that produces no
  chunks ends its task `FAILED` (`NO_INDEXABLE_CONTENT`), and its callback
  reports `"error"`, instead of being reported as indexed.
- **Workspaces.** Workspace IDs are unique per partition, so two partitions can
  use the same ID. A search over several partitions that finds the ID in more
  than one answers `422 WORKSPACE_AMBIGUOUS`. Deleting a workspace no longer
  deletes the files indexed before the upgrade; a file uploaded with
  `workspace_ids` from 2.3.0 on is deleted with its last workspace.
- **Error statuses.** A model provider that answers `401` or `403` is reported
  as `502` (`400` when the request overrode the endpoint with its own key). Milvus
  errors return their own status and `VDB_*` code instead of `500`, and reranker
  errors keep the reranker's status instead of `503`.
- **Task logs.** `GET /indexer/task/{task_id}/logs` is removed, and so is the MCP
  `get_task_logs` tool. `GET /indexer/task/{task_id}/error` still returns a failed
  task's error.
- **Embedders (admin API).** A partition's `embedder` must name a registered
  endpoint (`422`). Changing it once the partition holds files is refused
  (`409 PARTITION_HAS_INDEXED_FILES`). Editing the URL or model of an embedder
  endpoint that has indexed files needs `acknowledge_indexed_data: true`
  (`409` otherwise), and deleting an embedder that a partition uses is refused
  (`409`).

### Metrics scraping

`GET /metrics` no longer accepts the admin token. Either:

- set `METRICS_TOKEN`, and have your scraper send it as a bearer token; or
- on a network only your scraper reaches, leave `METRICS_TOKEN` unset and set
  `METRICS_ALLOW_UNAUTHENTICATED=true`.

With neither, every scrape gets `403`. See
[Prometheus metrics](/openrag/documentation/prometheus_metrics/).

### Secrets

OpenRAG 2.3.0 refuses to start, with an error naming the variable, when:

- `AUTH_TOKEN`, `POSTGRES_PASSWORD`, `CHAINLIT_AUTH_SECRET`, `MINIO_SECRET_KEY` or
  `GRAFANA_ADMIN_PASSWORD` is shorter than 12 characters;
- a secret is set to a value the project publishes as an example, such as the
  defaults of the 2.2.x `.env.example`.

The rules are in
[Environment variables — What will be refused](/openrag/documentation/env_vars/#what-will-be-refused).

Changing a secret of a deployment that already holds data needs care:

- **PostgreSQL password.** If your current password passes the rules, keep it.
  The bundled PostgreSQL reads the password from your configuration only once,
  when it creates the database. To change it afterwards, you must change it in
  both the configuration and the database (`ALTER ROLE`). The procedures below
  show when. Do not change it in the database before the window: 2.2.x could no
  longer connect. Pick a value without quotes or `$`, such as the output of
  `openssl rand -hex 16`, because the commands below wrap it in single quotes.
- **`AUTH_TOKEN`** is the admin user's token: clients using it need the new value.
- **`CHAINLIT_AUTH_SECRET`**: changing it signs out every chat session.
- **`GRAFANA_ADMIN_PASSWORD`** (Compose monitoring overlay): like PostgreSQL,
  Grafana sets the admin password only when it creates its database. Change it
  in Grafana too, once the overlay runs, with `$DC` set as in the
  [Compose procedure](#22x-to-230-with-docker-compose):
  `$DC exec grafana grafana cli admin reset-admin-password '<new password>'`.
  Grafana keeps it through a rollback: set the old one back the same way.
- **Milvus's object storage keys** (`MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY`
  with Compose). Milvus v3.0.1, which 2.2.1 and 2.2.2 run, writes both to its log
  on every compaction attempt. Milvus v3.0.2, which 2.3.0 ships, no longer does,
  but the lines already written stay. If you keep Milvus logs or ship them to a log system,
  change the keys during the upgrade. MinIO and Milvus must restart together with
  the new pair; with Compose, [Update `.env`](#update-env) says how.

### Check the default embedder

When 2.3.0 migrates the database, partitions that hold files and use the
`default` embedder are tied, for good, to the embedder endpoint marked as
default. If such partitions exist and there is not exactly one default
embedder, this migration stops: OpenRAG answers `503`, or `helm upgrade` fails
if the Helm migration Job runs the migrations.

Before the window, run this query in OpenRAG's database (the commands for each
deployment are below):

```sql
SELECT name FROM model_endpoints WHERE model_type = 'embedder' AND is_default;
```

It should return one row. If not, mark one embedder endpoint as the default
before upgrading, in the admin UI or through the admin API. Choose the embedder
those partitions were indexed with: the choice is permanent, and the Milvus
migration moves their vectors into that embedder's field.

If 2.3.0 already stopped on this migration, its admin UI is down. Set the
default in the database instead, then check that the query above returns one
row:

```sql
UPDATE model_endpoints SET is_default = (name = '<endpoint name>') WHERE model_type = 'embedder';
```

Then apply the migrations again:

- With Docker Compose, or on Kubernetes when OpenRAG applies them itself (the
  default): restart OpenRAG.
- On Kubernetes with the migration Job: run `helm upgrade` again.
- On Kubernetes with both off: run them by hand as in
  [Kubernetes step 4](#4-upgrade-the-release), then restart OpenRAG.

Rolling back from the backups undoes this choice; the Compose rollback without
backups does not.

### If you ran a development build

If that build ran the version 3 Milvus migration on Milvus v3.0.1, upgrade
Milvus to v3.0.2 before continuing. The old Milvus version cannot compact the
segments created by that migration; restarting v3.0.1 or rerunning the
migration will not fix it. The 2.3.0 Compose files and Helm chart use v3.0.2.

If a development build from 22 to 28 September 2026 migrated your collection to
version 3, that migration rounded chunk section IDs, which breaks neighbour-chunk
expansion. The 2.3.0 migration does not repair them: after upgrading, re-index
the files that were indexed before that migration. A Milvus
dry run that prints version `3` means a development build already migrated the
collection; whether that copy rounded the IDs depends on the date it ran.

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
PG_USER=root               # postgresql.auth.username
DB=partitions_for_collection_vdb_test   # POSTGRES_DATABASE; when unset, partitions_for_collection_<VDB_COLLECTION_NAME>
```

Besides the [changes for every deployment](#changes-in-230-for-every-deployment),
the chart changes:

- **Probes.** Readiness moves from `/health_check` to `/ready`, which only 2.3.0
  images serve: upgrade the chart and the images together. Values that set
  `openrag.probes` keep their own paths.
- **KubeRay.** A KubeRay cluster keeps its old pods until they are deleted.
- **vLLM engines.** The embedder and LLM engines are pinned to `v0.30.0-cu129`.
- **Embedder engine.** It now runs with `maxModelLen: 2048` and
  `gpuMemoryUtilization: 0.1`, instead of the model's own maximum length and 0.3.
  If you raised `MAX_MODEL_LEN` or an endpoint's `extra.max_model_len` above
  2048, raise `maxModelLen` with it. Otherwise vLLM refuses the longer requests,
  and OpenRAG logs a warning and falls back to the length vLLM serves: it embeds
  shorter inputs and makes new chunks smaller.
- **Bundled PostgreSQL.** Its own network policy, which let any source reach port
  5432, is off. With `networkPolicy.enabled` (the default), PostgreSQL gets the
  chart's default-deny rules, and clients outside the release's namespace no
  longer reach it.
- **Metrics.** `openrag.metrics.prometheusAnnotations` is on by default. A
  Prometheus that discovers pods by annotation starts scraping `GET /metrics`, and
  gets `403` until its scrape job sends `METRICS_TOKEN`.
- **Logs.** The `<FULLNAME>-logs` volume is no longer mounted, and `LOG_FORMAT`
  defaults to `json`; set `env.config.LOG_FORMAT: text` for the previous format.

### Before the maintenance window

None of this interrupts the running release.

#### Render the new chart offline

This checks your values against the 2.3.0 chart without touching the cluster:
anything that fails here would also fail `helm upgrade`. Pass the same `--set`
flags or secret values your install used, here and in step 4: `-f "$VALUES"`
alone does not carry a `--set postgresql.auth.password=…` given at install time,
and the render then fails on the missing secret.

```bash
helm template "$RELEASE" oci://ghcr.io/linagora/openrag-stack \
  --version "$CHART_VERSION" -n "$NS" -f "$VALUES" > /dev/null
```

The failures an existing release can hit:

- **Secrets** that break the [rules above](#secrets). With the bundled
  PostgreSQL, the chart reads `POSTGRES_PASSWORD` from `postgresql.auth.password`.
  If you change the PostgreSQL password, render with the new value
  (`postgresql.auth.password`, or `env.secrets.POSTGRES_PASSWORD` with an external
  PostgreSQL), and keep the old one: step 4 needs it.
- **`ray.enabled: true` without Ray Serve.** The API must be pointed at the
  cluster with `env.config.RAY_ADDRESS`, even when `env.existingSecret` already
  carries it: the chart cannot read that Secret. The error prints the address.
- **`monitoring.bundled: true`**, new in this chart, needs `env.secrets.METRICS_TOKEN` when the chart
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

If your values pin `milvus.image.all.tag`, set it to `v3.0.2` or remove the
override. Charts 0.6.4 and 0.6.5 pinned it to `v3.0.1`. If you keep that pin, the
migration runs on v3.0.1, which cannot compact the segments the migration
creates.

#### vLLM engine overrides

If your values set `vllm.servingEngineSpec.modelSpec`, that list replaces the
chart's entries entirely, so none of the chart's engine changes reach your
release. In particular an embedder entry copied from an older chart keeps
`tag: latest` and `--task embed`, which current vLLM releases reject. Rebuild your
override from the 2.3.0 chart's `values.yaml`.

The bundled engines run CUDA 12.9 builds: check that the NVIDIA driver on your
GPU nodes supports CUDA 12.9, in NVIDIA's
[CUDA compatibility documentation](https://docs.nvidia.com/deploy/cuda-compatibility/). Rolling a
vLLM engine starts its new pod before stopping the old one, so it needs a free GPU
while it rolls.

#### Run the default-embedder check

With the bundled PostgreSQL (the password file below is the chart's default;
with `postgresql.auth.existingSecret` and a custom key, adjust its name):

```bash
echo "SELECT name FROM model_endpoints WHERE model_type = 'embedder' AND is_default" | \
  kubectl exec -i -n "$NS" "$PG_POD" -- env PGUSER="$PG_USER" PGDATABASE="$DB" \
  sh -c 'PGPASSWORD="$(cat /opt/bitnami/postgresql/secrets/password)" psql'
```

With an external PostgreSQL, run the query with your usual client.

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

These backups are your rollback: step 5 drops the old vector field, and an older
image cannot undo the new PostgreSQL migrations. Take all three now, while
traffic is stopped, so that they match each other:

- **PostgreSQL**, for example:

  ```bash
  kubectl exec -n "$NS" "$PG_POD" -- env PGUSER="$PG_USER" PGDATABASE="$DB" \
    sh -c 'PGPASSWORD="$(cat /opt/bitnami/postgresql/secrets/password)" pg_dump -Fc' > openrag.dump
  ```

  Check that the dump is not empty: this lists its contents.

  ```bash
  kubectl exec -i -n "$NS" "$PG_POD" -- pg_restore -l < openrag.dump | head
  ```

- **Milvus**: its etcd and its object storage (MinIO or your S3 bucket). Stop
  them first, so that the snapshots are one point in time:
  1. Note the replica counts that `kubectl get deploy,statefulset -n "$NS"` shows
     for Milvus, etcd and MinIO.
  2. Scale those to zero.
  3. Snapshot their volumes (or copy your bucket) with your storage's snapshot
     mechanism.
  4. Scale them back to the counts you noted, and wait until their pods are
     Ready.

  OpenRAG 2.2.x keeps running meanwhile; with traffic stopped, it does nothing
  with Milvus.
- **Uploaded files**: snapshot the `<FULLNAME>-data` volume.

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
```

Your values decide what applies the PostgreSQL migrations:

| `postgresProvisioning` | What applies the migrations |
|---|---|
| `migrationJob.enabled: true` | The `pre-upgrade` migration Job, before the pods roll. A failing migration fails `helm upgrade`. |
| `runMigrationsInApp: true` (the default), Job off | The new OpenRAG pod, when it starts. Under Ray Serve, the Serve replicas on the Ray pods, once you scale OpenRAG back up. |
| Both off | Nothing: run them by hand after `helm upgrade`, as shown below. |

If you change the PostgreSQL password (see [Secrets](#secrets)), do it now, before
`helm upgrade`:

1. Change it in the database. With the bundled PostgreSQL:

   ```bash
   echo "ALTER ROLE CURRENT_USER WITH PASSWORD '<new password>'" | kubectl exec -i -n "$NS" "$PG_POD" -- \
     env PGPASSWORD='<old password>' psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d postgres
   ```

   With an external PostgreSQL, run the same `ALTER ROLE` with your client.
2. If the migration Job is enabled, make sure it logs in with the new password.
   It runs before Helm updates the release's Secret. With `env.existingSecret`,
   it reads that Secret: update it now. With an external secrets provider,
   update the source. With the Secret the chart renders from your values, the
   Job reads a copy rendered from your new values, so there is nothing to do.

   When you upgrade to chart 0.7.1 (OpenRAG 2.3.1) or earlier, there is no copy:
   the Job reads the release's Secret, so update that one now, rather than
   letting `helm upgrade` do it.
   With an external secrets provider, wait until it has synced. For the Secret
   the chart renders from your values:

   ```bash
   kubectl patch secret -n "$NS" "$FULLNAME-env-secrets" \
     -p '{"stringData":{"POSTGRES_PASSWORD":"<new password>"}}'
   ```

   Your values already carry the new value, from
   [Render the new chart offline](#render-the-new-chart-offline); with
   `postgresql.auth.existingSecret`, update that Secret's password too.

Then upgrade the release. With `ray.enabled: false`:

```bash
helm upgrade "$RELEASE" oci://ghcr.io/linagora/openrag-stack \
  --version "$CHART_VERSION" -n "$NS" -f "$VALUES"
```

If `kubectl get deploy -n "$NS" "$FULLNAME-openrag"` still shows zero replicas
afterwards, scale it back to your usual count.

With `ray.enabled: true`, add `--set openrag.replicas=0`, which keeps OpenRAG from
starting against the 2.2.x Ray pods:

```bash
helm upgrade "$RELEASE" oci://ghcr.io/linagora/openrag-stack \
  --version "$CHART_VERSION" -n "$NS" -f "$VALUES" --set openrag.replicas=0
```

Once `helm upgrade` has returned, delete every Ray pod, head included, so KubeRay
recreates them from the 2.3.0 template (deleting them earlier would bring them
back on 2.2.x). The notes `helm upgrade` prints say to recreate only the workers,
for their metrics port; this upgrade needs the head recreated too. Once they are
Ready, scale OpenRAG up:

```bash
kubectl delete pod -n "$NS" -l ray.io/cluster="$FULLNAME-raycluster"
kubectl get pod -n "$NS" -l ray.io/cluster="$FULLNAME-raycluster" -w   # until the new pods are Running and Ready
kubectl scale -n "$NS" deploy/"$FULLNAME-openrag" --replicas=<your usual count>
```

`kubectl get raycluster -n "$NS"` prints the cluster's name if you changed
`fullnameOverride`. A wrong name selects no pods, and Ray stays on 2.2.x. A later
`helm upgrade` without `--set openrag.replicas=0` sets the count from your values
again.

If `helm upgrade` fails after you changed the password, do not run a bare
`helm rollback`: it puts the old password back in the Secret the chart renders
from your values, and the database no longer accepts it. Fix the cause and run
`helm upgrade` again, or follow
[Rolling back a Kubernetes upgrade](#rolling-back-a-kubernetes-upgrade).

With both `runMigrationsInApp` and `migrationJob.enabled` off, the new pod starts
without its services and stays unready until the migrations are applied and it
restarts. Run them from it, then restart it:

```bash
kubectl exec -n "$NS" deploy/"$FULLNAME-openrag" -- \
  uv run --no-dev --no-sync python -m services.persistence.migrations.run
kubectl rollout restart -n "$NS" deploy/"$FULLNAME-openrag"
```

Once the PostgreSQL migrations are applied, the pod becomes Ready, but searches
answer `503` with `VDB_SCHEMA_MIGRATION_REQUIRED` and uploads fail until step 5:
readiness does not check the Milvus schema version.

#### 5. Migrate the Milvus collection

Run the migration from a new OpenRAG pod, which has the image, the
configuration and access to both PostgreSQL and Milvus. Pick one pod and use it
for every command of this step: with several replicas, `kubectl exec deploy/…`
can land on a different pod each time.

```bash
POD=$(kubectl get pod -n "$NS" -l app.kubernetes.io/name=openrag,app.kubernetes.io/instance="$FULLNAME" -o name | head -1)
```

First a dry run, which changes nothing and lists the pending migrations:

```bash
kubectl exec -n "$NS" "$POD" -- \
  uv run --no-dev --no-sync python services/persistence/migrations/milvus/migrate.py --dry-run
```

The dry run prints the collection's current version:

- `2`, normally, for 2.2.1 and 2.2.2.
- `1` means version 2 was never applied (2.2.x only enforced it on uploads), and
  `0` that no migration was: the upgrade then runs the missing versions too.
  Version 2 rebuilds the collection and keeps a backup copy of it, unless
  `hybrid_search` is off; see
  [Milvus migrations — Version 2](/openrag/documentation/milvus_migration/#version-2--case-insensitive-bm25-analyzer).
- `3` means the collection is already migrated, by an earlier run of this step
  (the dry run then says it is already up to date) or by a development build;
  for the latter, see [If you ran a development build](#if-you-ran-a-development-build).

To size the maintenance window, take the number of rows from the dry run's
``Splitting `vector` of '<collection>' (N rows, dim=…)`` line. The tested
collection migrated 13,805 rows in 25 seconds. Assuming the duration grows
linearly with the number of rows:

```text
duration (seconds) ≈ N × 25 / 13,805 ≈ N / 550
```

That is about 5 minutes for 150,000 rows and 30 minutes for 1,000,000. Treat it
as an order of magnitude: it comes from a single run, and the hardware Milvus
runs on and the vector dimension change it.

Check that the plan routes every partition to an embedder field, and review any
`row(s) belong to partitions that do not exist in Postgres` warning: those rows
lose their vector. Then apply it:

```bash
kubectl exec -n "$NS" "$POD" -- \
  uv run --no-dev --no-sync python services/persistence/migrations/milvus/migrate.py
```

While it runs:

- **The pod can stay up**: until the collection reaches version 3, OpenRAG cannot
  index into it.
- **Keep traffic stopped**: deleting a file still reaches Milvus. If the row
  count changes during the copy, the migration stops before dropping the old
  `vector` field.
- **If it fails, run it again.** The original vectors stay in place until the
  last step, but each chunk's metadata is already rewritten, as described below.
- **If your `kubectl exec` session drops** (an idle timeout on the API server or
  on a load balancer in between), the migration may still be running in the pod,
  and nothing stops a second run from overlapping it. Check first, in the same
  pod; no output means it has stopped:

  ```bash
  kubectl exec -n "$NS" "$POD" -- pgrep -af migrate.py
  ```

  Then run the dry run again. If it says the collection is already up to date,
  the migration finished; otherwise, run the migration again.

What the migration does to the data:

- Each chunk's vector moves to the field of the embedder its partition uses.
  Rows of a partition that PostgreSQL does not know are already unreachable,
  and lose their vector.
- Integers above 2^53 in a chunk's metadata come back rounded, except section
  IDs, which the copy folds below 2^53 so that each chunk stays linked to its
  neighbours. PostgreSQL keeps each file's original upload metadata.

Details are in
[Milvus migrations — Version 3](/openrag/documentation/milvus_migration/#version-3--one-vector-field-per-embedder).
Searches and uploads recover on their own once it finishes; no restart is needed.

#### 6. Verify

- `GET /ready` returns `200` with `"status": "ready"`, and `checks.embedder` is
  `ok`: the `200` alone does not cover the embedder unless
  `READINESS_REQUIRE_EMBEDDER` is on. The `llm` and `reranker` checks do not
  block readiness: `unavailable` or `timeout` means that endpoint is
  unreachable, `unresolvable` that it is not configured.
- A search on an existing partition returns results, not `503`.
- A chat completion returns sources in the new shape.
- Uploading a small file completes (`GET /queue/tasks?task_status=active` empties again).
- `GET /metrics` answers with `Authorization: Bearer <METRICS_TOKEN>`.

In Attu, the index on a new `vector_<embedder>` field can stay `InProgress`
indefinitely. This is expected: the migration rewrites every row, and Milvus
keeps the old 2.2.x copies without indexing them, so they stay pending. Look at
`indexed_rows` instead, which `describe_index` reports. The index is complete
once it reaches the number of rows the migration copied into that field; the
migration logs both numbers (`N row(s) indexed, M copied`). For example, the
tested collection reported 13,805 indexed rows, with `total_rows` 27,610 and
13,805 pending: complete.
The old copies also take disk and memory until you
[compact the collection](#after-the-upgrade-compact-the-milvus-collection), once
traffic is back.

Then let traffic back in.

#### 7. Clean up

The `<FULLNAME>-logs` volume is no longer mounted. With the default
`persistence.annotations`, the chart keeps it (`helm.sh/resource-policy: keep`),
so delete it once you have kept what you need from it:

```bash
kubectl delete pvc -n "$NS" "$FULLNAME-logs"
```

Logs now go to stderr only; see [Logs with Loki](/openrag/documentation/loki_logs/)
to collect them.

Then [compact the Milvus collection](#after-the-upgrade-compact-the-milvus-collection).

### Rolling back a Kubernetes upgrade

Rolling back the images alone is not enough: 2.2.x starts, but every call fails,
because it does not know the newer PostgreSQL schema and its searches expect the
`vector` field that step 5 dropped. Restore the backup set from step 2 instead,
which is also the only way to recover the chunk metadata step 5 rewrote (rounded
integers, folded section IDs):

1. Scale OpenRAG to zero. With `ray.enabled: true`, also delete the Ray cluster,
   whose pods mount the same Python-packages volume as OpenRAG:
   `kubectl delete raycluster -n "$NS" "$FULLNAME-raycluster"` (the rollback
   recreates it). With an external Ray cluster, retire the 2.3.0 indexer
   generation, as in
   [Retire an old indexer actor generation](/openrag/documentation/deploy_ray_cluster/#retire-an-old-indexer-actor-generation).
2. Restore the backup set from step 2:
   - PostgreSQL: empty its schema, then restore the dump. `pg_restore --clean`
     alone fails here: the constraints 2.3.0 added and changed stop the tables in
     the dump from being dropped. With the bundled PostgreSQL, where `<password>`
     is the one the database has now (the new one if you changed it in upgrade
     step 4):

     ```bash
     echo "DROP SCHEMA public CASCADE; CREATE SCHEMA public; GRANT USAGE ON SCHEMA public TO PUBLIC;" | kubectl exec -i -n "$NS" "$PG_POD" -- \
       env PGPASSWORD='<password>' psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d "$DB"
     kubectl exec -i -n "$NS" "$PG_POD" -- \
       env PGPASSWORD='<password>' pg_restore --no-owner --exit-on-error -U "$PG_USER" -d "$DB" < openrag.dump
     ```

     With an external PostgreSQL, do the same with your client, as the database's
     owner.
   - Milvus: scale Milvus, etcd and MinIO to zero, restore their volumes (or your
     bucket) from the snapshots, then scale them back.
   - Uploaded files: restore the `<FULLNAME>-data` volume.
3. If you changed the PostgreSQL password in [upgrade step 4](#4-upgrade-the-release),
   set the old one back: a `pg_dump` of one database holds no role passwords, so
   the restore keeps the new one. With the bundled PostgreSQL:

   ```bash
   echo "ALTER ROLE CURRENT_USER WITH PASSWORD '<old password>'" | kubectl exec -i -n "$NS" "$PG_POD" -- \
     env PGPASSWORD='<new password>' psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d postgres
   ```

   With an external PostgreSQL, use your client. `helm rollback` puts the old
   value back in the Secret the chart renders from your values; in an
   `env.existingSecret`, a `postgresql.auth.existingSecret` or at your secrets
   provider, put it back yourself.
4. Delete the `<FULLNAME>-venv` PVC, which holds the Python packages 2.3.0
   installed, and wait until `kubectl get pvc -n "$NS"` no longer lists it. The
   rollback recreates it empty, and 2.2.x installs its own at startup.
5. `helm rollback "$RELEASE" <previous revision> -n "$NS"`. `helm history "$RELEASE" -n "$NS"`
   lists the revisions: roll back to the last one before the 2.3.0 upgrade.

## 2.2.x to 2.3.0 with Docker Compose

This section upgrades a deployment started from `infra/compose` in a checkout of
this repository. Run the commands from `infra/compose`, after setting these
variables to match how you start the stack:

```bash
# bash; in zsh, see the note at the top of this page
DC="docker compose"   # add -p <project>, your -f overlays, and --profile cpu on a CPU host
SVC=openrag           # openrag-cpu on a CPU host
PG_USER=root          # POSTGRES_USER
DB=partitions_for_collection_vdb_test   # POSTGRES_DATABASE; when unset, partitions_for_collection_<VDB_COLLECTION_NAME>
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

#### Check the host

On GPU hosts that run the bundled vLLM, the driver must be 580 or newer, or the
bundled vLLM containers do not start. Update it before upgrading:

```bash
nvidia-smi --query-gpu=driver_version --format=csv,noheader
```

If you run the monitoring overlay, the 2.3.0 overlay needs Docker Compose 2.23.1
or newer:

```bash
docker compose version --short
```

#### Update `.env`

Your `.env` is not tracked, so checking out 2.3.0 keeps it. Copy it aside first,
outside the repository: the rollback needs it, and a copy inside the checkout is
not git-ignored, so it could be committed with your secrets.

```bash
cp .env ~/openrag-2.2.x.env
```

Then compare it with the 2.3.0 `infra/compose/.env.example` and change:

- **Secrets** that break the [rules above](#secrets). The 2.2.x `.env.example`
  shipped example values for `AUTH_TOKEN`, `POSTGRES_PASSWORD`, `MINIO_ACCESS_KEY`,
  `MINIO_SECRET_KEY` and `CHAINLIT_AUTH_SECRET`; OpenRAG 2.3.0 refuses them.
  - `POSTGRES_PASSWORD`: put the new value in `.env` now; step 4 changes it in
    the database. Until step 4, do not run `$DC up`: it would start OpenRAG with
    a password the database does not accept yet.
  - `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY`: change them in `.env` only. MinIO
    and Milvus both read them from there, and the upgrade restarts both
    together.
- **`EMBEDDER_MODEL_NAME`.** Its default is now `Qwen/Qwen3-Embedding-0.6B`, and it
  is the only variable the bundled vLLM reads to pick its model. If your `.env`
  does not set it, set it to the model your data was indexed with:
  `jinaai/jina-embeddings-v3`, unless you chose another. If you also set the
  legacy `EMBEDDING_MODEL`, give it the same value. Otherwise vLLM serves Qwen
  while OpenRAG still asks for your old model: every embedding call fails, and
  `/ready` reports `checks.embedder: unavailable`. See the
  [`EMBEDDER_MODEL_NAME` row](/openrag/documentation/env_vars/).
- **`RERANKER_EXTRA_ARGS`**, if you set `RERANKER_PROVIDER=openai` with the
  `Alibaba-NLP/gte-multilingual-reranker-base` model. The bundled vLLM reranker
  now needs the `--hf-overrides` line that the 2.3.0 `.env.example` ships
  commented out: uncomment it in your `.env`.
- **`METRICS_TOKEN`**, if you scrape `GET /metrics` or run the monitoring overlay.
  The overlay needs both `METRICS_TOKEN` and `GRAFANA_ADMIN_PASSWORD` in `.env`:
  without them, every `$DC` command that includes the overlay fails, `down`
  included.

#### Run the default-embedder check

```bash
$DC exec rdb psql -U "$PG_USER" -d "$DB" -c \
  "SELECT name FROM model_endpoints WHERE model_type = 'embedder' AND is_default"
```

### During the maintenance window

#### 1. Stop traffic and let indexing finish

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

`down` keeps the data, which lives in bind-mounted directories. Copy all three
now, so that they match each other, preserving ownership (for example with
`sudo cp -a`):

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

#### 4. Start 2.3.0 to apply the PostgreSQL migrations

If you changed `POSTGRES_PASSWORD`, start PostgreSQL alone and change the role's
password first:

```bash
$DC up -d rdb
until $DC exec rdb pg_isready -q; do sleep 1; done   # if it keeps waiting, check `$DC ps rdb`
$DC exec rdb psql -U "$PG_USER" -d postgres \
  -c "ALTER ROLE CURRENT_USER WITH PASSWORD '<new password>'"
```

Start the stack. OpenRAG applies the PostgreSQL migrations when it starts, and
the Milvus migration needs them:

```bash
$DC up -d
$DC logs -f "$SVC"   # until it logs "Startup: complete"; Ctrl-C stops following
```

`Startup: complete` is logged even when startup failed. If
`ServiceContainer.initialize failed` appears before it, stop here and read the
error logged with it: a PostgreSQL migration may have failed, for example on the
[default embedder](#check-the-default-embedder).

Otherwise, stop OpenRAG, leaving PostgreSQL and Milvus running. Until the next
step, searches would answer `503` with `VDB_SCHEMA_MIGRATION_REQUIRED` anyway:

```bash
$DC stop "$SVC"
```

#### 5. Run the Milvus migration

A dry run first, which changes nothing and lists the pending migrations:

```bash
$DC run --no-deps --rm --entrypoint "" "$SVC" \
  uv run --no-dev python services/persistence/migrations/milvus/migrate.py --dry-run
```

The dry run prints the collection's current version:

- `2`, normally, for 2.2.1 and 2.2.2.
- `1` means version 2 was never applied (2.2.x only enforced it on uploads), and
  `0` that no migration was: the upgrade then runs the missing versions too.
  Version 2 rebuilds the collection and keeps a backup copy of it, unless
  `hybrid_search` is off; see
  [Milvus migrations — Version 2](/openrag/documentation/milvus_migration/#version-2--case-insensitive-bm25-analyzer).
- `3` means the collection is already migrated, by an earlier run of this step
  (the dry run then says it is already up to date) or by a development build;
  for the latter, see [If you ran a development build](#if-you-ran-a-development-build).

To size the maintenance window, take the number of rows from the dry run's
``Splitting `vector` of '<collection>' (N rows, dim=…)`` line. The tested
collection migrated 13,805 rows in 25 seconds. Assuming the duration grows
linearly with the number of rows:

```text
duration (seconds) ≈ N × 25 / 13,805 ≈ N / 550
```

That is about 5 minutes for 150,000 rows and 30 minutes for 1,000,000. Treat it
as an order of magnitude: it comes from a single run, and the hardware Milvus
runs on and the vector dimension change it.

Check that the plan routes every partition to an embedder field, and review any
`row(s) belong to partitions that do not exist in Postgres` warning: those rows
lose their vector. Then apply it:

```bash
$DC run --no-deps --rm --entrypoint "" "$SVC" \
  uv run --no-dev python services/persistence/migrations/milvus/migrate.py
```

A failed run can be run again: until its last step drops the old `vector` field,
the original vectors stay in place.

What the migration does to the data:

- Each chunk's vector moves to the field of the embedder its partition uses.
  Rows of a partition that PostgreSQL does not know are already unreachable,
  and lose their vector.
- Integers above 2^53 in a chunk's metadata come back rounded, except section
  IDs, which the copy folds below 2^53 so that each chunk stays linked to its
  neighbours. PostgreSQL keeps each file's original upload metadata.

Details are in
[Milvus migrations — Version 3](/openrag/documentation/milvus_migration/#version-3--one-vector-field-per-embedder).

#### 6. Start OpenRAG and verify

```bash
$DC up -d
```

- `GET /ready` returns `200` with `"status": "ready"`, and `checks.embedder` is
  `ok`: the `200` alone does not cover the embedder unless
  `READINESS_REQUIRE_EMBEDDER` is on. The `llm` and `reranker` checks do not
  block readiness: `unavailable` or `timeout` means that endpoint is
  unreachable, `unresolvable` that it is not configured.
- A search on an existing partition returns results, not `503`.
- A chat completion returns sources in the new shape.
- Uploading a small file completes.
- `GET /metrics` answers with `Authorization: Bearer <METRICS_TOKEN>`.

In Attu, the index on a new `vector_<embedder>` field can stay `InProgress`
indefinitely. This is expected: the migration rewrites every row, and Milvus
keeps the old 2.2.x copies without indexing them, so they stay pending. Look at
`indexed_rows` instead, which `describe_index` reports. The index is complete
once it reaches the number of rows the migration copied into that field; the
migration logs both numbers (`N row(s) indexed, M copied`). For example, the
tested collection reported 13,805 indexed rows, with `total_rows` 27,610 and
13,805 pending: complete.
The old copies also take disk and memory until you
[compact the collection](#after-the-upgrade-compact-the-milvus-collection), once
traffic is back.

Then let traffic back in.

#### 7. Remove what is no longer used

- The `logs/` directory at the repository root (`LOG_VOLUME`), once you have kept
  what you need from it.
- On a CPU host, once you no longer need to roll back, the locally built
  `openrag-vllm-openai-cpu` image: `docker image rm openrag-vllm-openai-cpu`. A
  rollback to 2.2.x would otherwise rebuild it from `extern/vllm`.
- The old copies of the Milvus rows:
  [compact the collection](#after-the-upgrade-compact-the-milvus-collection).

### Rolling back a Compose upgrade

Rolling back the checkout alone is not enough: 2.2.x starts, but every call fails,
because it does not know the newer PostgreSQL schema and its searches expect the
`vector` field that step 5 dropped. Either way below also removes the
`openrag_venv` volume, which holds the Python packages 2.3.0 installed; 2.2.x
installs its own at startup. Its name is `<project>_openrag_venv`, where the
project is `compose` unless you set one (`docker volume ls` lists it).

**From the backups of step 2:**

1. `$DC down`.
2. Restore the directories copied in step 2: remove each current directory first,
   then copy the backup back with `sudo cp -a`. Copying over a directory that
   still exists merges the two, which corrupts PostgreSQL and etcd. Then remove
   the `openrag_venv` volume: `docker volume rm <project>_openrag_venv`.
3. `git checkout` the version you ran before, restore the `.env` you copied aside
   (`cp ~/openrag-2.2.x.env .env`), and `$DC up -d`.

**Without the backups**, by undoing the migrations with 2.3.0 still checked out.
Before you start, make sure **all** of the following hold. Some of them are only
checked by the PostgreSQL downgrade, which runs after the Milvus one: if it
refuses then, Milvus is back at version 2 while PostgreSQL is still at 2.3.0,
and neither version runs on that.

- `POSTGRES_DATABASE` is unset or equal to
  `partitions_for_collection_<VDB_COLLECTION_NAME>`: the Alembic command only
  targets that database.
- The Milvus migration of step 5 completed: its dry run says the collection is up
  to date. A failed run stops at version 2 with the new fields already added and
  the metadata rewritten, and the downgrade then does nothing. Run the migration
  again to completion first, or roll back from the backups.
- No workspace ID is shared by two partitions: 2.3.0 allows it, and the
  PostgreSQL downgrade refuses it. This query must return no row:
  `$DC exec rdb psql -U "$PG_USER" -d "$DB" -c "SELECT workspace_id FROM workspaces GROUP BY workspace_id HAVING COUNT(*) > 1"`
- Every embedder field in use has the same dimension.
- Every partition holding files still resolves, through PostgreSQL, to an
  embedder field: no embedder deleted and no default changed since the upgrade.
- The collection has fewer than 10 vector fields, since the downgrade adds
  `vector` back.

Run the commands below in this order: the Milvus downgrade reads the PostgreSQL
schema that `alembic downgrade` removes. The first one, a dry run, checks the
dimensions, the routing and the number of vector fields without changing
anything; the workspace check above is yours to run. `b9c0d1e2f3a4` is the last
PostgreSQL revision of 2.2.1 and 2.2.2:

```bash
$DC stop "$SVC"
$DC run --no-deps --rm --entrypoint "" "$SVC" \
  uv run --no-dev python services/persistence/migrations/milvus/migrate.py --downgrade --target 2 --dry-run
$DC run --no-deps --rm --entrypoint "" "$SVC" \
  uv run --no-dev python services/persistence/migrations/milvus/migrate.py --downgrade --target 2
$DC run --no-deps --rm --entrypoint "" "$SVC" \
  uv run --no-dev alembic -c /app/openrag/services/persistence/migrations/alembic/alembic.ini downgrade b9c0d1e2f3a4
$DC down
docker volume rm <project>_openrag_venv
```

Then `git checkout` the version you ran before, restore the `.env` you copied
aside (`cp ~/openrag-2.2.x.env .env`), and `$DC up -d`. If you changed `POSTGRES_PASSWORD` in step 4, edit the
restored `.env` to use the new password: the database still has it.
Files indexed after the upgrade are kept. The chunk metadata the upgrade
rewrote is not restored: integers above 2^53 stay rounded and section IDs stay
folded. To recover it, roll back from the backups instead.

## After the upgrade: compact the Milvus collection

Run this once on Kubernetes or Docker Compose, after the upgrade, at a quiet
time. Searches and uploads keep working while it runs.

The version 3 migration rewrites every chunk, and Milvus keeps the old 2.2.x
copies, marked deleted. Searches skip them, so answers are correct, but Milvus
does not remove them on its own:

- the collection holds about twice as many rows as it serves, on disk and in
  query-node memory;
- the old segments keep the data of the dropped `vector` field;
- the index on the `vector_<embedder>` field stays `InProgress`.

A plain compaction does not remove them. The old segments have no value for the
new field, so Milvus never builds its index on them, and by default
(`dataCoord.compaction.indexBasedCompaction`) Milvus only compacts segments whose
indexes are built. Turn that setting off for one compaction, then back on.
Milvus reads it from etcd, under `<etcd.rootPath>/config/`, without a
restart.

Set these variables, with the block for your deployment only: both set `RUN`,
`ETCD` and `CONFIG`.

With Docker Compose, from `infra/compose`:

```bash
# bash; in zsh, see the note at the top of this page
DC="docker compose"   # add -p <project>, your -f overlays, and --profile cpu on a CPU host
SVC=openrag           # openrag-cpu on a CPU host
RUN="$DC exec $SVC"
ETCD="$DC exec -T etcd etcdctl"
CONFIG=by-dev/config  # <Milvus's etcd.rootPath>/config; the root path is by-dev unless you changed it
```

On Kubernetes:

```bash
NS=openrag            # the release namespace
FULLNAME=openrag      # fullnameOverride; `kubectl get deploy -n $NS` shows it as <FULLNAME>-openrag
ETCD_POD=<etcd pod>   # one of the etcd pods Milvus uses; `kubectl get pod -n $NS | grep etcd` lists them
RUN="kubectl exec -n $NS deploy/$FULLNAME-openrag --"
ETCD="kubectl exec -n $NS $ETCD_POD -- etcdctl"
CONFIG=by-dev/config  # <Milvus's etcd.rootPath>/config; the root path is by-dev unless you changed it
```

And this check, which reads the collection from OpenRAG's configuration:

```bash
CHECK='
from core.config import load_config
from pymilvus import MilvusClient
vdb = load_config().vectordb
name = vdb.collection_name
c = MilvusClient(uri=f"http://{vdb.host}:{vdb.port}")
print("live rows:", c.query(name, filter="", output_fields=["count(*)"])[0]["count(*)"])
s = c.get_collection_stats(name)
print("physical rows:", s["row_count"])
print("current-schema segments:", s.get("schema_version_consistent_segments"), "/", s.get("schema_version_total_segments"))
for field in c.describe_collection(name)["fields"]:
    if field["name"].startswith("vector_"):
        for idx in c.list_indexes(name, field_name=field["name"]):
            i = c.describe_index(name, idx)
            print("index", idx, i["state"], "pending", i["pending_index_rows"])
'
```

1. Run the check and keep its output:

   ```bash
   $RUN uv run --no-dev --no-sync python -c "$CHECK"
   ```

   Right after the migration, `physical rows` is about twice `live rows`, and the
   index is `InProgress`. If `physical rows` is already close to `live rows`,
   with the index `Finished`, there is nothing to do.
2. List the overrides of this setting already in etcd:

   ```bash
   KEY=$CONFIG/datacoord.compaction.indexbasedcompaction
   $ETCD get --prefix "$CONFIG/" --keys-only | grep -i indexbasedcompaction
   ```

   Milvus accepts the same setting under several spellings of its key. If this
   prints a key other than `$KEY`, stop and ask whoever set it: step 4 restores
   only `$KEY`, so the procedure would leave both in place. Otherwise, save the
   current value of `$KEY`, then turn index-based compaction off:

   ```bash
   PREVIOUS=$($ETCD get "$KEY" --print-value-only)
   echo "previous value: ${PREVIOUS:-none}"
   $ETCD put "$KEY" false
   ```

   Note the previous value: step 4 needs it if you run it from another shell.
3. Compact the collection:

   ```bash
   $RUN uv run --no-dev --no-sync python -c '
   import sys, time
   from core.config import load_config
   from pymilvus import MilvusClient
   TIMEOUT = 3600  # seconds; raise it for a large collection
   vdb = load_config().vectordb
   c = MilvusClient(uri=f"http://{vdb.host}:{vdb.port}")
   deadline = time.monotonic() + TIMEOUT
   def left():  # each call to Milvus may wait only until the deadline
       seconds = deadline - time.monotonic()
       if seconds <= 0:
           sys.exit(f"compaction: still executing after {TIMEOUT} s")
       return seconds
   job = c.compact(vdb.collection_name, timeout=left())
   print("job", job, flush=True)
   while (state := c.get_compaction_state(job, timeout=left())) != "Completed":
       if state != "Executing":
           sys.exit(f"compaction {job}: unexpected state {state}")
       print(state, flush=True)
       time.sleep(min(15, left()))
   print("done")
   '
   ```

   It prints the job ID, then `Executing` every 15 seconds while the job runs.
   It prints `done` once Milvus reports the job `Completed`, and stops with an
   error on any state other than `Executing`, such as `UndefiedState`, or when
   the job, or a call to Milvus that does not answer, outlasts `TIMEOUT`. Go to
   step 4 either way. After a timeout, the compaction keeps running in Milvus.
   `Completed` only means that no task of the job is still running (Milvus also
   reports it for a job ID it does not know): step 5 checks what the compaction
   did.
4. Restore the previous value, even if step 3 failed:

   ```bash
   if [ -n "$PREVIOUS" ]; then $ETCD put "$KEY" "$PREVIOUS"; else $ETCD del "$KEY"; fi
   $ETCD get --prefix "$CONFIG/" | grep -i -A1 indexbasedcompaction
   ```

   The `get` prints the previous key and value, or nothing if there was none.

5. Wait a minute, then run the check again. The compaction worked when
   `physical rows` is close to `live rows`, the two segment counts are equal,
   and the index is `Finished` with `pending 0`; the index can take a few more
   minutes. `live rows` must not drop. It can be slightly higher than
   `physical rows` while new uploads are still in memory.

   If `physical rows` has barely changed, Milvus did not apply the setting: turn
   it off again with `$ETCD put "$KEY" false` alone, not all of step 2, which
   would save `false` as the previous value. Then restart Milvus
   (`$DC restart milvus`, or on Kubernetes the Milvus coordinator that
   `kubectl get deploy -n "$NS" | grep milvus` lists), wait until it is ready,
   and repeat steps 3 to 5.

Milvus deletes the old files from its object storage after
`dataCoord.gc.dropTolerance`, 3 hours by default, so the disk space comes back a
few hours later. With Docker Compose, `$DC exec -T minio du -sh /minio_data`
shows how much MinIO holds. Do not delete files from MinIO or the bucket
yourself.

On one deployment, this brought 291,628 physical rows down to 144,474 for
144,683 live rows, with the index `Finished` and 0 pending.
