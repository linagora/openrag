# Phase 14 Migration — Model Endpoints & Per-Partition Presets

> **Audience:** operators upgrading an existing OpenRAG deployment to the
> Phase 14 release.
> **TL;DR:** the upgrade is backward compatible. Existing partitions keep
> working untouched; the only new step is a one-time seed of the default model
> endpoints and presets, which the startup path (or `scripts/seed_presets.py`)
> performs automatically.

---

## What changed

Phase 14 introduces two DB-backed registries that replace the previous
"single global config" model:

1. **Model Endpoint Registry** (`model_endpoints` table → `config.models`):
   named endpoints per model type (`embedder`, `reranker`, `llm`, `vlm`).
   Components are built on demand from these named entries by the DI
   component factories.
2. **Per-Partition Preset System** (`pipeline_presets` table →
   `config.presets`): named bundles of pipeline configuration, of two types —
   `indexation` and `retrieval`. A partition references one preset of each type
   by name; the retrieval preset in turn references a reranker/llm *endpoint* by
   name.

A partition row now carries `embedder`, `indexation_preset` and
`retrieval_preset` columns. At startup these names are resolved into a cached
`PartitionConfig` (`config.partitions`) used by the retrieval/indexing
pipelines.

> **Note:** there is no "reranker preset". The reranker is a model *endpoint*.
> The retrieval preset only *references* it via the `reranker` (endpoint name)
> and `enable_reranker` (on/off) fields.

---

## Backward compatibility

The upgrade is designed to be a no-op for existing deployments:

- **Schema:** the Phase 14B migration adds the new columns with
  `server_default="default"`, so every pre-existing partition row automatically
  references the `default` indexation and retrieval presets — no manual
  backfill required. Migrations are idempotent (guarded by inspector existence
  checks), so they are safe to re-run against an already-bootstrapped database.
- **Defaults derived from your current config:** the default endpoints and
  presets are seeded *from your existing global `Settings`* (YAML + env vars).
  For example the default reranker endpoint comes from `RERANKER_ENDPOINT` /
  `reranker.base_url`, and the default retrieval preset inherits your
  `reranker.enabled` kill-switch. So the seeded defaults reproduce your current
  behavior rather than imposing new defaults.
- **Reranker availability:** if reranking is disabled (`RERANKER_ENABLED=false`
  / `reranker.enabled: false`), no reranker endpoint is seeded and the default
  retrieval preset is seeded with `enable_reranker=false`. Deployments without a
  reachable reranker therefore keep working and do not start failing on
  retrieval.

---

## Migration steps

### 1. Apply the upgrade

Deploy the new images / pull the new code as usual. Alembic migrations (run at
app startup) add the new tables and columns idempotently.

### 2. Seed the default endpoints and presets

Seeding reads your current `Settings` and writes one default row per model type
plus the default indexation/retrieval presets and the `default` partition.

Run the one-time utility **inside the application container** (it needs the
project venv and reaches the database over the compose network — Postgres is not
published to the host). From the repo root:

```bash
# GPU deployment: service "openrag"; CPU deployment: service "openrag-cpu"
docker compose -f infra/compose/docker-compose.yaml exec openrag uv run python /app/scripts/seed_presets.py
```

Expected output (counts depend on what is configured in your environment):

```text
Seeded model endpoints: 1 embedder, 1 reranker, 1 llm, 1 vlm
Seeded 1 indexation presets, 1 retrieval presets
Seeded 1 partition(s)
```

The script is **idempotent** — each phase skips any type/preset/partition that
already has rows, so it is safe to run more than once (e.g. after adjusting env
vars and re-seeding only the missing types).

> If a model type has no endpoint configured (e.g. no `VLM_ENDPOINT` and an
> empty `vlm.base_url`), that type is skipped with a log line and simply has no
> seeded default — register one later through the admin API when needed.

### 3. Verify

Run `psql` inside the `rdb` container. The database name is
`partitions_for_collection_<COLLECTION>`, where `<COLLECTION>` is your
`vectordb.collection_name` (e.g. `partitions_for_collection_vdb`). From the repo
root:

```bash
DB=partitions_for_collection_vdb_test   # adjust to your collection name

# Model endpoints — one row per configured type, the seeded one is_default=true
docker compose -f infra/compose/docker-compose.yaml exec rdb psql -U root -d "$DB" \
  -c "SELECT name, model_type, endpoint, is_default FROM model_endpoints;"

# Presets — default indexation + retrieval (plus the multiquery / hyde presets)
docker compose -f infra/compose/docker-compose.yaml exec rdb psql -U root -d "$DB" \
  -c "SELECT name, preset_type FROM pipeline_presets;"

# Partitions reference presets by name
docker compose -f infra/compose/docker-compose.yaml exec rdb psql -U root -d "$DB" \
  -c "SELECT name, embedder, indexation_preset, retrieval_preset FROM partitions;"
```

---

## After migration

- **Change defaults / add endpoints:** use the admin API
  (`/admin/model-endpoints`, `/admin/presets`) rather than editing YAML. Changes
  are persisted to the DB, reloaded into `config.models` / `config.presets`
  atomically, and the stale cached client is evicted so the next request builds
  a fresh client.
- **Per-partition configuration:** create a partition with explicit presets, or
  update an existing one:

  ```bash
  curl -X POST http://localhost:8080/partition/research \
    -H "Authorization: Bearer <AUTH_TOKEN>" -H "Content-Type: application/json" \
    -d '{"indexation_preset": "default", "retrieval_preset": "hyde"}'
  ```

- The global YAML config remains the **source of the seed**: it is read once to
  populate the registries. Ongoing operational changes live in the DB.
