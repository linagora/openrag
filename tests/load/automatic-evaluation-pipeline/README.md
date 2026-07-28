# Automatic Evaluation Pipeline

End-to-end harness for measuring an OpenRAG deployment on retrieval **and** generation quality, with a Streamlit dashboard for browsing runs and trends.

The pipeline runs in two modes:

- **Self-bootstrapping** — upload your own PDFs to OpenRAG, cluster the resulting chunks, and ask an LLM to write a synthetic Q/A dataset against them (with an LLM-critic quality gate and question-type shaping). Then benchmark.
- **Golden dataset** — bring your own curated `dataset.json` and skip straight to benchmarking. Assumes the documents are already indexed in the target partition.

Either mode can be run two ways:

- **Against a live instance** — point the scripts (`upload_files.py` → `generate_questions.py` → `benchmark.py`) at an OpenRAG you already have running.
- **Orchestrated, per version** — let `orchestrator.py` deploy a *specific* OpenRAG git version in an isolated compose project, ingest + benchmark it, then tear it down and reclaim disk. Each run lands in its own `reports/<uuid>/` folder so the same dataset can be scored across versions/configs and compared in the dashboard.

---

## Layout

```
automatic-evaluation-pipeline/
├── upload_files.py         # Index pdf_files/ into OpenRAG; (optionally) trigger SCORE analysis
├── generate_questions.py   # Cluster chunks, generate typed Q/A pairs, critic-filter them, write a manifest
├── benchmark.py            # Run the question set through OpenRAG and score every metric
├── orchestrator.py         # Deploy one OpenRAG git version, ingest + benchmark it, then tear it down
├── context_ablation.py     # Side-by-side: with-context (RAG) vs without-context (closed-book)
├── dashboard.py            # Streamlit UI: browse/compare runs (incl. across versions), trigger new ones
├── config.py               # Central tunable defaults (the CONFIG object) shared by all scripts
├── evaluation_prompts.py   # All judge / generator system prompts (FR + EN variants)
├── judge_schemas.py        # Pydantic schemas for structured judge output
├── metrics.py              # ROUGE / BLEU / METEOR + hit@k / MRR / nDCG / MAP / R-precision / recall@k
├── run_all.sh              # Convenience script: upload + generate + benchmark, or benchmark a golden set
├── dataset.json            # Generated or hand-curated Q/A dataset (gitignored)
├── dataset.manifest.json   # Sidecar provenance for a generated dataset (config snapshot + counts)
├── pdf_files/              # Source documents to index (gitignored)
├── golden_uploads/         # Goldens uploaded via the dashboard (gitignored)
├── reports/                # Per-run artifacts — flat (live-instance) or reports/<uuid>/ (orchestrated); gitignored
├── runs/                   # Per-run bind-mount data for orchestrated deploys; rm -rf'd on teardown (gitignored)
└── assets/                 # Metric-explanation diagrams used in this README
```

---

## Setup

### Dependencies

The module shares the parent project's virtualenv. Beyond the parent project, you need:

```
httpx loguru python-dotenv tqdm openai langchain-openai pydantic
pandas numpy scikit-learn
umap-learn hdbscan
nltk rouge-score
streamlit altair
pypdf python-docx python-pptx
```

