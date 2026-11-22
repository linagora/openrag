---
title: Object storage upgrade (MinIO to SILO)
---

OpenRAG 2.3.2 replaces MinIO `RELEASE.2024-12-18T13-15-44Z`, which gets no more security fixes and is affected by CVE-2026-40344 and CVE-2026-41145, with [SILO](https://silo.pgsty.com/), the maintained MinIO fork: `pgsty/silo:RELEASE.2026-09-16T00-00-00Z`. SILO opens an existing MinIO volume in place. If that image becomes unavailable, `linagoraai/silo` is a copy with the same digest.

A fresh Kubernetes install only needs the [credentials](#credentials). An external S3 store only needs [External S3 store](#external-s3-store).

## The switch is one-way: back up first

SILO writes objects the old MinIO cannot read. After the upgrade, going back to the previous release makes Milvus fail (`decodeXLHeaders: Unknown xl meta version 3`) until you restore a backup taken before it. Switching forward again recovers everything.

The backup includes PostgreSQL, because 2.3.2 also migrates the database.

## Docker Compose

Run the commands from `infra/compose`, with these set to your `.env` values if it overrides them:

```bash
DB_DIR=../../db             # DB_VOLUME
MILVUS_DIR=milvus/volumes   # MILVUS_VOLUME_DIRECTORY, prefixed with milvus/ when relative
DATA_DIR=../../data         # DATA_VOLUME
```

Named volumes, as set by `.env.named-volumes.example`, aren't directories: back up and restore those volumes instead.

Add your `-f` overlays, and `--profile cpu` on a CPU host, to every `docker compose` command.

1. Stop the stack and back it up:

   ```bash
   docker compose down
   mkdir ~/openrag_backup &&   # stops if an earlier backup is there: move it aside first
     sudo cp -a "$DB_DIR"     ~/openrag_backup/db &&      # PostgreSQL
     sudo cp -a "$MILVUS_DIR" ~/openrag_backup/milvus &&  # etcd, MinIO and Milvus
     sudo cp -a "$DATA_DIR"   ~/openrag_backup/data       # uploaded files
   ```

2. Start the new release. `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY` don't change.

   ```bash
   git fetch --tags
   git checkout v2.3.2   # or a later release
   docker compose pull
   docker compose up -d
   ```

### Rolling back

Remove each directory before copying its backup back: copying over an existing directory merges the two and corrupts PostgreSQL and etcd.

```bash
docker compose down
sudo rm -rf "${DB_DIR:?}" "${MILVUS_DIR:?}" "${DATA_DIR:?}" &&
  sudo cp -a ~/openrag_backup/db     "$DB_DIR" &&
  sudo cp -a ~/openrag_backup/milvus "$MILVUS_DIR" &&
  sudo cp -a ~/openrag_backup/data   "$DATA_DIR" &&
  git checkout v2.3.1 &&   # the release you ran before
  docker compose pull &&
  docker compose up -d
```

## Kubernetes (Helm)

### Credentials

The chart refuses to install until `minioCredentials.accessKey` and `minioCredentials.secretKey` replace the public `minioadmin` default: an access key of at least 3 characters, a secret key of at least 12, and no published value. MinIO accepts new credentials on existing data. If your values set `milvus.minio.accessKey` / `secretKey`, move them to `minioCredentials`: the old keys now fail the install.

Generate them once and pass the file on every upgrade:

```bash
touch minio-credentials.yaml && chmod 600 minio-credentials.yaml
cat > minio-credentials.yaml <<EOF
minioCredentials:
  accessKey: "$(openssl rand -hex 8)"
  secretKey: "$(openssl rand -hex 20)"
EOF
```

To manage the Secret yourself, for example with External Secrets or Vault, create `openrag-minio-credentials` with the keys `accesskey` and `secretkey` and set `minioCredentials.create: false`.

### Upgrade

Every MinIO pod must be stopped before SILO starts: a distributed MinIO refuses peers running a different binary.

```bash
NS=openrag                 # the release namespace
RELEASE=openrag            # the Helm release name
CHART_VERSION=0.7.8        # the chart of the release you upgrade to; 0.7.8 is OpenRAG 2.3.2
PG_POD=openrag-postgresql-0   # the bundled PostgreSQL pod: <postgresql.fullnameOverride>-0
PG_USER=root               # postgresql.auth.username
DB=partitions_for_collection_vdb_test   # POSTGRES_DATABASE; when unset, partitions_for_collection_<VDB_COLLECTION_NAME>
```

1. Stop traffic and wait until no indexing task is active (`GET /queue/tasks?task_status=active` returns `{"tasks": []}`). Then stop OpenRAG, Milvus, etcd and MinIO:

   ```bash
   kubectl scale deployment -n "$NS" -l app.kubernetes.io/name=openrag --replicas=0
   kubectl scale deployment -n "$NS" -l app.kubernetes.io/name=milvus,app.kubernetes.io/instance="$RELEASE" --replicas=0
   kubectl scale statefulset -n "$NS" -l app.kubernetes.io/name=etcd,app.kubernetes.io/instance="$RELEASE" --replicas=0
   kubectl scale deployment,statefulset -n "$NS" -l app=minio,release="$RELEASE" --replicas=0
   kubectl wait pod -n "$NS" -l app.kubernetes.io/name=openrag --for=delete --timeout=5m
   kubectl wait pod -n "$NS" -l app.kubernetes.io/name=etcd,app.kubernetes.io/instance="$RELEASE" --for=delete --timeout=5m
   kubectl wait pod -n "$NS" -l app=minio,release="$RELEASE" --for=delete --timeout=5m
   ```

2. Snapshot the etcd, MinIO and `<fullnameOverride>-data` volumes, and dump PostgreSQL:

   ```bash
   touch openrag.dump && chmod 600 openrag.dump
   kubectl exec -n "$NS" "$PG_POD" -- env PGUSER="$PG_USER" PGDATABASE="$DB" \
     sh -c 'PGPASSWORD="$(cat /opt/bitnami/postgresql/secrets/password)" pg_dump -Fc' > openrag.dump
   kubectl exec -i -n "$NS" "$PG_POD" -- pg_restore -l < openrag.dump > /dev/null && echo "dump OK"
   ```

3. Upgrade. Helm starts Milvus, etcd and MinIO again, MinIO on SILO.

   ```bash
   helm upgrade "$RELEASE" oci://ghcr.io/linagora/openrag-stack --version "$CHART_VERSION" -n "$NS" \
     -f ./values.yaml -f ./minio-credentials.yaml
   ```

   Don't use `--reuse-values`: it keeps the old chart's defaults, and the chart refuses it with "milvus.minio.existingSecret must name the Secret holding MinIO's credentials…". On Helm 3.14 or later, `--reset-then-reuse-values` works.

   With `milvus.minio.mode: standalone`, Helm doesn't start MinIO again:

   ```bash
   kubectl scale deployment -n "$NS" -l app=minio,release="$RELEASE" --replicas=1
   ```

4. Once Milvus is ready, restart OpenRAG, which otherwise keeps answering 503:

   ```bash
   kubectl rollout restart deployment -n "$NS" -l app.kubernetes.io/name=openrag
   ```

### Rolling back

1. Stop OpenRAG, Milvus, etcd and MinIO as in upgrade step 1. With `ray.enabled: true`, also run `kubectl delete raycluster -n "$NS" <fullnameOverride>-raycluster`. The rollback recreates it.
2. Restore the etcd, MinIO and `<fullnameOverride>-data` volumes from their snapshots.
3. Restore PostgreSQL, where `<password>` is the database password:

   ```bash
   echo "DROP SCHEMA public CASCADE; CREATE SCHEMA public; GRANT USAGE ON SCHEMA public TO PUBLIC;" | kubectl exec -i -n "$NS" "$PG_POD" -- \
     env PGPASSWORD='<password>' psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d "$DB"
   kubectl exec -i -n "$NS" "$PG_POD" -- \
     env PGPASSWORD='<password>' pg_restore --no-owner --exit-on-error -U "$PG_USER" -d "$DB" < openrag.dump
   ```

4. Roll back to the revision before the upgrade:

   ```bash
   helm rollback "$RELEASE" <revision> -n "$NS"
   ```

5. Start a standalone MinIO and restart OpenRAG, as in upgrade steps 3 and 4.

### Later SILO updates

A later release that changes the SILO image needs the same order: stop MinIO (the `app=minio` lines of upgrade step 1) before `helm upgrade`, then start a standalone MinIO again, as in upgrade step 3.

### Changing the credentials later

MinIO and Milvus only read the credentials when they start. Stop MinIO, upgrade, then restart Milvus, with `CHART_VERSION` set to the chart the release runs now (the CHART column of `helm list -n "$NS"`):

```bash
kubectl scale deployment,statefulset -n "$NS" -l app=minio,release="$RELEASE" --replicas=0
kubectl wait pod -n "$NS" -l app=minio,release="$RELEASE" --for=delete --timeout=5m
helm upgrade "$RELEASE" oci://ghcr.io/linagora/openrag-stack --version "$CHART_VERSION" -n "$NS" -f ./values.yaml -f ./minio-credentials.yaml --wait
kubectl scale deployment -n "$NS" -l app=minio,release="$RELEASE" --replicas=1   # standalone MinIO only
kubectl rollout restart deployment -n "$NS" -l app.kubernetes.io/name=milvus,app.kubernetes.io/instance="$RELEASE"
```

### External S3 store

With an external S3 store (`milvus.externalS3.enabled: true`, `milvus.minio.enabled: false`), skip all of the above and leave `minioCredentials` unset. The chart writes `milvus.externalS3.accessKey` / `secretKey` to the Secret `openrag-minio-credentials`. If a Secret of that name already exists outside the release, the install fails: delete it, or set `minioCredentials.create: false` and put the external store's keys in it.
