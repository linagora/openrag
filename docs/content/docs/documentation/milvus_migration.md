---
title: Milvus Migrations
---

# Milvus Version Migration
OpenRAG has been upgraded from Milvus **2.5.4** to **2.6.11** to leverage the enhancements introduced in the latest releases, particularly the new temporal querying capabilities added in version **2.6.6+**.

## What's New in 2.6.x

Milvus 2.6.6+ introduced the **`TIMESTAMPTZ`** field type, which enables:

- **Comparison and range filtering** using standard operators (`=`, `!=`, `<`, `>`, etc.)
- **Interval arithmetic** — add or subtract durations (days, hours, minutes) directly in filter expressions
- **Time-based indexing** for faster temporal queries
- **Combined filtering** — pair timestamp conditions with vector similarity search

**Example — basic comparison:**
```python
expr = "tsz != ISO '2025-01-03T00:00:00+08:00'"
results = client.query(
    collection_name, 
    filter=expr,
    output_fields=["id", "tsz"],
    limit=10
)
```

**Example — interval arithmetic:**
```python
expr = "tsz + INTERVAL 'P1D' > ISO '2025-01-03T00:00:00+08:00'"
results = client.query(
    collection_name,
    filter=expr,
    output_fields=["id", "tsz"], 
    limit=10
)
```

> `INTERVAL` values follow [ISO 8601 duration](https://en.wikipedia.org/wiki/ISO_8601#Durations) syntax: 
> * `P1D` = 1 day
> * `PT3H` = 3 hours
> * `P2DT6H` = 2 days and 6 hours.

## Current State

:::info
Temporal fields are currently stored as **strings**, not **`TIMESTAMPTZ`**. Migrating to `TIMESTAMPTZ` requires a schema and index change, and Milvus doesn't support migrations on schema and index changes: it has to be handled manually.

Until a migration strategy is define, filtering still works via **lexicographic string comparison** on ISO 8601 strings:
```python
expr = "tsz != '2025-01-03T00:00:00+08:00'"  # No ISO/INTERVAL keywords
results = client.query(
    collection_name,
    filter=expr,
    output_fields=["id", "tsz"],
    limit=10
)
```
Full `TIMESTAMPTZ` support will be activated in a future release once the migration is established.
:::

## Milvus Update Steps
These steps must be performed on a deployment running OpenRAG **prior to version 1.1.6** (Milvus 2.5.4).

> For the full official reference, see the [Milvus upgrade guide](https://milvus.io/docs/upgrade_milvus_standalone-docker.md#Upgrade-process).

### Step 1 — Upgrade to Milvus 2.5.16 first

Milvus requires an intermediate upgrade to **v2.5.16** before jumping to 2.6.x. 

Edit `vdb/milvus.yaml` and set the Milvus image tag:

```diff lang=yaml
// vdb/milvus.yaml
milvus:
-  image: milvusdb/milvus:v2.5.4
+  image: milvusdb/milvus:v2.5.16 # Migrate to milvus 2.5.16
```

Then restart the stack:

```bash
docker compose down
docker compose up -d
```

Wait for all services to be healthy before continuing.

### Step 2 — Upgrade to Milvus 2.6.11

Update `vdb/milvus.yaml` with the target versions (MinIO must also be updated for compatibility):

```diff lang=yaml
// vdb/milvus.yaml
minio:
-  image: minio/minio:RELEASE.2023-03-20T20-16-18Z
+  image: minio/minio:RELEASE.2024-12-18T13-15-44Z

...
milvus:
-  image: milvusdb/milvus:v2.5.16
+  image: milvusdb/milvus:v2.6.11
```

### Step 3 — Stop all services

```bash
docker compose down
```

Verify that all containers are stopped before proceeding:

```bash
docker ps | grep milvus
```

### Step 4 — Start with the new image

```bash
docker compose up -d
```

Once healthy, confirm the running version:

```bash
docker inspect milvus-standalone --format '{{ .Config.Image }}'
# Expected: milvusdb/milvus:v2.6.11
```