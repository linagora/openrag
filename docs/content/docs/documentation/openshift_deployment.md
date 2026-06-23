---
title: Deploying OpenRAG on OpenShift
---

# Deploying OpenRAG on OpenShift

OpenShift deployments should use platform-managed persistent storage instead of repository-local host paths. This keeps runtime data outside the application image and makes storage ownership explicit.

For local Docker Compose, OpenRAG keeps the historical host-path defaults to avoid breaking existing deployments. The OpenShift-style storage layout is opt-in.

## Storage model

OpenRAG stores several kinds of state:

- uploaded files and application data
- logs
- model weights and Hugging Face caches
- PostgreSQL data
- Milvus, etcd, and MinIO data

On OpenShift, these should be backed by persistent volumes. Do not rely on container-local storage for these paths, because pods can be rescheduled or recreated.

## Docker Compose parity

When you want to test an OpenShift-like storage layout with Docker Compose, use the named-volume profile:

```bash
cd infra/compose
cp .env.named-volumes.example .env.named-volumes.local
docker compose --env-file .env.named-volumes.local up -d
```

This profile makes Docker manage the storage volumes instead of writing state into the repository tree.

The default Compose stack still uses the historical host paths. This is intentional so existing local deployments keep using their current data.

Use `docker compose down` to stop the stack while keeping data. Use `docker compose down -v` only when you intentionally want to remove the named volumes and start from a clean state.

## Migration note

Switching an existing deployment from host paths to named volumes or PVC-backed storage changes where OpenRAG reads its data from.

The old files are not deleted, but the new deployment will not see them unless the data is copied to the new storage backend. Before switching storage mode:

1. Stop OpenRAG.
2. Back up the existing host-path data.
3. Copy the data into the target volume or PVC.
4. Start OpenRAG with the new storage configuration.
5. Verify partitions and indexed files before removing the old storage.

### Docker Compose: copy host paths into named volumes

Stop the stack (`docker compose down`, **not** `down -v`), then copy each host directory into its named volume. Set `REPO` to your repository root and `PROJECT` to the name you start the stack with (`docker compose -p <project>`):

```bash
# Copy a host directory into a named volume (preserves permissions and dotfiles)
copy() { docker run --rm -v "$1":/from:ro -v "$2":/to alpine sh -c 'cp -a /from/. /to/'; }

REPO=/path/to/openrag     # repository root
PROJECT=openrag           # your `docker compose -p <project>` name

copy $REPO/data                                 ${PROJECT}_appdata
copy $REPO/logs                                 ${PROJECT}_logs
copy $HOME/.cache/huggingface                   ${PROJECT}_modelweights   # optional: cache, can be re-downloaded
copy $REPO/db                                   ${PROJECT}_pgdata
copy $REPO/infra/compose/milvus/volumes/etcd    ${PROJECT}_etcd
copy $REPO/infra/compose/milvus/volumes/minio   ${PROJECT}_minio
copy $REPO/infra/compose/milvus/volumes/milvus  ${PROJECT}_milvus
```

Copy PostgreSQL and all three Milvus volumes (`etcd`, `minio`, `milvus`) as one set — a partial copy leaves the metadata and vector segments inconsistent. To reverse the migration (named volumes → host paths), swap the two `-v` mounts in `copy`.

## Helm deployment

For OpenShift, prefer the Helm chart and configure persistent storage through chart values. The exact StorageClass depends on the cluster.

At minimum, check:

- PostgreSQL persistence
- Milvus persistence
- MinIO persistence
- etcd persistence
- OpenRAG shared app data and model cache persistence

If your OpenShift cluster uses restricted security policies, also verify that the configured images, service accounts, and volume permissions match the cluster policy.

## Operational guidance

Use platform snapshots or PVC backups for infrastructure-level recovery. Use OpenRAG partition backup and restore when you need a portable application-level export.

Avoid mixing host-path and PVC-backed storage for the same deployment unless you are intentionally migrating data.
