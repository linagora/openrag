# Workspace Search Benchmark

Compares two approaches for filtering search results by workspace membership:

- **Approach A**: Milvus `ARRAY_CONTAINS(workspace_ids, "ws")` with INVERTED index on an ARRAY field
- **Approach B**: PostgreSQL resolution (`SELECT file_id FROM file_workspaces WHERE ...`) then Milvus `file_id in [...]` with parallel batching for >1000 files

## Quick Start

```bash
cd benchmark
docker compose up -d        # Milvus standalone + PostgreSQL
pip install -r requirements.txt
python benchmark.py          # print to stdout
python benchmark.py -o results.md  # also write to markdown file
```

Subsequent runs automatically skip data insertion if the collection is already populated.

### Teardown

```bash
docker compose down          # Keep volumes (fast restart, data cached)
docker compose down -v       # Remove everything (next run re-inserts data)
```

## Infrastructure

| Service    | Image                       | Port  |
|------------|-----------------------------|-------|
| Milvus     | milvusdb/milvus:v2.6.11     | 19530 |
| PostgreSQL | postgres:16                 | 5433  |
| etcd       | quay.io/coreos/etcd:v3.5.25 | -     |
| MinIO      | minio/minio                 | -     |

PostgreSQL uses port **5433** to avoid conflicts with any existing instance on 5432.

## What It Measures

### Data Setup

- 5,000 files x 20 chunks = **100,000 chunks** in Milvus
- Random 1024-dim unit vectors + random text (for BM25)
- Collection schema matches OpenRAG production (HNSW COSINE, BM25 sparse, INVERTED indexes)
- Data is inserted once and cached across runs (checked via entity count)

### Scenarios

| Scenario | Workspace    | Files | Batches | Purpose                    |
|----------|--------------|-------|---------|----------------------------|
| S1       | `ws_single`  | 1     | 0       | Baseline minimum latency   |
| S2       | `ws_5000`    | 5,000 | 5       | Worst case, heavy batching |
| S3       | `ws_2500_a`  | 2,500 | 3       | Medium batching            |
| S4       | `ws_1000_a`  | 1,000 | 0       | Max size without batching  |

### Search Types

Each scenario is tested with both:
- **Dense only** (HNSW COSINE)
- **Hybrid** (HNSW + BM25 with RRFRanker)

### Batching (Approach B)

When file_ids exceed 1,000, they are split into batches of 1,000 and searched **in parallel** using threads. Results are then merged (deduplicated by `_id`, sorted by score, top-k selected).

### Write Benchmark

- **Approach A**: Query 20 chunks by file_id, append workspace_id to array, full upsert
- **Approach B**: Single `INSERT INTO file_workspaces`

### Repetitions

- 5 warm-up queries (discarded) per search path (both approaches, both search types)
- 20 measured runs per scenario
- Stats: median, mean, p95, min, max (in milliseconds)

## Output

The script prints three tables to stdout:

1. **Search results** — per-scenario comparison with timing breakdown (Approach B splits into PG resolution, Milvus search, and merge times)
2. **Write results** — upsert vs INSERT latency
3. **Summary** — winner per scenario with absolute and relative difference

Use `-o results.md` to also write the tables to a markdown file.
