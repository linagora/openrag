# Workspace Search Benchmark Results

## Search Results

| Scenario        | Search   |   Files |   Batches |   A median (ms) |   A p95 (ms) |   B total median (ms) |   B total p95 (ms) |   B pg (ms) |   B search (ms) |   B merge (ms) |
|-----------------|----------|---------|-----------|-----------------|--------------|-----------------------|--------------------|-------------|-----------------|----------------|
| S1 (1 file)     | dense    |       1 |         0 |           18.42 |        30.07 |                 21.34 |              34.51 |        4.76 |           14.94 |           0.00 |
| S1 (1 file)     | hybrid   |       1 |         0 |           18.88 |        26.21 |                 22.05 |              29.85 |        5.01 |           17.13 |           0.00 |
| S2 (5000 files) | dense    |    5000 |         5 |           39.71 |        57.80 |                 89.16 |             117.85 |       18.06 |           69.37 |           0.25 |
| S2 (5000 files) | hybrid   |    5000 |         5 |           31.32 |        43.22 |                 70.15 |             108.43 |       12.36 |           57.42 |           0.19 |
| S3 (2500 files) | dense    |    2500 |         3 |           19.32 |        26.75 |                 29.46 |              39.85 |        4.47 |           24.38 |           0.08 |
| S3 (2500 files) | hybrid   |    2500 |         3 |           14.55 |        17.71 |                 21.44 |              27.29 |        2.90 |           18.31 |           0.05 |
| S4 (1000 files) | dense    |    1000 |         0 |           10.21 |        12.46 |                 14.80 |              15.79 |        1.71 |           12.96 |           0.00 |
| S4 (1000 files) | hybrid   |    1000 |         0 |           10.59 |        11.91 |                 16.41 |              17.63 |        1.75 |           14.59 |           0.00 |

## Write Results (add 1 file / 20 chunks to workspace)

| Approach         |   median (ms) |   mean (ms) |   p95 (ms) |   min (ms) |   max (ms) |
|------------------|---------------|-------------|------------|------------|------------|
| A (ARRAY upsert) |         11.01 |       12.04 |      15.02 |       8.38 |      25.28 |
| B (PG INSERT)    |          1.86 |        1.90 |       2.33 |       1.49 |       2.65 |

## Summary

```
  S1 (1 file)          dense  : A=  18.42ms  B=  21.34ms  -> A wins by 2.92ms (1.2x)
  S1 (1 file)          hybrid : A=  18.88ms  B=  22.05ms  -> A wins by 3.17ms (1.2x)
  S2 (5000 files)      dense  : A=  39.71ms  B=  89.16ms  -> A wins by 49.45ms (2.2x)
  S2 (5000 files)      hybrid : A=  31.32ms  B=  70.15ms  -> A wins by 38.83ms (2.2x)
  S3 (2500 files)      dense  : A=  19.32ms  B=  29.46ms  -> A wins by 10.14ms (1.5x)
  S3 (2500 files)      hybrid : A=  14.55ms  B=  21.44ms  -> A wins by 6.89ms (1.5x)
  S4 (1000 files)      dense  : A=  10.21ms  B=  14.80ms  -> A wins by 4.59ms (1.4x)
  S4 (1000 files)      hybrid : A=  10.59ms  B=  16.41ms  -> A wins by 5.82ms (1.5x)

  Write: A=11.01ms  B=1.86ms  -> B wins
```
