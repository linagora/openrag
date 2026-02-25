---
title: Milvus Migrations
---

# Milvus Upgrade
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

## Milvus version upgrade Steps
:::danger[Before running Milvus Version Migration]
These steps must be performed on a deployment running OpenRAG **prior to version 1.1.6** (Milvus 2.5.4) before switching to the newest version of OpenRAG.
:::

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
docker compose up --build milvus -d
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

Now you can switch to the newest release of OpenRAG and it should work fine.

## Schema Migration — Add Temporal Fields

:::info
This migration adds `TIMESTAMPTZ` fields (`datetime`, `created_at`, `updated_at`, `indexed_at`) and their `STL_SORT` indexes to an existing collection.

Existing documents will have `null` for these fields; new documents will have them populated at index time.
:::

:::danger[OpenRAG must be stopped]
Stop the OpenRAG application before running this migration.
:::

### Step 1 — Start only the Milvus container

```bash
docker compose up -d milvus
```

Wait until Milvus is healthy:

```bash
docker compose ps milvus
```

### Step 2 — Dry-run (inspect, no changes)

```bash
docker compose run --no-deps --rm --build --entrypoint "" openrag \
    uv run python scripts/migrations/milvus/1.add_temporal_fields.py --dry-run
```

Review the output to confirm which fields and indexes are missing.

### Step 3 — Apply the migration

```bash
docker compose run --no-deps --rm --build --entrypoint "" openrag \
    uv run python scripts/migrations/milvus/1.add_temporal_fields.py
```

The script will:
1. Add any missing `TIMESTAMPTZ` fields (nullable)
2. Create `STL_SORT` indexes for each field
3. Stamp the collection with `schema_version=1` so OpenRAG no longer reports a migration error on startup

### Step 4 — Restart OpenRAG

```bash
docker compose up --build -d
```

### Rollback

Milvus does not yet support dropping fields. The rollback only removes the indexes and resets the version stamp — the fields remain in the schema but are unused:

```bash
docker compose run --no-deps --rm --build --entrypoint "" openrag \
    uv run python scripts/migrations/milvus/1.add_temporal_fields.py --downgrade
```

To fully remove the fields you would need to recreate the collection from scratch.