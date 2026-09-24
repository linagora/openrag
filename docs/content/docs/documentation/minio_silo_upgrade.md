---
title: Object storage upgrade (MinIO to SILO)
---

Milvus keeps its segments in an S3-compatible object store. OpenRAG used to ship MinIO `RELEASE.2024-12-18T13-15-44Z` for it. MinIO no longer publishes images or security fixes, and that build is affected by CVE-2026-40344 and CVE-2026-41145: anyone who knows an access key can write objects without signing the request.

OpenRAG now ships [SILO](https://silo.pgsty.com/), the maintained MinIO fork by PGSTY, as `pgsty/silo:RELEASE.2026-09-16T00-00-00Z`, pinned by digest, for amd64 and arm64. SILO fixes both CVEs, speaks the same S3 API, and opens an existing MinIO volume in place. If that image ever becomes unavailable, `linagoraai/silo` holds a copy with the same digest: only the repository name changes.

Fresh installs need nothing more. Existing deployments must read the next section before upgrading.

## Before you upgrade: the switch is one-way

SILO writes new objects in a metadata format the old MinIO build cannot read. Once SILO has written anything, going back to the old image makes Milvus fail on those segments (`decodeXLHeaders: Unknown xl meta version 3`) and OpenRAG searches return errors. Switching forward again recovers everything, but the only way back to the old image is a backup.

Stop writes and back up the MinIO, etcd and Milvus data together, as described in [Backup and restore](/openrag/documentation/backup_restore/) and [storage layout](/openrag/documentation/openshift_deployment/). To roll back, restore that backup with the previous release.

## Docker Compose

The Compose stack pulls the new image; nothing else changes, and your `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` stay as they are.

```bash
cd infra/compose
docker compose pull minio
docker compose up -d minio
docker compose restart milvus
```

Recreating only `minio` is enough: Milvus reconnects on its own, and the restart makes it reload every segment through SILO, which is the quickest way to confirm the upgrade.

## Helm

The bundled MinIO's image changes, and that needs **every MinIO pod stopped before any new one starts**: a distributed MinIO refuses peers running a different binary, so a normal rolling update leaves the cluster unable to read or write until the last pod has switched.

To upgrade an existing release:

1. Stop writes and back up the MinIO and etcd volumes (see above).
2. Stop every MinIO pod and wait until none is left, before upgrading. Upgrading first and deleting the pods afterwards leaves old and new pods running side by side: the rolling update starts as soon as the chart is applied, and the StatefulSet recreates each deleted pod as soon as it is gone.

   ```bash
   kubectl scale statefulset -n <namespace> <release>-minio --replicas=0
   kubectl wait pod -n <namespace> -l app=minio,release=<release> --for=delete --timeout=5m
   ```

3. Run `helm upgrade` with the new chart. It scales MinIO back to its configured replica count, with every pod on SILO.
4. Wait for MinIO to become ready. Milvus reconnects on its own; restarting it makes it reload every segment through SILO.

   ```bash
   kubectl get pods -n <namespace> -l app=minio,release=<release> \
     -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.containers[0].image}{"\n"}{end}'
   # Expected image: pgsty/silo:RELEASE.2026-09-16T00-00-00Z@sha256:635197cb9f36d01bee221d34d1c7d7960f6a95c48b0b6c01d99cd13bdae51a46
   kubectl get pods -n <namespace> -l app.kubernetes.io/name=milvus
   ```

Plan a maintenance window: search and indexing are unavailable while MinIO and Milvus restart.

Deployments that use an external S3 store (`milvus.minio.enabled: false`) are not affected.
