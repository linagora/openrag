# Workspace Search Benchmark Results

## Search Results

| Scenario        | Search   |   Files |   Batches |   A median (ms) |   A p95 (ms) |   B total median (ms) |   B total p95 (ms) |   B pg (ms) |   B search (ms) |   B merge (ms) |
|-----------------|----------|---------|-----------|-----------------|--------------|-----------------------|--------------------|-------------|-----------------|----------------|
| S1 (1 file)     | dense    |       1 |         0 |            6.30 |         7.10 |                  7.71 |               8.55 |        1.24 |            6.44 |           0.00 |
| S1 (1 file)     | hybrid   |       1 |         0 |            6.48 |         7.31 |                  7.37 |               8.50 |        1.14 |            6.20 |           0.00 |
| S2 (5000 files) | dense    |    5000 |         5 |           15.52 |        17.17 |                 34.36 |              42.19 |        3.90 |           29.13 |           0.08 |
| S2 (5000 files) | hybrid   |    5000 |         5 |           12.85 |        16.06 |                 33.71 |              44.39 |        3.41 |           28.81 |           0.07 |
| S3 (2500 files) | dense    |    2500 |         3 |            8.65 |         9.44 |                 18.74 |              24.02 |        2.19 |           16.47 |           0.05 |
| S3 (2500 files) | hybrid   |    2500 |         3 |           12.93 |        15.58 |                 32.18 |              37.73 |        3.22 |           28.94 |           0.06 |
| S4 (1000 files) | dense    |    1000 |         0 |            9.38 |        13.46 |                 13.29 |              17.54 |        1.56 |           11.72 |           0.00 |
| S4 (1000 files) | hybrid   |    1000 |         0 |            9.18 |         9.94 |                 13.91 |              15.24 |        1.46 |           12.48 |           0.00 |

## Write Results (add 1 file / 20 chunks to workspace)

| Approach         |   median (ms) |   mean (ms) |   p95 (ms) |   min (ms) |   max (ms) |
|------------------|---------------|-------------|------------|------------|------------|
| A (ARRAY upsert) |          8.92 |        9.20 |      10.91 |       7.57 |      11.06 |
| B (PG INSERT)    |          0.83 |        1.36 |       1.65 |       0.63 |      10.86 |

## Summary

```
  S1 (1 file)          dense  : A=   6.30ms  B=   7.71ms  -> A wins by 1.41ms (1.2x)
  S1 (1 file)          hybrid : A=   6.48ms  B=   7.37ms  -> A wins by 0.89ms (1.1x)
  S2 (5000 files)      dense  : A=  15.52ms  B=  34.36ms  -> A wins by 18.84ms (2.2x)
  S2 (5000 files)      hybrid : A=  12.85ms  B=  33.71ms  -> A wins by 20.86ms (2.6x)
  S3 (2500 files)      dense  : A=   8.65ms  B=  18.74ms  -> A wins by 10.09ms (2.2x)
  S3 (2500 files)      hybrid : A=  12.93ms  B=  32.18ms  -> A wins by 19.25ms (2.5x)
  S4 (1000 files)      dense  : A=   9.38ms  B=  13.29ms  -> A wins by 3.91ms (1.4x)
  S4 (1000 files)      hybrid : A=   9.18ms  B=  13.91ms  -> A wins by 4.73ms (1.5x)

  Write: A=8.92ms  B=0.83ms  -> B wins
```
