---
title: Upgrading OpenRAG
tableOfContents:
  maxHeadingLevel: 4
---

Each upgrade has a section for what changes in every deployment, followed by the
procedure for Kubernetes and for Docker Compose. Read all of it for the version you
are moving to before you start. The 2.3.0 sections start from 2.2.1 or 2.2.2; from
an earlier release, upgrade to 2.2.2 first.

The commands are written for bash. In zsh, run `setopt interactive_comments sh_word_split`
first: without it, the comments after the commands are read as arguments and the
variables below are not set.

## Changes in 2.3.0 for every deployment

These apply to an upgrade from OpenRAG 2.2.1 or 2.2.2, whichever way it is
deployed. The [Kubernetes](#22x-to-230-on-kubernetes) and
[Docker Compose](#22x-to-230-with-docker-compose) procedures below include them.

The upgrade needs a **maintenance window**: the Milvus collection must be migrated
while nothing writes to it, and searches fail until it is. Each procedure has three
parts:

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
| New PostgreSQL migrations | Applied when OpenRAG 2.3.0 starts, or on Kubernetes by the migration Job when you enabled it. |
| Chat and text completion responses: `extra` and its source entries | `extra` is a JSON object instead of a JSON-encoded string, and each document source puts the chunk's metadata under `chunk`. Clients must be updated. |
| `GET /metrics` | The admin token is refused (`403`); scrapers need `METRICS_TOKEN`. |
| Secret checks | Five credentials shorter than 12 characters, or a checked secret set to a value the project publishes as an example, stop OpenRAG at startup. |
| Ray | Indexer actors change protocol: tasks running when the old version stops are lost. |
| Readiness | `GET /ready` is new: it fails while PostgreSQL, Milvus or Ray is unreachable, and reports the model endpoints. |
| vLLM embedder | Runs on vLLM `v0.30.0` with `--runner pooling --convert embed`. |
| Logs | Written to stderr only; the `logs` volume is no longer mounted. |

### Update API clients

In chat completion responses, streamed or not, and in text completion responses,
`extra` is now a JSON object. Up to 2.2.2 it was a JSON-encoded string that clients had to parse with
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
`rerank_score`, new in 2.3.0, is present only when a reranker ran. Web entries
(`source_type: "web"`) are unchanged.

Other requests that 2.2.x accepted, or answered differently:

- **`Content-Type`.** A JSON body sent without `Content-Type: application/json`
  is answered `422`.
- **Uploads.** A file whose content does not match its extension (PDF, images,
  DOCX, PPTX, `.doc`) is refused with `415`. A second `POST` of a file that is
  still indexing is refused with `409 DOCUMENT_INDEXING_IN_PROGRESS`, which gives
  the running task's status URL; `PUT` is unchanged. A document that produces no
  chunks fails with `422 NO_INDEXABLE_CONTENT` instead of being reported as
  indexed, and its callback reports `"error"`.
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

`GET /metrics` no longer accepts the admin token. Set `METRICS_TOKEN` and give the
same value to your scraper as a bearer token, or, on a network only your scraper
reaches, leave `METRICS_TOKEN` unset and set `METRICS_ALLOW_UNAUTHENTICATED=true`. See
[Prometheus metrics](/openrag/documentation/prometheus_metrics/).

### Secrets

`AUTH_TOKEN`, `POSTGRES_PASSWORD`, `CHAINLIT_AUTH_SECRET`, `MINIO_SECRET_KEY` and
`GRAFANA_ADMIN_PASSWORD` need at least 12 characters, and none of the checked secrets may be a value
the project publishes as an example, such as the defaults of the 2.2.x
`.env.example`. OpenRAG refuses to start otherwise, with an error naming the
variable. The rules are in
[Environment variables — What will be refused](/openrag/documentation/env_vars/#what-will-be-refused).

Changing a secret of a deployment that already holds data needs care:

- **PostgreSQL password.** The bundled PostgreSQL sets its password only when it
  initialises an empty data directory, so a new value in the configuration also
  needs an `ALTER ROLE` in the database; changing the configuration alone leaves
  OpenRAG unable to log in. If your current password passes the rules, keep it.
  Otherwise change it in the database during the window, after the backup and
  before 2.3.0 starts, and in the configuration where the steps below say: 2.2.x
  cannot open a connection once the database has the new value. The rollback
  sections below account for it. Pick a value without quotes or `$`, such as the
  output of `openssl rand -hex 16`: the commands below pass it inside quotes.
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
  on every compaction attempt. The v3.0.2 of 2.3.0 no longer does, but the lines
  already written stay. If you keep Milvus logs or ship them to a log system,
  change the keys during the upgrade. MinIO and Milvus must restart together with
  the new pair; with Compose, [Update `.env`](#update-env) says how.

### Check the default embedder

A PostgreSQL migration pins the partitions that hold files and follow the
`default` embedder to the endpoint marked as default. If such partitions exist and
there is not exactly one default embedder, it stops: OpenRAG answers `503`, or,
when the Helm migration Job applies the migrations, `helm upgrade` fails. Run this query in OpenRAG's database before the window (the
commands for each deployment are below):

```sql
SELECT name FROM model_endpoints WHERE model_type = 'embedder' AND is_default;
```

One row is expected; otherwise mark one embedder endpoint as the default, in the
admin UI or through the admin API, first. Mark the embedder those partitions were
indexed with: the migration pins them to it for good, and the Milvus migration
moves their vectors into its field.

If the migration has already stopped 2.3.0, whose admin UI is then unavailable,
mark it in the database and check that the query above returns that one row:

```sql
UPDATE model_endpoints SET is_default = (name = '<endpoint name>') WHERE model_type = 'embedder';
```

Then apply the migrations again: restart OpenRAG where it applies them itself; on
Kubernetes with the migration Job, run `helm upgrade` again; with both off, run
them by hand as in [step 4](#4-upgrade-the-release), then restart OpenRAG.

Rolling back from the backups undoes the pinning; the Compose rollback without
backups keeps it.

### If you ran a development build

If that build ran the version 3 Milvus migration on Milvus v3.0.1, upgrade
Milvus to v3.0.2 before continuing. The old Milvus version cannot compact the
segments created by that migration; restarting v3.0.1 or rerunning the
migration will not fix it. The 2.3.0 Compose files and Helm chart use v3.0.2.

A collection migrated to version 3 by a build of the development branch from 22
September 2026 (#994) until #1096 merged on 28 September went through a copy that
rounded chunk section IDs, which breaks neighbour-chunk expansion. The migration does not repair them: after
upgrading, re-index the files that were indexed before that migration. A Milvus
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
  2048, raise `maxModelLen` with it: otherwise vLLM refuses the longer requests,
  and OpenRAG falls back to the served length, with a warning, embedding shorter
  inputs and sizing new chunks for them.
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

A failure here would also fail `helm upgrade`. Pass the same `--set` flags or
secret values your install used: `-f "$VALUES"` alone does not carry a
`--set postgresql.auth.password=…` given at install time, and the render then
fails on the missing secret. The same applies to the `helm upgrade` in step 4.

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
override. Charts 0.6.4 and 0.6.5 pinned this value to `v3.0.1`; carrying that
setting forward makes Helm keep the old Milvus version for the migration,
despite the newer chart default.

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

With the bundled PostgreSQL. The password file is the bundled chart's default;
with `postgresql.auth.existingSecret` and a custom key, adjust its name:

```bash
kubectl exec -n "$NS" "$PG_POD" -- env PGUSER="$PG_USER" PGDATABASE="$DB" sh -c \
  'PGPASSWORD="$(cat /opt/bitnami/postgresql/secrets/password)" psql -c \
   "SELECT name FROM model_endpoints WHERE model_type = '"'"'embedder'"'"' AND is_default"'
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

Take the backups as one set, after step 1:

- PostgreSQL, for example
  `kubectl exec -n "$NS" "$PG_POD" -- env PGUSER="$PG_USER" PGDATABASE="$DB" sh -c 'PGPASSWORD="$(cat /opt/bitnami/postgresql/secrets/password)" pg_dump -Fc' > openrag.dump`
- Milvus: its etcd and its object storage (MinIO or your S3 bucket). Stop them
  first, so that the snapshots are one point in time: note the replica counts
  that `kubectl get deploy,statefulset -n "$NS"` shows for Milvus, etcd and
  MinIO, scale those to zero, snapshot their volumes (or copy your bucket) with
  your storage's snapshot mechanism, then scale them back to those counts and
  wait until their pods are Ready. OpenRAG 2.2.x keeps running meanwhile; with
  traffic stopped, it does nothing with Milvus.
- Uploaded files: snapshot the `<FULLNAME>-data` volume.

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
2. Put the new value in the Secret OpenRAG reads. With
   `postgresProvisioning.migrationJob.enabled`, the migration Job runs before
   `helm upgrade` updates that Secret, and would otherwise fail to log in. For the
   Secret the chart renders from your values:

   ```bash
   kubectl patch secret -n "$NS" "$FULLNAME-env-secrets" \
     -p '{"stringData":{"POSTGRES_PASSWORD":"<new password>"}}'
   ```

   With `env.existingSecret`, update that Secret; with an external secrets
   provider, update the source and wait until the Secret has synced. Your values
   already carry the new value, from
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
back on 2.2.x). The notes `helm upgrade` prints recreate only the workers, for
their metrics port; this upgrade needs the head recreated too. Once they are
Ready, scale OpenRAG up. A later `helm upgrade` without the flag sets the count
from your values again:

```bash
kubectl delete pod -n "$NS" -l ray.io/cluster="$FULLNAME-raycluster"
kubectl get pod -n "$NS" -l ray.io/cluster="$FULLNAME-raycluster" -w   # until the new pods are Running and Ready
kubectl scale -n "$NS" deploy/"$FULLNAME-openrag" --replicas=<your usual count>
```

`kubectl get raycluster -n "$NS"` prints the cluster's name if you changed
`fullnameOverride`. A wrong name selects no pods, and Ray stays on 2.2.x.

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

Run the migration from the new OpenRAG pod, which has the image, the
configuration and access to both PostgreSQL and Milvus. First a dry run, which
changes nothing and lists the pending migrations:

```bash
kubectl exec -n "$NS" deploy/"$FULLNAME-openrag" -- \
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

Check that the plan routes every partition to an embedder field, and review any
`row(s) belong to partitions that do not exist in Postgres` warning: those rows
lose their vector. Then apply it:

```bash
kubectl exec -n "$NS" deploy/"$FULLNAME-openrag" -- \
  uv run --no-dev --no-sync python services/persistence/migrations/milvus/migrate.py
```

The pod can stay up while this runs: until the collection reaches version 3, the
application cannot index into it. Deleting a file still reaches Milvus, though,
so keep traffic stopped: after copying, the migration counts the rows again and,
if the count moved, stops before dropping the old `vector` field. A failed run can
be run again. Until that last step, the original vectors stay in place, but the
copy has already rewritten each chunk's metadata, as described below. The
migration runs inside `kubectl exec`, and stops if that session drops (an idle
timeout on the API server or a load balancer in between): run it again.

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

- `GET /ready` returns `200` with `"status": "ready"`, and `embedder` is `ok`
  under `checks` (only `postgres`, `milvus` and `ray` decide the `200`, unless
  `READINESS_REQUIRE_EMBEDDER` is on). An `llm` or `reranker` check that is not
  `ok` means that endpoint is unreachable (`unavailable`, `timeout`) or not
  configured (`unresolvable`); it does not block readiness.
- A search on an existing partition returns results, not `503`.
- A chat completion returns sources in the new shape.
- Uploading a small file completes (`GET /queue/tasks?task_status=active` empties again).
- `GET /metrics` answers with `Authorization: Bearer <METRICS_TOKEN>`.

The index for a new `vector_<embedder>` field can remain `InProgress` while
superseded 2.2.x copies are pending; Milvus does not index those rows. Attu
shows the index state, and `describe_index` reports `indexed_rows`,
`total_rows` and `pending_index_rows`. For example, the tested collection
reported 13,805 indexed rows out of 27,610, with 13,805 pending.

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
5. `helm rollback "$RELEASE" <previous revision> -n "$NS"`.

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

Your `.env` is not tracked, so checking out 2.3.0 keeps it. Copy it aside first;
the rollback needs it:

```bash
cp .env .env.2.2.x
```

Then compare it with the 2.3.0 `infra/compose/.env.example` and change:

- **Secrets** that break the [rules above](#secrets). The 2.2.x `.env.example`
  shipped example values for `AUTH_TOKEN`, `POSTGRES_PASSWORD`, `MINIO_ACCESS_KEY`,
  `MINIO_SECRET_KEY` and `CHAINLIT_AUTH_SECRET`; OpenRAG 2.3.0 refuses them.
  - `POSTGRES_PASSWORD`: put the new value in `.env`; step 4 changes it in the
    database. 2.2.x keeps running with the old value until step 2 stops it; do not
    run `$DC up` before then, which would restart OpenRAG with the new value.
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
  which refuses to start without it. With the overlay in `$DC`, every `$DC`
  command, `down` included, fails until `.env` sets both `METRICS_TOKEN` and
  `GRAFANA_ADMIN_PASSWORD`.

#### Run the default-embedder check

```bash
$DC exec rdb psql -U "$PG_USER" -d "$DB" -c \
  "SELECT name FROM model_endpoints WHERE model_type = 'embedder' AND is_default"
```

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

If you changed `POSTGRES_PASSWORD`, start PostgreSQL alone and change the role's
password first:

```bash
$DC up -d rdb
until $DC exec rdb pg_isready -q; do sleep 1; done   # if it keeps waiting, check `$DC ps rdb`
$DC exec rdb psql -U "$PG_USER" -d postgres \
  -c "ALTER ROLE CURRENT_USER WITH PASSWORD '<new password>'"
```

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

- `GET /ready` returns `200` with `"status": "ready"`, and `embedder` is `ok`
  under `checks` (only `postgres`, `milvus` and `ray` decide the `200`, unless
  `READINESS_REQUIRE_EMBEDDER` is on). An `llm` or `reranker` check that is not
  `ok` means that endpoint is unreachable (`unavailable`, `timeout`) or not
  configured (`unresolvable`); it does not block readiness.
- A search on an existing partition returns results, not `503`.
- A chat completion returns sources in the new shape.
- Uploading a small file completes.
- `GET /metrics` answers with `Authorization: Bearer <METRICS_TOKEN>`.

The index for a new `vector_<embedder>` field can remain `InProgress` while
superseded 2.2.x copies are pending; Milvus does not index those rows. Attu
shows the index state, and `describe_index` reports `indexed_rows`,
`total_rows` and `pending_index_rows`. For example, the tested collection
reported 13,805 indexed rows out of 27,610, with 13,805 pending.

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
2. Restore the directories copied in step 2: remove each current directory first,
   then copy the backup back with `sudo cp -a`. Copying over a directory that
   still exists merges the two, which corrupts PostgreSQL and etcd. Then remove
   the `openrag_venv` volume: `docker volume rm <project>_openrag_venv`.
3. `git checkout` the version you ran before, restore the `.env` you copied aside,
   and `$DC up -d`.

**Without the backups**, by undoing the migrations with 2.3.0 still checked out.
This way works only when `POSTGRES_DATABASE` is unset or equal to
`partitions_for_collection_<VDB_COLLECTION_NAME>`: the Alembic command always
targets that database. It also needs every check below to pass **before** you
start, since a refusal halfway leaves Milvus back at version 2 and PostgreSQL
still at 2.3.0, which neither version runs on:

- the Milvus migration of step 5 completed: its dry run says the collection is up
  to date. A run that failed stops at version 2 with the new fields already added
  and the metadata rewritten, and the downgrade to version 2 then does nothing.
  Run the migration again to completion first, or roll back from the backups;

- no workspace ID shared by two partitions, which 2.3.0 allows and the PostgreSQL
  downgrade refuses:
  `$DC exec rdb psql -U "$PG_USER" -d "$DB" -c "SELECT workspace_id FROM workspaces GROUP BY workspace_id HAVING COUNT(*) > 1"`
  returns no row;
- every embedder field in use has the same dimension;
- every partition holding files still resolves, through PostgreSQL, to an
  embedder field: no embedder deleted and no default changed since the upgrade;
- the collection has fewer than 10 vector fields, since the downgrade adds
  `vector` back.

A dry run of the Milvus downgrade checks the dimensions, the routing and the
number of vector fields without changing anything; the workspace check is yours. Then, in this order (the Milvus
downgrade reads the PostgreSQL schema that `alembic downgrade` removes).
`b9c0d1e2f3a4` is the last PostgreSQL revision of 2.2.1 and 2.2.2:

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
aside, and `$DC up -d`. If you changed `POSTGRES_PASSWORD` in step 4, keep the new
value in the restored `.env`: this way does not restore the database's copy of it.
Files indexed after the upgrade are kept. The chunk metadata the upgrade
rewrote is not restored: integers above 2^53 stay rounded and section IDs stay
folded. To recover it, roll back from the backups instead.
