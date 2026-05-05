# Refactoring Decision Log

Records **why** decisions were made that deviate from or extend the refactoring
docs. When a decision changes the plan, update the strategy/workflow docs to
reflect the new reality — then log the reasoning here so future readers know
why the docs changed.

Source abbreviations:
- STRATEGY = `docs/refactoring/REFACTORING_STRATEGY_v1.md`
- WORKFLOW = `docs/refactoring/REFACTORING_DEV_WORKFLOW.md`

---

## Phase 0 — Scaffold + import guard + CI wiring (2026-04-21)

**1. The guard ignores files outside the four new layer roots.**
Files under `openrag/components/`, `openrag/routers/`, `openrag/models/`,
`openrag/config/`, `openrag/utils/` are skipped.
- Why: Phase 0's verification requires existing tests to keep passing. If the
  guard ran against legacy code, every old import that doesn't fit the new
  rules would trip the check and block the phase. Legacy code gets migrated in
  Phases 5–12 and the guard picks those files up as they move into the new
  layer roots.
- Alternative considered: whitelist-only enforcement on new code (same idea,
  different framing). What we chose is "enforce wherever the file lives in one
  of the four roots", which is simpler.

**2. Split CI into `layer_guard.yml` + extending existing `lint.yml` and
`unit_tests.yml`, instead of one new `refactor-ci.yml`.**
WORKFLOW's CI example is a single file with three jobs (`unit-tests`,
`layer-guard`, `docker-build`). We took a different shape.
- Why: We already have a well-set-up `unit_tests.yml` and `lint.yml`. Creating
  a parallel `refactor-ci.yml` with its own unit-tests job would duplicate the
  uv setup and caching. Extending the existing files adds a few lines of
  config and reuses everything.
- Alternative considered: follow the WORKFLOW example literally. Rejected for
  the duplication reason above. Trade-off is that refactor-specific CI isn't
  all in one file.

**3. `docker-build` CI check NOT wired in Phase 0.**
WORKFLOW lists it as a required check.
- Why: Existing `build.yml` and `build_dev.yml` workflows push images to ghcr,
  which isn't what we want on every refactor push. A lightweight "docker build
  only, don't push" check needs a new job. Deferred to keep Phase 0 scope
  tight. Docker build was verified locally on the phase-0 tree.
- Alternative considered: add the job in this phase. Rejected for scope.
  Follow-up: add a `docker-build` job in a separate PR, modelled on the
  WORKFLOW CI example.

**4. Decision log policy: log reasoning, update docs.**
When a decision deviates from the strategy/workflow docs, update the docs to
match reality, then record the reasoning here.
- Why: The docs should always reflect the current plan. The log captures
  why the plan changed, not what the plan is.

---

## Phase 5 — Core domain logic for retrieval, chunking, prompts (2026-04-29)

**1. New `RetrievalSearcher` port in `core/retrieval/searcher.py`, separate from
the narrow `VectorStore` ABC.**
The retriever needs four operations (search by query string, multi-query
search, related-chunk lookup, ancestor lookup) that the Phase-4
`VectorStore` ABC does not cover — that ABC is intentionally narrow
(`search(embedding, top_k)`). We added a transitional ABC the retriever
depends on, implemented by `services/storage/milvus_ray_shim.py` over the
legacy Ray actor.
- Why: STRATEGY §5A says the retriever should "call `VectorStore.search()`
  (port method), not `vectordb.async_search.remote()`". But the legacy Ray
  actor's `async_search` takes a query *string* and embeds internally; the
  narrow `VectorStore.search(embedding, ...)` ABC doesn't fit. Pre-embedding
  in the shim before calling Ray is impractical because the actor also owns
  BM25 and surrounding-chunks semantics. A retrieval-facing port keeps the
  retriever clean of Ray today and survives Phase 7 — when the Vectordb
  god object is decomposed, these methods either move onto a richer
  `VectorStore` or split between `VectorStore` and `ChunkRepository`.
- Alternative considered: extend `VectorStore` with the four legacy methods.
  Rejected — bloats the ABC with operations that should not exist past
  Phase 7. Also considered: skip the new core port and have the retriever
  call the Ray actor through the shim with the legacy method names —
  rejected because that leaks legacy method names into core/ and makes the
  retriever harder to test.

**2. Skipped: bringing up integration tests for the new code.**
Phase 5 ships pure-domain unit tests only (50 new tests in `core/`, no Ray /
Milvus / real LLM). The new pipeline is dormant until Phase 8 wires it.
- Why: Mode 2 forbids touching the legacy wiring; the new pipeline has
  nowhere to be plugged in yet. Integration coverage will land with Phase 8
  orchestrators (or Phase 7 storage if it goes first).
