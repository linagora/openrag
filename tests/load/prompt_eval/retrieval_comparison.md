# Retrieval comparison measurements

The October 9 prompt evaluation and held-out checks are in the linked result files. These measurements record source coverage and latency; they do not establish improved chunk-level retrieval.

## October 12 live comparison

The bundled contextualizer now validates in the admin UI, using literal query limits compatible with older renderers. We tested the UI against the actual bundled template.

For latency, `develop` (`db7f6c66`) and candidate (`d9db441e`) each ran in a fresh Compose project with branch-specific prompt seeds and storage. Both indexed the same two research PDFs. We replayed the same three comparison questions three times per revision, using streamed one-token replies and excluding one warm-up request per revision. Median TTFT was **3.82 s on develop** and **4.17 s on the candidate** (+0.35 s / 9.2% in this run). This small sample does not support a latency-improvement claim.

For source recall on the same two papers, both revisions retrieved both relevant PDFs for both prior example questions (2/2 each). On three new comparisons over the five supplied `mini-data` PDFs, `develop` retrieved 6/6 relevant source files and the candidate retrieved 5/6 (0.83). The candidate missed the soybean document on one comparison. This is a small source-level sample, not a general recall claim.

The evaluator and production retry now share the same over-cap instruction. The stale query-cap comment is corrected.

Validation: 300 focused Python tests and 435 UI tests passed; Ruff check, Ruff format check, and `git diff --check` passed.

Detailed evidence: [label evaluation](results/retrieval_comparison_labels_20261009.json), [full prompt run](results/retrieval_comparison_full_summary_20261009.json), [October 9 recall](results/retrieval_comparison_recall_20261009.json), [October 9 latency](results/retrieval_comparison_latency_20261009.json), [current recall comparison](results/retrieval_comparison_recall_20261012.json), and [current TTFT comparison](results/retrieval_comparison_latency_20261012.json).

## Upgrade and rollback

The shared prompt-refresh implementation from #1097 is already in `develop`, and this comparison branch is based on it. The integration preserves the histories of shipped defaults and uses one refresh path. Customized prompts and custom prompt directories remain untouched. `PROMPTS_REFRESH_DEFAULTS=false` delays updates to existing defaults; new installations still receive the bundled prompt. The refreshed text uses placeholders understood by v2.2 and v2.3 callers. Rollback does not restore the previous saved prompt: back up the prompt library before upgrading if an exact behavioral rollback is required.

`rag.max_contextualized_query_len` is an output-token limit, despite its historical name. Explicit limits such as 512 remain respected. The contextualizer receives that budget and retries truncated or malformed responses with compact JSON and coarser complete coverage. Filters and all requested facets must survive grouping. A token budget cannot guarantee arbitrarily long requests will fit; two failures still fall back to the original question and may lose inferred date filters. Monitor these warnings and raise the configured limit when needed.

Web search allows up to three provider calls per request by default. `WEBSEARCH_MAX_QUERIES` (1–8) controls this independently of document retrieval, preserving separate calls for common two- or three-facet decompositions. Queries exceeding this budget are combined without dropping facets. This bounds per-request amplification; aggregate traffic across replicas still needs to fit the provider quota. No provider quota or load test was available to establish a safe aggregate rate.


## October 13 chunk comparison

A controlled run used the full local SciFact BEIR corpus (5,183 title-and-abstract chunks), the actual contextualizer and retrieval pipeline from each revision, and live Mistral Small 3.2, Qwen3 embedding and GTE reranking endpoints. Both runs used the same frozen embeddings, 50 dense candidates per query, reranking before RRF (k=60), and a final five-chunk budget. Twelve questions were checked against the actual abstracts; their biomedical topics do not occur in the prompt examples. Generated queries, filters, rankings, model usage and content hashes are recorded in the [develop](results/scifact_chunk_develop_20261013.json) and [candidate](results/scifact_chunk_candidate_20261013.json) results.

| Full chunk recall | Develop | Candidate |
| --- | --- | --- |
| Two required chunks | 7/7 | 7/7 |
| Three required chunks | 3/5 | 5/5 |
| All questions | 10/12 | 12/12 |

Both revisions found every required chunk before reranking. Develop lost a required chunk during reranking on the metabolism and tumor-and-neuron comparisons; decomposition recovered both in the final results. The final set contains all required chunks for these cases, rather than merely citing each source document.

Mean measured contextualizer input usage increased from 2,758 to 3,917 tokens (+42%). Median generation plus retrieval time increased from 1.40 s to 2.25 s; median retrieval alone was 0.35 s versus 0.59 s. Each question ran once per revision, so these are small-sample observations, not capacity estimates. The candidate artifact records source SHA `a2493ecf` with `source_dirty: true`, and the recorded query-service, query-model, and prompt hashes do not match that clean commit. This PR relies on these results as exploratory only and makes no retrieval-improvement claim from them. Re-run the comparison from a clean final PR revision before making a merge or rollout quality claim. The runtime date is pinned, and the artifacts retain the measured file hashes.

The first, broader question set produced 2/12 full-recall results on both revisions. Manual inspection then found that several original SciFact claims referred to full-paper details absent from the indexed abstracts. Those [questions](datasets/scifact_chunk_comparisons_exploratory.json) and [develop](results/scifact_chunk_develop_exploratory_20261013.json)/[candidate](results/scifact_chunk_candidate_exploratory_20261013.json) results are retained. All twelve questions were then grounded in the actual chunks and both revisions rerun. This refinement was exploratory, not preregistered; no retrieval prompt examples were tuned to the questions, and these results should not be generalized to the original issue-751 corpus.

This run uses exact dense cosine search, not Milvus hybrid search. It measures the final retrieval cutoff, not selection into the answer prompt. No new API TTFT measurement is claimed: the API stack was stopped, and the earlier October 12 TTFT measurements above describe the earlier candidate. A full API run with production indexing, hybrid search and context budgeting remains the rollout check.

The [bounded-output check](results/bounded_queries_20261013.json) exercises a 512-token limit with eight dated materials and a ten-material comparison. Both return complete grouped queries in one call, retain every item and criterion, and the dated query uses the full 2025 interval. Earlier attempts exposed truncation, omitted items, and an exclusive end-date error; the budget and date guidance was revised before recording this final check. These examples verify the measured cases, not a guarantee for every possible model response.

To reproduce, use `benchmark_chunk_recall.py --help`, a SciFact BEIR corpus directory, the checked-in question set, a credential dotenv file, and a detached develop checkout. Reuse the same embedding cache for both revisions; never compare reports whose corpus, question, embedding or settings hashes differ.
