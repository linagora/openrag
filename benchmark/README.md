# Workspace Search Benchmark

Compares two approaches for filtering search results by workspace membership:

- **Approach A**: Milvus `ARRAY_CONTAINS(workspace_ids, "ws")` with INVERTED index on an ARRAY field
- **Approach B**: PostgreSQL resolution (`SELECT file_id FROM file_workspaces WHERE ...`) then Milvus `file_id in [...]` with batching for >1000 files

## Quick Start

```bash
cd benchmark
docker compose up -d        # Milvus standalone + PostgreSQL
pip install -r requirements.txt
python benchmark.py
```

Teardown:

```bash
docker compose down -v       # Remove containers and volumes
```

## Infrastructure

| Service    | Image                    | Port  |
|------------|--------------------------|-------|
| Milvus     | milvusdb/milvus:v2.6.11  | 19530 |
| PostgreSQL | postgres:16              | 5433  |
| etcd       | quay.io/coreos/etcd:v3.5.25 | -  |
| MinIO      | minio/minio              | -     |

PostgreSQL uses port **5433** to avoid conflicts with any existing instance on 5432.

## What It Measures

### Data Setup

- 5,000 files x 20 chunks = **100,000 chunks** in Milvus
- Random 1024-dim unit vectors + random text (for BM25)
- Collection schema matches OpenRAG production (HNSW COSINE, BM25 sparse, INVERTED indexes)

### Scenarios

| Scenario | Workspace | Files | Batches (B) | Purpose |
|----------|-----------|-------|-------------|---------|
| S1 | `ws_single` | 1 | 0 | Baseline minimum latency |
| S2 | `ws_5000` | 5,000 | 5 | Worst case, heavy batching |
| S3 | `ws_2500_a` | 2,500 | 3 | Medium batching |
| S4 | `ws_1000_a` | 1,000 | 0 | Max size without batching |

### Search Types

Each scenario is tested with both:
- **Dense only** (HNSW COSINE)
- **Hybrid** (HNSW + BM25 with RRFRanker)

### Write Benchmark

- **Approach A**: Query 20 chunks by file_id, append workspace_id to array, `partial_update=True` upsert
- **Approach B**: Single `INSERT INTO file_workspaces`

### Repetitions

- 5 warm-up queries (discarded) per search path
- 20 measured runs per scenario
- Stats: median, mean, p95, min, max (in milliseconds)

## Output

The script prints three tables:

1. **Search results** — per-scenario comparison with timing breakdown (Approach B splits into PG resolution, Milvus search, and merge times)
2. **Write results** — upsert vs INSERT latency
3. **Summary** — winner per scenario with absolute and relative difference
