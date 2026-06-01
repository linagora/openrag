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
| S1 | 10 | Personal workspace |
| S2 | 100 | Small team |
| S3 | 1,000 | Large team (batching boundary) |
| S4 | 2,500 | Department — 3 parallel batches |
| S5 | 5,000 | Organization-wide — 5 parallel batches |
| S6 | 5,000 | Same as S5 but single query (no batching) |

## Search Results

| Scenario                  | Search   |   Files |   Batches |   A median (ms) |   A p95 (ms) |   B total median (ms) |   B total p95 (ms) |   B pg (ms) |   B search (ms) |   B merge (ms) |
|---------------------------|----------|---------|-----------|-----------------|--------------|-----------------------|--------------------|-------------|-----------------|----------------|
| S1 (10 files)             | dense    |      10 |         0 |           18.84 |        26.48 |                 21.27 |              28.61 |        4.57 |           15.20 |           0.00 |
| S1 (10 files)             | hybrid   |      10 |         0 |           17.34 |        40.97 |                 19.33 |              63.43 |        3.45 |           15.45 |           0.00 |
| S2 (100 files)            | dense    |     100 |         0 |           18.49 |        29.14 |                 24.46 |              37.67 |        3.97 |           17.85 |           0.00 |
| S2 (100 files)            | hybrid   |     100 |         0 |           24.34 |        41.57 |                 37.55 |             144.35 |        9.27 |           26.78 |           0.00 |
| S3 (1000 files)           | dense    |    1000 |         0 |           60.68 |       129.40 |                 80.42 |             133.52 |       10.45 |           68.70 |           0.00 |
| S3 (1000 files)           | hybrid   |    1000 |         0 |           52.00 |        73.01 |                 73.41 |             133.44 |        9.04 |           65.48 |           0.00 |
| S4 (2500 files)           | dense    |    2500 |         3 |           81.26 |       193.33 |                116.97 |             339.26 |       12.29 |           99.97 |           0.16 |
| S4 (2500 files)           | hybrid   |    2500 |         3 |           54.44 |        87.58 |                 91.15 |             147.90 |       12.68 |           76.56 |           0.17 |
| S5 (5000 files)           | dense    |    5000 |         5 |           22.34 |        31.47 |                 46.93 |              92.89 |       10.52 |           37.68 |           0.16 |
| S5 (5000 files)           | hybrid   |    5000 |         5 |           13.37 |        17.19 |                 28.91 |              60.81 |        4.86 |           24.08 |           0.07 |
| S6 (5000 files, no batch) | dense    |    5000 |         0 |           16.56 |        22.19 |                 40.96 |              56.06 |        6.37 |           33.70 |           0.00 |
| S6 (5000 files, no batch) | hybrid   |    5000 |         0 |           11.88 |        13.70 |                 31.18 |              41.00 |        3.83 |           27.18 |           0.00 |

## Write Results (add 1 file / 20 chunks to workspace)

| Approach         |   median (ms) |   mean (ms) |   p95 (ms) |   min (ms) |   max (ms) |
|------------------|---------------|-------------|------------|------------|------------|
| A full upsert    |         10.06 |       10.31 |      12.69 |       8.59 |      16.51 |
| A partial update |          9.18 |        9.66 |      12.41 |       7.83 |      15.54 |
| B (PG INSERT)    |          0.75 |        0.94 |       1.73 |       0.62 |       1.87 |

## Summary

```
  S1 (10 files)        dense  : A=  18.84ms  B=  21.27ms  -> A wins by 2.43ms (1.1x)
  S1 (10 files)        hybrid : A=  17.34ms  B=  19.33ms  -> A wins by 1.99ms (1.1x)
  S2 (100 files)       dense  : A=  18.49ms  B=  24.46ms  -> A wins by 5.97ms (1.3x)
  S2 (100 files)       hybrid : A=  24.34ms  B=  37.55ms  -> A wins by 13.21ms (1.5x)
  S3 (1000 files)      dense  : A=  60.68ms  B=  80.42ms  -> A wins by 19.74ms (1.3x)
  S3 (1000 files)      hybrid : A=  52.00ms  B=  73.41ms  -> A wins by 21.41ms (1.4x)
  S4 (2500 files)      dense  : A=  81.26ms  B= 116.97ms  -> A wins by 35.71ms (1.4x)
  S4 (2500 files)      hybrid : A=  54.44ms  B=  91.15ms  -> A wins by 36.71ms (1.7x)
  S5 (5000 files)      dense  : A=  22.34ms  B=  46.93ms  -> A wins by 24.59ms (2.1x)
  S5 (5000 files)      hybrid : A=  13.37ms  B=  28.91ms  -> A wins by 15.54ms (2.2x)
  S6 (5000 files, no batch) dense  : A=  16.56ms  B=  40.96ms  -> A wins by 24.40ms (2.5x)
  S6 (5000 files, no batch) hybrid : A=  11.88ms  B=  31.18ms  -> A wins by 19.30ms (2.6x)

  Write: A(full)=10.06ms  A(partial)=9.18ms  B=0.75ms
```
