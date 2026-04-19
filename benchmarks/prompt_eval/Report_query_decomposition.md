# Query Reformulation Prompt Evaluation

**Dataset:** [datasets/query_decomposition.json](datasets/query_decomposition.json) (80 cases — 20 D1, 40 D2, 20 D3)
**Scope:** reformulation + decomposition only. Temporal filters are out of scope (see [Report_temporal_filter.md](Report_temporal_filter.md)).

## Dataset

| Tier | # Cases | Description |
|------|--------:|-------------|
| **D1** | 20 | Standalone queries that do **not** need decomposition. |
| **D2** | 40 | Queries that **definitely need** decomposition, 2 to 4 sub-queries. Clear signals: distinct entities, distinct time periods, unrelated dimensions, or exclusions. |
| **D3** | 20 | **Ambiguous** queries. Surface features suggest decomposition (conjunctions, comparisons) but the semantics may require one retrieval or several — e.g. trends, interactions, joint effects, multi-attribute-for-one-subject. |

20 domains (finance, healthcare, legal, engineering, science, HR, education, marketing, real_estate, technology, environment, logistics, agriculture, energy, manufacturing, public_policy, retail, telecommunications, insurance, aviation), each represented 3–5 times across tiers.

Gold labels live in `expected_queries` (shape: `SearchQueries`, so it deserializes directly into the pipeline's Pydantic model). Relative dates in `query` strings are pre-resolved to the dataset's current date (2026-04-17); `temporal_filters` is null throughout.

## Metrics

1. **decomposition_count_matching** — `len(generated.query_list) == len(expected.query_list)`.
2. **decomposition_semantic_coverage** — LLM-as-judge boolean: do the generated sub-queries, taken together, cover every expected sub-query (count-insensitive, order-insensitive)? When coverage is incomplete, the judge returns a short reasoning naming the missing expected sub-query.

Reported slices: overall and per-difficulty (D1 / D2 / D3).

## Results

Full raw output: [results/result_query_decomposition.json](results/result_query_decomposition.json). Judge: `Qwen3-VL-8B-Instruct-FP8`.

**Overall**

| Prompt | Model | count_match | semantic_coverage |
|---|---|---|---|
| v0 | Mistral-Small-3.1-24B-Instruct-2503 | 66/80 (82.5%) | 74/80 (92.5%) |
| v0 | Qwen3-VL-8B-Instruct-FP8 | 58/80 (72.5%) | 69/80 (86.2%) |
| **v1** | **Mistral-Small-3.1-24B-Instruct-2503** | **69/80 (86.2%)** | **76/80 (95.0%)** |
| v1 | Qwen3-VL-8B-Instruct-FP8 | 66/80 (82.5%) | 71/80 (88.8%) |

**Per-difficulty (count_match · semantic_coverage)**

| Prompt | Model | D1 (n=20) | D2 (n=40) | D3 (n=20) |
|---|---|---|---|---|
| v0 | Mistral-Small | 19/20 · 20/20 | 39/40 · 38/40 | **8/20** · 16/20 |
| v0 | Qwen3-VL-8B | 20/20 · 20/20 | 28/40 · 35/40 | 10/20 · 14/20 |
| v1 | Mistral-Small | 16/20 · 20/20 | 38/40 · 38/40 | **15/20** · 18/20 |
| v1 | Qwen3-VL-8B | 20/20 · 20/20 | 35/40 · 36/40 | 11/20 · 15/20 |

### v0 vs v1 — Mistral-Small

- **v1 handles D3 much better than v0** (D3 count_match 8 vs 15): v0 splits "interaction / joint-effect / multi-attribute-for-one-subject" questions that should stay as one (id 68, 69, 71, 75, 79, 80), while v1 keeps them together. v1 also correctly splits the two bounded-range trend cases (id 61, 65) per the "evolution / trend over a bounded range" rule.
- **v1 is slightly weaker on D1** (19 vs 16). Chat-history turns pull prior-turn topics into the reformulation and trigger spurious splits (id 2, 5, 11).
- **Shared failures on both prompts**: id 30 (Lambda/GCF collapsed), id 63 (Kafka/RabbitMQ — prior gRPC turn leaks in), id 66 (HR onboarding under-split), id 70 (air/sea freight), id 74 (EASA/FAA under-split).

### v0 vs v1 — Qwen3-VL-8B

- v1 gains on count_match (+8) and coverage (+2): mostly D2 (28 vs 35) as the explicit split rules embolden Qwen to separate multi-entity/region/time-period queries it previously collapsed.
- Qwen still under-splits comparative questions and misses both bounded-range trend cases (id 61, 65) — it keeps them as one query despite the rule.

### Common hard cases (both models, both prompts)

- **id 30** — "Compare AWS Lambda and Google Cloud Functions" stays as a single comparison query.
- **id 63** — "Kafka vs RabbitMQ": earlier gRPC turn in the history contaminates the reformulation.
- **id 70** — "air freight vs sea freight": emitted as a single comparison instead of two independent lookups.
- **id 74** — "EASA vs FAA certification": same pattern.
