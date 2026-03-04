# Benchmarks

Performance benchmarks for OpenRAG. Each benchmark is a standalone script that shares the same Docker infrastructure (Milvus + PostgreSQL).

## Quick Start

```bash
cd benchmarks
docker compose up -d        # Milvus standalone + PostgreSQL
pip install -r requirements.txt
```

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

## Available Benchmarks

### Workspace Search (`workspace.py`)

Compares two approaches for filtering search results by workspace membership:

- **Approach A — Milvus ARRAY_CONTAINS**: Each chunk stores a `workspace_ids` ARRAY field with an INVERTED index. Search filters with `ARRAY_CONTAINS(workspace_ids, "ws")`. Writes require upserting every chunk to append a workspace ID.
- **Approach B — PostgreSQL resolution + file_id IN**: Workspace membership is stored in PostgreSQL. At search time, file IDs are resolved via SQL, then passed to Milvus as `file_id in [...]`. For >1,000 files, searches are batched in parallel and results merged. Writes are a single SQL INSERT.

```bash
python workspace.py                              # print to stdout
python workspace.py -o results_workspace.md      # also write to file
```

**Scenarios**: 6 scenarios — realistic workspace sizes (10 to 5,000 files) plus a no-batching variant for 5,000 files. Tested with both dense (HNSW COSINE) and hybrid (HNSW + BM25) search, plus a write benchmark.

**Data**: 5,000 files x 20 chunks = 100,000 chunks. Inserted once and cached across runs.

Results: [`results_workspace.md`](results_workspace.md)