(`altair` is used by `dashboard.py` for its charts — the dashboard won't start without it.)

(NLTK corpora `punkt`, `punkt_tab`, `wordnet`, `omw-1.4` are auto-downloaded on first use by `metrics.py`.)

### `.env`

The scripts read these from `.env`:

| Var | Read by | Purpose |
|-----|---------|---------|
| `BASE_URL` | generate, benchmark, ablation | Base URL of the **judge / generator** LLM (OpenAI-compatible endpoint) |
| `API_KEY` | generate, benchmark, ablation | API key for that endpoint |
| `MODEL` | generate, benchmark, ablation | Model name on that endpoint |
| `API_BASE_URL` | all scripts | Base URL of the **OpenRAG instance under test** — the default for `--base-url`. CLI `--base-url` overrides it. |
| `AUTH_TOKEN` | all scripts | Bearer token for OpenRAG (chat, search, chunk-fetch). `upload_files.py` falls back to `sk-1234` if unset; the others send it as-is. |
| `JUDGE_MODEL` / `JUDGE_BASE_URL` / `JUDGE_API_KEY` | benchmark | (optional) Point the judge at a **separate** endpoint. Each falls back to `MODEL` / `BASE_URL` / `API_KEY`. |
| `CRITIC_MODEL` / `CRITIC_BASE_URL` / `CRITIC_API_KEY` | generate | (optional) Point the generation quality-critic at a **separate** endpoint. Each falls back to `MODEL` / `BASE_URL` / `API_KEY`. |
| `SCORE_BASE_URL` / `SCORE_TOKEN` / `SCORE_API` | upload | (optional) SCORE corpus-analysis API base URL / bearer token / endpoint path. Only `upload_files.py` uses these. |
| `EVAL_RUN_TAG` | benchmark | (optional) Fallback for `--tag`: label appended to report filenames (`eval_<ts>_<tag>.*`). |
| `GOLDEN_DATASET` | benchmark, ablation | (optional) Path to a golden dataset, picked up by `run_all.sh` and as the fallback for `--dataset`. |

Everything else — partition, paths, sampling params, concurrency, timeouts — lives in `config.py` (see below). Only URLs, API keys, tokens, and a couple of `config.py` defaults come from `.env`. A few `config.py` values also honour an env override: `PARTITION` (→ `common.partition`), `UPLOAD_DIR` (→ `upload.dir_path`), and `UPLOAD_MAX_FILES` (→ `upload_files.py --max-files`).

### Configuration (`config.py`)

All tunable behaviour lives in `config.py` as a single `CONFIG` object built from nested dataclasses. Edit the defaults there — URLs, API keys, and tokens stay in `.env`.

| Section | Used by | Key knobs |
|---------|---------|-----------|
| `common` | all scripts | `partition`, `dataset_path`, `output_dir` |
| `benchmark` | `benchmark.py` | target/judge sampling, concurrency, `label_language`, `cot_audit_fraction`, `faithfulness_fraction`, `limit`, `ablation_limit`, `retrieval_trace_top_k`, `retrieval_trace_snippet_chars`, `classify_document_categories` (+ `document_category_*`), value-answer numeric grounding (`value_answer_types`, `numeric_rel_tol`, `numeric_abs_tol`), `retriever_type` (provenance label) |
| `ablation` | `context_ablation.py` | `temperature`, `timeout`, `limit`, concurrency, `csv_name` |
| `question_gen` | `generate_questions.py` | `profile` (`distribution` \| `capability_suite`), gen sampling, question mix, `language` (en/fr), `schema_version` / `prompt_version`, `clustering` (method + UMAP / KMeans / DBSCAN params), `filtration` (critic thresholds + retries), `typing` (question-type distribution), `chunk_filter` (drop boilerplate/bibliography chunks), `capability_suite` (per-capability counts), `classify_document_categories` (+ `document_category_*`) |
| `upload` | `upload_files.py` | `dir_path`, retries, timeouts, SCORE poll interval / timeout |

Every script accepts `--partition` to override `common.partition` for a single run. `benchmark.py` and `context_ablation.py` additionally take `--base-url`, `--dataset`, `--output-dir`, and `--limit`. `benchmark.py` also takes `--no-retrieval` (skip ID-based retrieval metrics *and* the raw retriever ranking trace), `--ablation` / `--ablation-limit` (run the context-contribution ablation inline after the benchmark; see §5), `--tag` (label appended to report filenames), and `--retriever-type` (provenance label only — see §6). `orchestrator.py` is driven entirely by CLI flags (below), reading only `common.partition` / `common.dataset_path` as defaults.

---

## Workflow

### 1. Index documents

Drop PDFs / DOCX / PPTX / TXT / MD into `pdf_files/` and run:

```bash
python upload_files.py [--partition *your_partition*]
```

This uploads each file under a content-hash file id (so re-runs are idempotent) and, if a `SCORE_TOKEN` is set, also kicks off an asynchronous SCORE analysis + audit and writes the latest available corpus score to `reports/score.csv`.

> The partition defaults to `common.partition` in `config.py` and can be overridden with `--partition`. The source directory (`upload.dir_path`) is also in `config.py` and can be overridden with `--path` (env `UPLOAD_DIR`); cap ingestion with `--max-files N` (env `UPLOAD_MAX_FILES`). The OpenRAG base URL comes from the `API_BASE_URL` env var.

### 2. Generate a Q/A dataset

```bash
python generate_questions.py [--partition *your_partition*]
```

This is the default **`distribution`** profile. Pulls every chunk from the chosen partition via `/partition/{p}/chunks`, drops low-information chunks (bibliographies, acknowledgements, copyright boilerplate — deterministic, no LLM, via `question_gen.chunk_filter`), reduces the embeddings with UMAP, clusters with HDBSCAN (KMeans / DBSCAN also available — set `question_gen.clustering.method` in `config.py`), then samples chunks per cluster and asks the LLM to produce:

- a **question** answerable from those chunks, plus a reference **answer**, **or**
- an **unanswerable** topic-adjacent question (no chunks attached, ground-truth answer is a refusal string) — used to measure abstention vs. hallucination.

Two shaping passes run on top of raw generation (both under `question_gen` in `config.py`, both toggleable):

- **Typing** (`typing`) — each answerable question is assigned a type sampled from `typing.distribution` (`single_hop_specific`, `multi_hop_specific`, `reasoning`, `comparative`), which steers difficulty and the number of chunks/hops.
- **Filtration** (`filtration`) — an LLM *critic* scores each item on answerability / self-containment / clarity (and faithfulness for answerable rows). Items below the thresholds are regenerated up to `max_quality_retries` times; if `drop_failed` is set they're excluded from the dataset (counted as `failed_critic` in the manifest).

Per-cluster volume (`n_questions_per_cluster`, `n_unanswerable_per_cluster`), the sampled-chunk range (`n_min` / `n_max`), and the question language (`language`, `en` / `fr`) are all under `question_gen` in `config.py`.

**Capability-suite profile (`--profile capability_suite`).** Instead of "N per cluster from a type distribution", this second profile generates a target number of questions **per capability**, each drawn from capability-appropriate chunks: `table_lookup`, `multi_hop_retrieval`, `cross_document_reasoning`, `citation_grounding`, `unanswerable`, `long_context_retrieval`, `numerical_reasoning`. It loops (over-generating to absorb critic rejections, capped at `capability_suite.max_rounds`) until each capability hits its count. Set the count with `--per-category N` (or `capability_suite.per_category` / `count_overrides` in `config.py`; set a capability's count to 0 to skip it). The two profiles live side by side — neither overrides the other.

**Document-category classification.** When `question_gen.classify_document_categories` is on (default), a content-domain taxonomy (`research_paper`, `hr_policy`, `financial_filing`, …) is **discovered from the corpus** at generation time (unambiguous filenames matched for free, the rest classified in batches by the critic LLM and cached to `.doc_categories/<partition>.json`), and each item's `metadata.source.document_category` is baked in so every later benchmark slices on identical labels.

**CLI overrides** (all optional, defaults from `config.py`): `--profile`, `--per-category`, `--n-questions-per-cluster`, `--n-unanswerable-per-cluster`, `--output <path>`, `--no-filtration` (disable the critic gate), `--no-typing` (disable typed diversity).

**Outputs:** `dataset.json` (schema `2.0` — each entry carries an `id`, `question`, `llm_answer`, `answerable`, `chunks`, and a rich `metadata` block; see [Dataset format](#dataset-format)) plus a sidecar `dataset.manifest.json` recording the generation config snapshot (model, clustering, generation params, filtration, typing) and final `counts` (`total`, `answerable`, `unanswerable`, `by_question_type`, `failed_critic`, `generation_errors`, `chunks_total`, `chunks_kept`, `chunks_dropped_by_reason`). Each item's `metadata.generation.run_id` joins back to the manifest.

### 3. Benchmark

```bash
python benchmark.py \
    --partition *your_partition* \
    --base-url http://your-openrag-host:8095 \
    --dataset ./dataset.json \
    --output-dir ./reports \
    [--limit 50] \
    [--no-retrieval]
```

`--no-retrieval` skips the ID-based retrieval metrics even when the dataset has ground-truth chunks (generation + judges still run). Use it when the dataset's chunk ids come from a different index than the one under test (e.g. a fixed golden dataset scored against a freshly re-indexed instance), where ID matching would otherwise produce misleading zeros.

For each question, the benchmark:

1. Calls OpenRAG's `/v1/chat/completions` and captures the answer, the retrieved chunk ids, per-token logprobs, and end-to-end latency.
2. Computes **retrieval** metrics against the dataset's ground-truth chunk ids (when present, and unless `--no-retrieval`): hit@5, MRR, precision@k, recall@k, nDCG@k, MAP, R-precision.
   > These are computed off `chat/completions`' returned sources, which OpenRAG has already **filtered down to what the LLM cited** (see `filter_sources_by_citations` in the main project). That conflates two different failure modes: the retriever missing a chunk vs. the retriever finding it but the LLM not citing it. For a ranking signal that isolates the retriever, see the **raw retriever ranking trace** below.
3. Also queries OpenRAG's `GET /search/partition/{partition}` directly for every answerable row with ground-truth chunks (`benchmark.retrieval_trace_top_k` in `config.py`, default top 10) — this never touches the LLM, so it's the retriever's actual ranked output, unfiltered. Writes a per-question **retrieval trace** (`retrieval_trace_<ts>.jsonl` / `.csv`) recording the full ranked chunk list, which rank (if any) each gold chunk landed at, and which gold chunks never showed up at all — open it to eyeball rankings or spot documents the retriever silently ignored. Also reports aggregate any-hit-rate / full-recall-rate / avg-missed-gold-count. Skipped whenever `--no-retrieval` is passed.
4. Computes **generation** overlap metrics against the reference answer: ROUGE-1/2/L, BLEU-4, METEOR.
5. Runs the **LLM-as-judge** suite (concurrently, with shared semaphores):
   - **Completion** (1–10): how much of the reference answer is covered.
   - **Precision** (1–10): how factually aligned the response is.
   - **Answer relevancy** (reference-free): is the answer on-topic for the question.
   - **Context relevancy** (reference-free): are the retrieved chunks relevant to the question — scores the *retriever*, not the answer.
   - **Context recall** (reference-based): what fraction of the *reference answer's* claims are supported by the retrieved chunks — the recall counterpart to context relevancy (does the retriever surface everything the answer needs). Shares the `benchmark.faithfulness_fraction` sample.
   - **Refusal** verdict on every answerable row (false-refusal rate) and on every unanswerable row (abstention rate).
   - **Faithfulness** (claim-level support against the actually-retrieved chunks). Sampled — `benchmark.faithfulness_fraction` in `config.py` (default 50 %).
   - **Response label** (`Fully Correct` / `Incomplete` / `Contradictory`) per row — `benchmark.label_language` (`config.py`) controls FR vs EN judge prompt.
   - **CoT audit** — a chain-of-thought re-judging of a random sample (`benchmark.cot_audit_fraction`, default 10 %) to spot-check the cheap judges.
6. Cross-tabs the label judge vs. the score judges, and correlates per-row perplexity against both scores (Pearson + Spearman) — a sanity check that confident answers actually score higher.

The summary also **slices scores by facet** — question type, difficulty, file format/type, `document_category`, and per source file — to surface where the RAG is weakest. For value-answer question types (`benchmark.value_answer_types`, default `table_lookup` / `numerical_reasoning`), a strict **numeric-grounding** check complements the LLM judge: it verifies every number in the golden answer appears in the response within `numeric_rel_tol` / `numeric_abs_tol`, and reports the per-type match rate (inert for datasets without those types, e.g. any distribution-profile dataset).

Output (per run, timestamped in `reports/`): a JSON summary the dashboard reads, a TXT digest, and per-question CSVs (per-row scores, response labels with reasoning, CoT audit details).

### 4. Bring-your-own (golden) dataset

If you already have a curated dataset and the matching documents are already indexed in the partition, skip steps 1–2:

```bash
PARTITION=*your_partition* ./run_all.sh --golden /path/to/my_golden.json
# or equivalently
PARTITION=*your_partition* GOLDEN_DATASET=/path/to/my_golden.json ./run_all.sh
```

Override the partition with the `PARTITION` env var (it defaults to `common.partition` in `config.py`). Any unrecognised args are forwarded straight to `benchmark.py` — e.g. `./run_all.sh --golden g.json --limit 20 --no-retrieval`. Don't pass `--partition` this way: `run_all.sh` already injects its own, so use the `PARTITION` env var instead.

### 5. Context-contribution ablation

Quick eyeball test for whether retrieval is actually helping:

```bash
python context_ablation.py --partition *your_partition* --limit 10
```

For N random answerable questions, captures both the OpenRAG answer (with retrieved chunks) and the same generator model answering the bare question (no chunks). Writes `reports/context_ablation.csv` for side-by-side inspection in the dashboard.

You can also run this **inline after a benchmark** without a separate invocation — `benchmark.py --ablation` runs the ablation once the benchmark completes, sampling `--ablation-limit` questions (default `benchmark.ablation_limit` in `config.py`):

```bash
python benchmark.py --partition *your_partition* --ablation [--ablation-limit 10]
```

### 6. Orchestrated multi-version runs (deploy → eval → teardown)

To score the *same* dataset across OpenRAG versions/configs, `orchestrator.py` drives one fully self-contained run against a specific git build and cleans up after itself. You point it at a **separate** OpenRAG clone (the "versions repo") that it checks out and deploys — not this working tree.

```bash
python orchestrator.py \
    --version v1.2.0 \
    --versions-repo ../openrag-versions \
    --partition eval_v120 \
    --dataset ./dataset.json \
    --deploy-env /path/to/deploy.env \
    --env CHUNKER=semantic_splitter --env RETRIEVER=hybrid \
    [--no-retrieval] [--generate-questions] [--limit N] [--keep]
```

Per run (all teardown in a `finally`, so a crashed run still cleans up):

1. `git checkout <version>` in the versions repo, then auto-detect the compose file (repo-root `docker-compose.yaml` for older versions, `infra/compose/docker-compose.yaml` for the hexagonal refactor; override with `--compose-file`).
2. `docker compose up --build -d` into an isolated per-UUID compose project, with all bind mounts pointed at `runs/<uuid>/` and a free host port for the API (`--app-port` sets the default, auto-bumped if taken).
3. Poll `/health_check` until ready (`--health-timeout`, default 600 s).
4. Write `reports/<uuid>/run_config.json` (version + git commit + full config snapshot).
5. `upload_files.py` the corpus (`--corpus-dir` / `--max-files` to override the default `pdf_files/`), then `benchmark.py` → `reports/<uuid>/eval_*.json`.
6. `docker compose down`, `rm -rf runs/<uuid>`, and prune dangling images / build cache (skip with `--no-prune`; skip teardown entirely with `--keep` for debugging).

Key flags:

| Flag | Purpose |
|------|---------|
| `--version` (req) | Git tag / branch / commit to deploy |
| `--versions-repo` (req) | Path to a **separate** OpenRAG clone used for checkout + deploy |
| `--deploy-env` | `.env` for the deployed instance (sets `SHARED_ENV`; `AUTH_TOKEN` is recovered from it so the eval scripts authenticate against the instance) |
| `--config` | A Hydra `config.yaml` (or conf dir) copied into the build context before building |
| `--env KEY=VALUE` | Config override for the deployed instance (repeatable; captured in `run_config.json`) |
| `--generate-questions` | Self-contained mode: generate the dataset from *this* run's indexed chunks so chunk ids match (retrieval metrics non-zero); ignores `--dataset`. Volume via `--n-questions-per-cluster` / `--n-unanswerable-per-cluster` |
| `--no-retrieval` | Forwarded to `benchmark.py` — skip ID-based retrieval metrics |
| `--service` | Compose service(s) to build/start (default `openrag`; use `openrag-cpu` for a CPU deploy) |
| `--auth-token` | Explicit OpenRAG bearer token for the eval scripts (overrides the one recovered from `--deploy-env`) |

> Storage note: the stack persists via **bind mounts**, so `docker compose down -v` does *not* reclaim it — the orchestrator points those mounts at `runs/<uuid>/` and `rm -rf`s them itself. Shared HuggingFace / vLLM model-weight caches are left untouched.

The dashboard can launch this for you (see below); it also groups and compares runs by `version`.

### 7. Dashboard

```bash
streamlit run dashboard.py
```

- Browse every past run — both legacy flat `reports/eval_*.json` files (live-instance runs) and orchestrated `reports/<uuid>/` folders. Orchestrated runs are labelled by `version · partition`.
- Compare any two runs metric-by-metric (with deltas) — including the same dataset across different OpenRAG versions.
- Plot trends across all runs, grouped by metric family.
- Inspect per-token logprobs: the answer is rendered with each token shaded by confidence (from the run's `logprobs_*.jsonl`).
- **Deploy & evaluate a version** from the sidebar: it shells out to `orchestrator.py` (version, partition / URL / limit overrides, optional golden upload, optional `--generate-questions`, and a **Skip retrieval metrics** checkbox that maps to `--no-retrieval`) — runs as a background subprocess with a tail-log panel.
- View the latest SCORE corpus-quality result and the latest context-ablation CSV.

---

## Dataset format

`dataset.json` is a JSON list. **The benchmark only requires four fields** — `question` and `llm_answer` are mandatory; `answerable` and `chunks` are optional:

```jsonc
{
    "question": "...",           // required, non-empty string
    "llm_answer": "...",         // required, the reference / ground-truth answer
    "answerable": true,          // optional, default true; set false for adversarial rows
    "chunks": [                  // optional; required for retrieval metrics — each chunk needs an "id"
        { "id": 458974149490248568, "text": "...", "file_id": "note.pdf" }
    ]
}
```

- Rows without `chunks` still get scored on generation overlap + judges; retrieval metrics are simply skipped for them.
- Rows with `answerable: false` only get scored on the refusal judge (abstention rate). Their `chunks` field should be empty and `llm_answer` should be a refusal-style string.

`benchmark._load_and_validate_dataset` validates every entry at startup and refuses to run on a malformed file. A hand-curated golden only needs the four fields above.

**Generated datasets (schema `2.0`)** additionally carry a stable `id` and a rich `metadata` block that the benchmark ignores but that is invaluable for provenance and slicing:

```jsonc
{
    "id": "q-test3-000000",
    "question": "...",
    "llm_answer": "...",
    "answerable": true,
    "chunks": [ /* ... */ ],
    "metadata": {
        "schema_version": "2.0",
        "question_type": "multi_hop_specific",   // from the typing distribution (or capability)
        "difficulty": "hard", "n_hops": 2,
        "evolution": ["base", "multi_hop_specific"],
        "language": "en", "persona": null,
        "source": { "partition": "test3", "cluster_id": 47, "clustering_method": "hdbscan",
                    "source_file_ids": [...], "source_filenames": [...], "document_category": "research_paper",
                    "source_chunk_ids": [...], "n_chunks_sampled": 2 },
        "quality": { "answerability": 0.8, "self_containment": 1.0, "references_container": false,
                     "clarity": 1.0, "faithfulness_verified": true, "unanswerable_verified": null,
                     "passed": true, "regen_attempts": 0, "reject_reasons": [] },
        "generation": { "generator_model": "...", "prompt_version": "qgen-v3", "temperature": 0.2, "seed": 42,
                        "created_at": "2026-07-01T13:22:15Z", "run_id": "gen-20260701-132215",
                        "trace_id": "gen-20260701-132215:q-test3-000000", "token_usage": {...}, "latency_ms": 13462 }
    }
}
```

`metadata.generation.run_id` joins the entry back to `dataset.manifest.json`. Unanswerable rows carry the same block (with `question_type: "unanswerable"`, `quality.unanswerable_verified` set) plus a top-level `topic_chunks` list — the chunk ids the topic-adjacent question was seeded from, so the benchmark can check whether the retriever surfaced the seed topic even though the answer is absent.

---

## Metrics

### Retrieval

#### Hit Rate

The percentage of times **any** correct chunk appears among the chunks retrieved.

![Hit rate](./assets/Hit_rate.png)
![Hit rate illustrated](./assets/image.png)

> Hit Rate ignores the **position** of the relevant chunk. Two retrievers can have identical hit rates while one ranks the gold chunk at position 1 and the other at position 10 — clearly the first is preferable. MRR addresses this.

![Documents ranking](./assets/documents_ranking.png)

#### MRR — Mean Reciprocal Rank

The average reciprocal rank of the **first** correct result.

- MRR close to 1 → the right answer is usually at the top.
- MRR close to 0 → it's missing or buried deep.

![MRR](./assets/image-3.png)

#### Recall (and Recall@k)

Of all the chunks that *should* have been retrieved for the question, what fraction actually were.

![Recall](./assets/Recall.png)

#### Precision@k, MAP, R-precision

- **Precision@k** — of the top-k retrieved, how many are relevant.
- **MAP** (Mean Average Precision) — average precision across recall levels, then averaged across questions.
- **R-precision** — precision at rank R, where R = number of gold chunks.

#### nDCG — Normalized Discounted Cumulative Gain

The dominant ranking-quality metric in the literature: rewards both **retrieving** the right chunks **and** ranking them near the top.

![nDCG](./assets/nDCG.png)
![nDCG formula](./assets/nDCG_formula.webp)

The pipeline reports `nDCG@5`, `nDCG@10`, and a full-list `nDCG` broken down by ground-truth chunk count.

#### Raw retriever ranking trace

Hit Rate/MRR/Recall/Precision/MAP/nDCG above are all computed off the chat-completion response's cited sources — a subset of what the retriever actually returned, filtered down to whatever the LLM chose to cite in its `[Sources: ...]` tag. A gold chunk that the retriever found but the LLM didn't cite counts as a miss in every one of those metrics, indistinguishable from the retriever never finding it at all.

To isolate pure retriever behaviour, the benchmark separately calls `GET /search/partition/{partition}` for every answerable, ground-truth-bearing question — no LLM in the loop and nothing citation-filtered. This produces:

> **Caveat:** `/search` is *not* the same pipeline chat completions use. Per `RetrievalService.search()`'s own docstring it's "a single `searcher.search` (no query generation / reranking / RRF — those belong to `QueryService`)" — plain hybrid vector+BM25 top-k, no cross-encoder rerank step. If `config.reranker.enabled` is `true`, chat completions see a *reranked* order this trace doesn't reflect. Read it as "did the raw hybrid search even surface the right candidates," not "the exact final ranking the model saw." It also always pulls in surrounding/neighbor chunks after the real top-k hits (`with_surrounding_chunks=True`, hardcoded) — the benchmark slices back to `top_k` client-side so the count stays honest.

- **`retrieval_trace_<ts>.jsonl`** — one record per question: the full ranked chunk list (id, file_id, snippet, `is_gold`), `missed_gold_ids` (gold chunks absent from the entire fetched top-k), and `first_gold_rank`. Open one question at a time to see exactly how it was ranked.
- **`retrieval_trace_<ts>.csv`** — the same data flattened to one row per retrieved rank, for quick spreadsheet/pivot-table inspection.
- Aggregate stats in the JSON/TXT/console summaries: any-hit rate, full-recall rate, avg recall@top_k, avg missed-gold-chunk count, mean rank of the first gold hit.

Configured via `benchmark.retrieval_trace_top_k` (default 10) and `benchmark.retrieval_trace_snippet_chars` (default 240) in `config.py`. Skipped whenever `--no-retrieval` is passed (same flag that disables the citation-based metrics above).

### Generation (n-gram overlap vs reference)

ROUGE-1, ROUGE-2, ROUGE-L (F1), BLEU-4 (with smoothing), and METEOR. Useful as a cheap regression signal — not as an absolute quality score (a good answer that rewords the reference can score low).

### LLM-as-judge

| Judge | Output | Notes |
|-------|--------|-------|
| **Completion** | int 1–10 | Coverage of key points from `llm_answer`. |
| **Precision** | int 1–10 | Factual alignment with `llm_answer`. |
| **Answer relevancy** | fraction 0–1 | Reference-free: is the answer on-topic for the question (judged against the query, not `llm_answer`). |
| **Context relevancy** | fraction 0–1 | Reference-free: are the retrieved chunks relevant to the question — scores the **retriever**, not the answer (precision-flavoured). |
| **Context recall** | per-claim verdicts | Decomposes the **reference answer** into ≤12 atomic claims and marks each supported/unsupported by the retrieved chunks — the recall counterpart to context relevancy. Sampled at `benchmark.faithfulness_fraction`. |
| **CoT Completion / Precision** | reasoning + int 1–10 | Slower; sampled at `benchmark.cot_audit_fraction` to spot-check the cheap judges. |
| **Faithfulness** | per-claim verdicts | Decomposes the answer into ≤6 atomic claims and marks each `supported` / `unsupported` against the actually-retrieved chunks. Sampled at `benchmark.faithfulness_fraction`. |
| **Refusal** | `refusal` / `non_refusal` | Run on every row. Yields **false-refusal rate** on answerable rows and **abstention rate** on unanswerable rows. |
| **Label** | `Fully Correct` / `Incomplete` / `Contradictory` | A coarse, telegraphic classification per row, with one-line reasoning. |

All judges use structured output (Pydantic schemas in `judge_schemas.py`) and share an `asyncio.Semaphore` to bound concurrency. Judges occasionally emit malformed JSON; `_ainvoke_with_retry` retries up to `benchmark.judge.retry_attempts` times (default 3) before giving up on a row.

### Confidence / calibration

- **Mean per-token logprob** and **perplexity** (computed from the per-token logprobs OpenRAG forwards from the generator).
- A `Label × score-bucket` cross-tab — loud disagreements (e.g. label = `Contradictory` but completion ≥ 7) are flagged.
- Pearson and Spearman correlation of perplexity vs both completion and precision (expect negative — confident answers should score higher).

### Latency

Mean, p50, p95, p99 end-to-end seconds per OpenRAG request.

---

## Per-run artifacts

Each `python benchmark.py` writes to `--output-dir` (default `./reports/`):

| File | Contents |
|------|----------|
| `eval_<ts>.json` | Full structured summary — what the dashboard reads. |
| `eval_<ts>.txt` | Human-readable digest of every metric block. |
| `eval_<ts>.csv` | Per-question scores (completion, precision, nDCG, n_chunks, mean_logprob, perplexity). |
| `response_labels_<ts>.csv` | Per-question label + label reasoning + cheap judge scores. |
| `cot_audit_<ts>.csv` | The CoT sample — both cheap and CoT scores side by side, with reasoning. |
| `logprobs_<ts>.jsonl` | Per-question token strings + per-token logprobs — powers the dashboard's confidence-shaded token view. |
| `retrieval_trace_<ts>.jsonl` | Per-question raw retriever ranking (via `/search`, no LLM/citation filtering): full ranked chunk list, gold/miss flags, `missed_gold_ids`, `first_gold_rank`. |
| `retrieval_trace_<ts>.csv` | Same data flattened to one row per retrieved rank, for spreadsheet inspection. |

`upload_files.py` additionally writes `reports/score.csv` (latest SCORE corpus score + queued job ids). `context_ablation.py` writes `reports/context_ablation.csv`.

**Orchestrated runs** (`orchestrator.py`) write into a per-run `reports/<uuid>/` folder instead of flat into `reports/`, so nothing collides across versions. Alongside the benchmark artifacts above, that folder also contains:

| File | Contents |
|------|----------|
| `run_config.json` | Version, resolved git commit, partition, ports, compose project/files, env overrides, config snapshot. |
| `container_logs.txt` | `docker compose logs` captured before teardown. |
| `ports-override.yaml` | The generated compose override that pinned this run's host port(s). |
| `dataset.json` / `dataset.manifest.json` | Only in `--generate-questions` mode — the dataset generated from this run's own index, plus its manifest. |
| `error.txt` | Only on failure — the exception string from a crashed run, written before teardown so the dashboard/user can see why it failed. |
