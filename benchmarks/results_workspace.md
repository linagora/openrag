# Workspace Search Benchmark Results

## Approaches

- **Approach A — Milvus ARRAY_CONTAINS**: Each chunk stores a `workspace_ids` ARRAY field. Search filters with `ARRAY_CONTAINS(workspace_ids, "ws")` using an INVERTED index. Writes require upserting every chunk to append a workspace ID.
- **Approach B — PostgreSQL resolution + file_id IN**: Workspace membership is stored in PostgreSQL. At search time, file IDs are resolved via a SQL query, then passed to Milvus as `file_id in [...]`. For >1,000 files, searches are batched in parallel and results merged. Writes are a single SQL INSERT.

## Setup

- **Data**: 5,000 files x 20 chunks = 100,000 chunks in Milvus
- **Vectors**: 1024-dim float32 (4,096 bytes each)
- **Search params**: top_k=10, ef=64
- **Runs**: 5 warmup + 20 measured

## Scenarios

| Scenario | Files | Description |
|----------|-------|-------------|
| S1 | 1 | Baseline minimum latency — single file workspace |
| S2 | 10 | Matches top_k=10 — tiny workspace, no batching |
| S3 | 1,000 | Max size without batching (batch threshold = 1,000) |
| S4 | 2,500 | Medium batching — 3 parallel batches |
| S5 | 5,000 | Worst case — 5 parallel batches, all files |

## Search Results

| Scenario        | Search   |   Files |   Batches |   A median (ms) |   A p95 (ms) |   B total median (ms) |   B total p95 (ms) |   B pg (ms) |   B search (ms) |   B merge (ms) |
|-----------------|----------|---------|-----------|-----------------|--------------|-----------------------|--------------------|-------------|-----------------|----------------|
| S1 (1 file)     | dense    |       1 |         0 |           11.47 |        20.91 |                 14.87 |              20.95 |        2.48 |           10.87 |           0.00 |
| S1 (1 file)     | hybrid   |       1 |         0 |           11.29 |        18.81 |                 14.38 |              19.95 |        2.70 |           10.90 |           0.00 |
| S2 (10 files)   | dense    |      10 |         0 |           65.68 |        80.55 |                 68.38 |              80.85 |        2.81 |           65.38 |           0.00 |
| S2 (10 files)   | hybrid   |      10 |         0 |           71.84 |        82.18 |                 75.42 |              91.51 |        3.04 |           71.80 |           0.00 |
| S3 (1000 files) | dense    |    1000 |         0 |           21.77 |        31.08 |                 33.08 |              40.91 |        4.78 |           27.72 |           0.00 |
| S3 (1000 files) | hybrid   |    1000 |         0 |           22.39 |        30.25 |                 41.03 |              50.26 |        4.68 |           36.10 |           0.00 |
| S4 (2500 files) | dense    |    2500 |         3 |           23.58 |        35.06 |                 61.54 |              85.38 |        8.80 |           53.12 |           0.12 |
| S4 (2500 files) | hybrid   |    2500 |         3 |           25.89 |        30.47 |                 72.13 |              77.67 |        9.21 |           61.78 |           0.13 |
| S5 (5000 files) | dense    |    5000 |         5 |           29.18 |        57.03 |                 84.55 |             106.48 |       16.30 |           65.89 |           0.19 |
| S5 (5000 files) | hybrid   |    5000 |         5 |           30.97 |        39.31 |                 88.01 |             116.68 |       15.64 |           71.79 |           0.19 |

## Write Results (add 1 file / 20 chunks to workspace)

| Approach         |   median (ms) |   mean (ms) |   p95 (ms) |   min (ms) |   max (ms) |
|------------------|---------------|-------------|------------|------------|------------|
| A (ARRAY upsert) |         32.52 |       36.95 |      48.56 |      23.38 |     128.74 |
| B (PG INSERT)    |          2.40 |        2.58 |       3.66 |       2.00 |       4.57 |

## Summary

```
  S1 (1 file)          dense  : A=  11.47ms  B=  14.87ms  -> A wins by 3.40ms (1.3x)
  S1 (1 file)          hybrid : A=  11.29ms  B=  14.38ms  -> A wins by 3.09ms (1.3x)
  S2 (10 files)        dense  : A=  65.68ms  B=  68.38ms  -> A wins by 2.70ms (1.0x)
  S2 (10 files)        hybrid : A=  71.84ms  B=  75.42ms  -> A wins by 3.58ms (1.0x)
  S3 (1000 files)      dense  : A=  21.77ms  B=  33.08ms  -> A wins by 11.31ms (1.5x)
  S3 (1000 files)      hybrid : A=  22.39ms  B=  41.03ms  -> A wins by 18.64ms (1.8x)
  S4 (2500 files)      dense  : A=  23.58ms  B=  61.54ms  -> A wins by 37.96ms (2.6x)
  S4 (2500 files)      hybrid : A=  25.89ms  B=  72.13ms  -> A wins by 46.24ms (2.8x)
  S5 (5000 files)      dense  : A=  29.18ms  B=  84.55ms  -> A wins by 55.37ms (2.9x)
  S5 (5000 files)      hybrid : A=  30.97ms  B=  88.01ms  -> A wins by 57.04ms (2.8x)

  Write: A=32.52ms  B=2.40ms  -> B wins
```