- Alternative considered: stand up a fake searcher in an integration
  fixture and run a full retriever-pipeline-RRF round trip. Defers the
  same coverage to Phase 8 with less code; not worth the extra fixtures.

**3. `Query`, `SearchQueries`, `TemporalPredicate` lifted into
`core/models/query.py`.**
The legacy `components/pipeline.py` defined these inline. The new
`RetrieverPipeline` consumes them — they're domain types, not pipeline
internals.
- Why: STRATEGY §2 calls these out as `pipeline.py SearchQueries → core/models/query.py`.
- Alternative considered: keep them in `core/retrieval/`. Rejected — they
  describe a query in the abstract; the orchestrator (Phase 8) and the API
  layer will both use them, not just retrieval.

**4. Phase 5.15 (re-export shims) deferred to follow-up — then completed.**
The first Phase 5 commits (5A/5B/5C, 2026-04-29) created the new core/
modules but left the legacy `components/` files intact. STRATEGY §4.1
mandates a three-step move — create new file, update old file to
re-export from new, update consumers — and Phase 5 step 5.15 says
"Update old files to re-export from core/". We skipped that.
The follow-up sweep (2026-05-05) replaced six legacy files with shims:

| Legacy file | Shim strategy |
|---|---|
| `components/indexer/chunker/utils.py` | Plain re-export from `core.chunking.markdown_utils` |
| `components/prompts/prompts.py` | `load_prompt(key)` adapter calling `core.prompts.template_loader.load_template_by_key` |
| `components/utils.py:format_context` + `format_web_context` | Adapters into `core.prompts.chat_prompt_builder` (rest of utils stays — Phase 6+ scope) |
| `components/indexer/chunker/chunker.py` | `BaseChunker` / `RecursiveSplitter` delegate to `core.chunking.RecursiveSplitter` via Document↔ProcessedDocument↔Chunk conversion. `ChunkContextualizer` + `ChunkerFactory` retained (5D + Phase 8). |
| `components/retriever.py` | `Single`/`MultiQuery`/`HyDe` retrievers wrap `core.retrieval.retriever` strategies. Ray actor → `MilvusRayShim`; `ChatOpenAI` → `_LangChainLLMAdapter`. `RetrieverFactory` retained. |
| `components/pipeline.py` | `Query`/`SearchQueries`/`TemporalPredicate` re-exported from `core.models.query`. `RetrieverPipeline` delegates to `core.retrieval.pipeline.RetrieverPipeline` via a `_LegacyRerankerAdapter` bridging the legacy reranker (Document-in / Document-out) to the core ABC (str-in / `(idx, score)`-out). `RagPipeline` + `RAGMODE` retained (Phase 8). |

- Why: STRATEGY §4.1 is explicit ("Update old file to re-export from new
  location"); leaving the duplication in place would let the codepaths
  drift. Three CodeRabbit fixes from PR #352 (image_caption ChunkType,
  page-marker semantics, chunk_table header-only flush) had to be applied
  twice or only fixed in core — exactly the failure mode 5.15 prevents.
  The shim pattern matches the prior-art shims for config (1329cc18) and
  exceptions (a0f3d9f2).
- Alternative considered: leave the copies until Phase 8 cutover. Rejected
  — the doc explicitly puts 5.15 *inside* Phase 5, and one round of
  drift already happened.
- Side effect: a noqa side-effect import in `chunker.py` keeps the legacy
  `components.utils` ↔ `components.indexer.utils.files` circular-import
  resolving in the right order. Removed once `components.utils` is split
  in Phase 6+.
- Behavioral note: image elements in the legacy chunker now stamp
  `chunk_type=image_caption` (matching `core.models.chunk.ChunkType`)
  instead of the previous raw `image`. No legacy reader filters on this
  value, so the change is invisible to consumers.

---

## Phase 1 — Registry + Exceptions (2026-04-21)

**1. Exceptions keep HTTP status_code on the class (OpenRAG style), not in
a separate error handler mapping (mandragora style).**
- Why: Existing code reads `exc.status_code` in multiple places. Switching
  to a pure domain exception + API-layer mapping dict would require changing
  every consumer now, which is unnecessary churn in Phase 1.
- Alternative considered: mandragora's pattern (bare exceptions in core/,
  status code mapping in api/error_handlers.py). Cleaner for hexagonal
  purity but rejected for backward compatibility.
- Follow-up: strip status codes from core exceptions in Phase 10 when
  api/error_handlers.py is built. The error handler will own the mapping.

---

## Template for future entries

```
## Phase N — [short title] ([YYYY-MM-DD])

**K. [decision in one line].**
- Why: [what forced the call, what the docs didn't cover].
- Alternative considered: [what else was on the table, why it was rejected].
```
