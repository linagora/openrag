# PR #1161 review measurements

The review checks used the configured Mistral model and the two PDFs already indexed in `issue751-e2e-20261008`.

| Check | Result |
| --- | --- |
| Cases 64, 68, 73, 80: count match against `develop` labels | 0/4 |
| Same generated outputs: count match against the new labels | 4/4 |
| Same outputs: semantic coverage against either label set | 4/4 |
| One query per compared item, with each shared criterion preserved | 4/4 |

The four changed labels each ask for two item-focused queries. The 81-case run scored 75/81 on query count and 74/81 on semantic coverage. Three judge calls returned no structured result (cases 50, 41, and 45), so those coverage results remain inconclusive. The rewritten held-out cases 79 and 81 both passed count and coverage checks; the held-out test now checks both evaluation datasets.

For retrieval, two comparison questions each had the two PDFs as their manually judged relevant sources, one per compared method. With a two-hit search budget, `develop` retrieved 2/4 relevant sources; the candidate split retrieved 4/4. The rebuilt chat service also returned both source PDFs for both questions. Search results include adjacent context chunks, so this is source-file recall over this small set, not a general chunk-level recall claim.

Warm end-to-end chat TTFT was measured on three comparison questions, three requests each, using streamed responses capped at one answer token. Median TTFT was 4.45 s on head `16be4aa` and 4.18 s on the candidate build. The candidate's first request after container restart took 20.1 s; it was recorded separately from the warmed sample set.

The model now rejects an over-cap query list, retries with instructions to group periods without losing coverage, and falls back to the complete user query if retry fails. The previous bundled prompt hash is registered as superseded so existing default prompts refresh on upgrade.

Validation: 363 focused unit tests passed; Ruff check, Ruff format check, and `git diff --check` passed. Detailed outputs are in [the label evaluation](results/andy_review_labels_20261009.json), [the full-run summary](results/andy_review_full_summary_20261009.json), [the recall run](results/andy_review_recall_20261009.json), and [the TTFT samples](results/andy_review_latency_20261009.json).
