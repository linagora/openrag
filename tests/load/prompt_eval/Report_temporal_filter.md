# Temporal Filter Generation Prompt Evaluation (v2)

**Dataset:** [datasets/temporal_filter.json](datasets/temporal_filter.json) (40 cases — 20 positive, 20 negative)
**Scope:** whether the model emits `temporal_filters` when (and only when) it should, and whether the emitted predicates are correct. Decomposition is out of scope (see [Report_query_decomposition.md](Report_query_decomposition.md)).

## Dataset

Each case has the minimal schema `{id, messages, query_with_temporal_filter}`:

| Class | # Cases | Description |
|------|--------:|-------------|
| **Positive** (`true`) | 20 | User restricts by document creation/authoring/publication date. Covers all resolution rules: today, yesterday, this/last week, this/last month, this/last year, past N days/weeks/months, recent/latest, since X, before X, bare MONTH, in YEAR, specific date, exclusion, multi-entity with shared time, plus multi-turn context (3- and 5-message). |
| **Negative** (`false`) | 20 | Filter must be null. Three sub-patterns: (a) dates that describe the topic/subject ("2024 sustainability report", "trends 2020→2025", "2016 US election"); (b) no temporal reference (policy, how-to, trivia); (c) conversational fillers (greetings, thanks). Includes a 5-message negative where the last turn pivots to a pure topic question. |

Document types and verbs are varied on purpose — design specs, incident reports, PRs, commits, lab results, audit logs, invoices, legal briefs, slide decks, meeting minutes, safety bulletins, etc. — so the evaluation does not reduce to "the model learned the word *uploaded*".

## Metrics

Positive class = a filter **was** / **should have been** emitted.

1. **filter_detection_accuracy** — (TP + TN) / N.
2. **filter_detection_precision** — TP / (TP + FP). How often an emitted filter was actually wanted.
3. **filter_detection_recall** — TP / (TP + FN). How often a wanted filter was actually emitted.
4. **filter_detection_f1** — harmonic mean of the two.
5. **filter_correctness** — LLM-as-judge boolean on TP cases only: given the chat history, current date, and generator output JSON, are the predicates correct as a whole (operator, field, ISO values, closed-vs-open intervals, exclusion split)?

Judge is invoked **only on TP** (filter expected and emitted). Precision/recall capture the detection decision; correctness captures the filter body.

## Results

Full raw output: [results/result_filter_generation.json](results/result_filter_generation.json). Judge: `Qwen3-VL-8B-Instruct-FP8`. Current date at eval time: Sunday, April 19, 2026.

**Overall**

| Prompt | Model | Acc | Precision | Recall | F1 | TP / FP / FN / TN | filter_correctness (TP only) |
|---|---|---:|---:|---:|---:|:-:|---:|
| v0 | Mistral-Small-3.1-24B-Instruct-2503 | 75.0% | 100.0% | 50.0% | 66.7% | 10 / 0 / 10 / 20 | 8/10 (80.0%) |
| v0 | Qwen3-VL-8B-Instruct-FP8 | 57.5% | 100.0% | 15.0% | 26.1% | 3 / 0 / 17 / 20 | 2/3 (66.7%) |
| **v1** | **Mistral-Small-3.1-24B-Instruct-2503** | **100.0%** | **100.0%** | **100.0%** | **100.0%** | 20 / 0 / 0 / 20 | **19/20 (95.0%)** |
| v1 | Qwen3-VL-8B-Instruct-FP8 | 92.5% | 87.0% | 100.0% | 93.0% | 20 / 3 / 0 / 17 | 17/20 (85.0%) |

(Numbers above are from a matched-conditions rerun with `DATASET_CURRENT_DATE = "Sunday, April 19, 2026"` pinned in the evaluator. Minor v0/v1 drift vs earlier archived runs reflects generator + LLM-as-judge variance.)

### v0 vs v1

v0 contains no `temporal_filters` rules, so both models default to "no filter" and collapse on recall (Mistral 50%, Qwen 15%). v1's explicit resolution table brings both to 100% recall. No false positives under v0 — the cost of v0 is recall only, not precision.

Exclusion (id 14) illustrates the v0 body failure: without a rule, Mistral emits three contradictory predicates (`>= 2025-04-19 AND < 2025-03-01 AND >= 2025-04-…`); Qwen collapses to the full year including March. v1 fixes this for Mistral.

### v1 — Mistral-Small (winner)

Perfect detection (20/20 TP, 0/20 FP). Two judge-rejected filter bodies among TP:

- **id 20 "Latest safety bulletins"** → emitted a 12-month window `[2025-07-20, 2026-07-20)` extending into the future. Prompt rule is `recent/latest → past 90 days`, expected `[2026-01-19, 2026-04-20)`. Real generator bug.
- **id 18 "Commits pushed since last Monday"** → added a spurious upper bound `< 2026-04-19`. Prompt rule is `since X → one predicate >= X`. Real generator bug.

### v1 — Qwen3-VL-8B

Perfect recall but 2 false positives where a year describes the **content**, not the document creation date:

| id | Query | Bad filter |
|---|---|---|
| 30 | "Findings in the 2024 annual sustainability report." | `created_at ∈ [2024-01-01, 2025-01-01)` |
| 34 | "Effects of climate change on Arctic sea ice between 2010 and 2020." | `created_at ∈ [2010-01-01, 2021-01-01)` |

Cause: v1's topic-vs-creation section is short and its single null example ("Q3 2024 reporting template") does not cover research/historical framings with spanning year ranges.

Two judge-rejected filter bodies among TP:

- **id 18 "Commits pushed since last Monday"** → added a spurious upper bound `< 2026-04-20`. Prompt rule is `since X → one predicate >= X`. Correct rejection.
- **id 9 "Recent SRE incident reports"** → emitted past 10 days instead of past 90. Correct rejection.

## Recommendations

1. **Ship v1 + Mistral-Small-24B as the production pairing** — 100% detection, 90% filter correctness, with the two remaining body bugs on "latest" and "since X" worth a targeted prompt tweak.
2. **Patch v1's topic-vs-creation section.** Add null examples for the two patterns Qwen still trips on: `"findings in the YEAR report"` and `"events in YEAR"` / `"between YEAR1 and YEAR2"` when the year is the subject.
3. **Reinforce `since X` and `latest` rules.** Both Mistral and Qwen emit an unwanted upper bound on "since last Monday"; Mistral emits an over-wide, future-extending window for "latest". A short prompt clarification or an additional example should eliminate these.
4. **Upgrade the judge.** Qwen3-VL-8B as judge is flaky (one call returned `None` on id 8) and occasionally misreads the prompt's resolution rules. A stronger judge would tighten the correctness metric.
