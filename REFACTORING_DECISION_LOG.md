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

**5. `core/chunking/recursive.py` keeps `langchain.text_splitter.RecursiveCharacterTextSplitter` as a dependency.**
STRATEGY §3 lists core as "stdlib + pydantic + pure libs" and §4.7 limits
LangChain in core to boundary converters (`from_langchain` / `to_langchain`
on domain models). The chunker imports `RecursiveCharacterTextSplitter`
directly, which is neither stdlib nor a boundary converter.
- Why: `RecursiveCharacterTextSplitter` is a self-contained recursive
  separator-based string splitter — no IO, no LLM client, no Document
  semantics. Reimplementing it in core/ would be a meaningful chunk of
  pure code with no behavior change, and the legacy chunker has been
  using it for two years with stable output. Keeping it in for Phase 5
  preserves byte-for-byte chunk equivalence, which the strangler-fig
  shim relies on for behavior parity. The import is also deferred
  (inside the constructor / `split_text`) so importing the module
  without LangChain installed doesn't fail.
- Alternative considered: write a stdlib-only recursive splitter as part
  of Phase 5. Rejected as scope creep — would couple a behaviorally
  risky rewrite (chunk boundaries shift, downstream embedding output
  changes) to the additive Phase 5 cut, breaking the parity guarantee
  the legacy shims rely on. Tracked as a Phase 12 / post-cutover
  follow-up: replace the splitter with a stdlib implementation behind
  the same `Callable[[str], int]` length-function injection point.
- Scope: limited to `RecursiveCharacterTextSplitter`. No other LangChain
  symbol leaks into core; `langchain_core.documents.Document` only
  appears inside `Chunk.from_langchain` / `Chunk.to_langchain` /
  `Document.from_langchain` / `Document.to_langchain`, all with deferred
  imports, exactly as §4.7 prescribes.

**6. Two file-layout deviations from STRATEGY §3 / §5A / §5B.**
- `core/chunking/markdown_section.py` (§5B, line 1038; §3, line 387)
  → renamed to `core/chunking/markdown_utils.py`. The contents are pure
  parsing helpers — `MDElement`, `split_md_elements`, `chunk_table`,
  `parse_markdown_table`, `get_chunk_page_number` — not a
  section-aware chunker strategy. The name `markdown_utils.py` matches
  the module's role (utilities consumed by `RecursiveSplitter`); the
  separate `markdown_section.py` / `markdown_layout.py` *strategies*
  listed in §3's tree aren't built in Phase 5 and remain available
  filenames if/when those strategies land.
- `core/retrieval/hydration.py` (§5A, line 1027) → kept as private
  `_expand_with_related_chunks` in `retriever.py`. The function is
  ~60 LOC, only invoked by `BaseRetriever.expand_search_results`, and
  splitting it would add an import + test fixture surface without any
  reuse benefit. If a second consumer ever appears (Phase 8 likely),
  promoting it to a module-level public function in `hydration.py` is
  a one-commit move.
- Why: both deviations make the module names track the actual contents
  rather than the strategy doc's pre-write naming guess. Recording so
  future readers don't grep for files that aren't there.
- Alternative considered: rename to match the strategy doc verbatim.
  Rejected — the strategy filenames anticipated different content
  (a section/layout chunker, a standalone hydration entry point) than
  what Phase 5 actually produced.

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
  `api/error_handlers.py` is built. The error handler will own the mapping.

---

## Phase 5D — Indexing domain logic + parsers (2026-04-30)

**1. `core/indexing/validators.py` is fully framework-free.**
- All FastAPI types removed (`Form`, `UploadFile`, `HTTPException`,
  `status`, `Depends`); validators are pure functions on `str` / `dict`
  / `Iterable[str]`.
- `accepted_formats` / `accepted_mimetypes` are passed as args instead
  of read from Hydra `config` at module import (legacy module-level reads
  of `ACCEPTED_FILE_FORMATS` / `DICT_MIMETYPES` /
  `FORBIDDEN_CHARS_IN_FILE_ID` are gone).
- `ValidationError` accepts a `status_code` (and `code`) kwarg. Phase 1
  hardcoded 422; the original validators raised HTTP 400 (invalid
  `file_id` / metadata JSON) and HTTP 415 (unsupported format), so a
  status-code override is needed to preserve those codes from a
  pure-domain exception. Existing precedent in the same module
  (`LLMParsingError` overrides `status_code` after `super().__init__`)
  shows the pattern is already accepted.
- HTTP translation flows through the existing global
  `openrag_exception_handler` (`@app.exception_handler(OpenRAGError)`)
  wired in Phase 1, not local `HTTPException` raises in routers.
- Why: A core module that imports FastAPI or reaches into Hydra is not
  framework-free, blocks reuse from non-HTTP entry points, and
  re-introduces the boundary violation the refactor exists to fix.
  Stripping only `Depends()` — the literal task description — would
  leave the boundary half-broken.
- Trade-off: error body becomes `{"detail": "[CODE]: msg", "extra": {}}`
  instead of FastAPI's `{"detail": "..."}` — matches every other
  `OpenRAGError`.
- Alternatives considered: (a) consolidate everything on 422 — rejected,
  observable behaviour change; (b) introduce specific subclasses
  (`UnsupportedFileFormatError`, etc.) — rejected as premature, only two
  call sites need non-default codes today (Phase 10's API error-handler
  layer can re-evaluate); (c) keep module-level Hydra reads — rejected,
  embeds the infrastructure config object into core; (d) catch and
  re-raise as `HTTPException` in the router wrappers — rejected,
  duplicates the global handler.

**2. Exception shims under `utils/exceptions/` use `core.X`, not `openrag.core.X`.**
The legacy shims imported via `openrag.core.utils.exceptions`. With both
`/app` and `/app/openrag/` reachable, Python loads the same file as two
distinct modules, producing two distinct `OpenRAGError` classes —
`isinstance` failed and the global handler never fired.
- Why: Unifying on the bare `core.X` path matches `pythonpath = ./openrag`
  and the relative-imports-within-`core/` convention (commit 4528c71).
- Follow-up: ~20 other `from openrag.X` imports across `core/`, `config/`,
  and components are latent dual-import traps and should be migrated
  in a separate pass.

**3. Parser layering: native in core, services-backed in services/workers, type-marker bases without vendor names, DI for pools.**
- Native-bytes parsers (PyMuPDF, html_to_markdown, chardet, image) live
  in `core/indexing/parsers/`. Service-/Ray-backed parsers (Marker,
  LocalWhisper) live in `services/workers/parsers/`.
- Empty marker subclasses `BasePooledParser` / `BaseClientParser` in
  `core/indexing/parsers/document_parser.py` categorize parsers by *how*
  they get their work done (actor-pool vs HTTP-client) without naming
  the implementation. A core base class called `RayPoolParser` or
  `OpenaiClientParser` would leak vendor/infrastructure into the
  framework-free layer and foreclose swapping the backend.
- Core facades (`MarkerParser`, `LocalWhisperParser`, `ClientPdfParser`,
  `ClientAudioParser`) accept any pool/client of the appropriate marker
  type via `__init__`; services own the actor lifecycle.
- Why: `@ray.remote` decoration imports infrastructure at
  class-definition time and can't be hidden behind a port. DI keeps
  facades testable with in-memory fakes.
- Alternatives considered: (a) all parsers in core with Ray injected
  via DI — rejected, class-level decoration can't be deferred to
  composition; (b) have core facades resolve the actor by name
  themselves — rejected, couples core to Ray's named-actor registry.

**4. Image preprocessing helpers extracted to `core/indexing/image_preprocessor.py`.**
Pure helpers (`ensure_png_compatible_mode`, `pil_to_png_bytes`,
`pil_to_base64`, `is_http_url`, `is_data_uri`, `HTTP_IMAGE_PATTERN`,
`DATA_URI_IMAGE_PATTERN`, `MIN_IMAGE_PIXELS`). Used by the core image
parser and by Marker captioning in services.
- Why: Both layers need PNG normalization and markdown image-reference
  detection. Sharing via core (no VLM, no langchain imports) avoids
  services depending on `components/indexer/loaders/base.py`.
- Alternative considered: leave helpers in
  `components/indexer/loaders/base.py`. Rejected —
  services-importing-components is a layering violation, and `base.py`
  drags in langchain.

**5. `services/workers/ray_utils.py` keeps function and decorator forms together; `description=` is a format-string template.**
- `call_ray_actor_with_timeout` / `@with_timeout` and `retry_with_backoff`
  / `@with_retry` (with jitter) live in one module — STRATEGY's proposed
  `_retry.py` / `_timeout.py` split for `services/inference/` doesn't
  apply here because workers need both forms in practice (decorator at
  class-definition for static-param call sites, function form for
  callsite-resolved values). The decorators delegate to the function
  form internally; splitting across two files would duplicate that
  wiring.
- `description=` accepts a **format string** like
  `"PDF parse ({file_path})"`; `_resolve_description` binds it via
  `inspect.signature.bind` against the wrapped call's args at call
  time. **Callables (lambdas) are NOT supported** — they fall through
  to `if "{" not in template:` and raise `TypeError: argument of type
  'function' is not iterable`. (One outlier in `marker_workers.py` used
  a lambda and was fixed in Phase 5E.)
- Inline `call_ray_actor_with_timeout(worker.X.remote(...))` calls in
  workers are extracted into one-line `@with_timeout`-decorated helper
  methods (`_transcribe_chunk`, `_check_pool_broken`,
  `_reset_worker_pool`, `_run_chunk`, `_convert_pdf`) returning the
  `ObjectRef`; the decorator awaits it with timeout. Worker files use
  only decorator form — no mixed styles.
- Retry-around-timeout semantics preserved: `@with_retry` outer,
  `@with_timeout` inner — `TimeoutError` propagates from the inner
  helper and the outer decorator re-runs the whole method body (slot
  pick, fresh `.remote()`, fresh timeout).
- Alternatives considered: (a) mirror inference's `_retry.py` /
  `_timeout.py` split verbatim — rejected, adds files that just import
  from each other; (b) keep description static, drop to function form
  when dynamic — rejected, re-introduces the verbose
  `call_ray_actor_with_timeout(...)` call sites the decorator was meant
  to remove; (c) keep function form for the inline cases — rejected,
  leaves a mix of styles in the same file with no clear rule.

**6. `ray_utils` canonical home moved from `components/` to `services/workers/`.**
`components/ray_utils.py` is now a back-compat shim re-exporting from
`services.workers.ray_utils`.
- Why: Ray-actor concurrency primitives belong in the services layer,
  not in `components/` (which is on the deprecation path). Routers and
  pipeline still import via the components shim during the transition.
- Follow-up: migrate the remaining `components.ray_utils` imports
  (pipeline, search router, indexer router, workspaces router, indexer
  utils) and delete the shim in Phase 5E.

**7. Docling and DoclingV2 PDF backends deferred — not migrated in Phase 5D.**
No `core/indexing/parsers/pdf/docling*` modules will be created in this
pass. Legacy `DoclingLoader` and `DoclingLoader2` stay where they are
for now.
- Why: This is a PDF backend we haven't used or tested recently —
  porting it now would pin a stale integration into the new layer. We'll
  revisit and re-port it (or drop it) in a later pass once the refactor
  has shaken out and we know whether Docling is still wanted.
- Alternative considered: port now alongside Marker / OpenAI / DotsOCR
  for completeness. Rejected — moves dead-feeling code into the new
  layer without verifying it still works.
- Follow-up: revisit during a later parser-coverage sweep. If the
  decision is to drop, the legacy modules get deleted in Phase 5E rather
  than shimmed.

**8. `ImageBlock` is the parser↔caption contract — captioning is a downstream stage's job.**
- Every parser (Image, Markdown, Docx, Pptx, Eml, Marker,
  `DotsOCRPdfClient`) emits `ImageBlock` with `caption=None`. The
  caption stage fills it in. For VLM-PDF specifically, the picture-bbox
  crop becomes an `ImageBlock(image_bytes=…, page_number=N)` — the
  parser never issues the second VLM call. One uniform contract beats
  per-parser carve-outs; the chunker sees the same `ImageBlock` shape
  from every parser, including `DotsOCRPdfClient`.
- `ImageBlock.metadata['markdown_ref']` holds the in-text placeholder
  (data-URI, `![](pptx-image-N)`, `![](docx-image-N)`,
  `![](marker-key)`); the caption stage `str.replace`s it. No
  placeholder ⇒ no `markdown_ref` ⇒ caption stage emits a
  free-standing `TextBlock`. Contract is documented on `ImageBlock`
  itself.
- `ImageBlock` carries `image_bytes` (default `b""`) AND `source_url`.
  Locally-extracted images set bytes; HTTP refs (`![alt](https://…)`)
  leave bytes empty and set `source_url`. The `image_url` property
  returns `data:{mime};base64,…` when bytes are present, else
  `source_url` — consumers read `image_url` regardless of shape.
- Why: Refs are per-image-unique and chunk-stable. Legacy
  `MarkdownLoader` captioned HTTP images via langchain `ChatOpenAI`
  (which accepts URLs natively). The new VLM ABC takes bytes only, so a
  fetch stage has to populate them — but the parser still emits one
  `ImageBlock` per in-text image, keeping the contract uniform.
- Alternatives considered: (a) positional matching of refs to images —
  rejected as fragile; (b) embedding image bytes inside `TextBlock` —
  rejected as a heavier model change.

**9. Paginated parsers emit `list[TextBlock]` with `page_number`; in-band `[PAGE_N]` markers are gone.**
Marker and PPTX previously concatenated all page content into one
`TextBlock` with `[PAGE_N]` markers between pages. They now emit one
`TextBlock` per page with `page_number` set, matching what PyMuPDF
already does. Parsers without natural pagination
(text/html/md/docx/doc/eml/whisper/image) still emit a single
`page_number=1` block.
- Why: Pagination is metadata, not content. Leaking `[PAGE_N]` markers
  into chunk text forced every consumer to know the marker syntax;
  `TextBlock.page_number` is the canonical channel and was already
  half-used.
- Implication for chunking: the chunker must NOT scan for `[PAGE_N]`
  markers. Iterate `ProcessedDocument.text_blocks` and carry
  `block.page_number` onto every emitted chunk. Page boundaries are
  block boundaries.

**10. Client-backed parsers: generic `Client*Parser` facades; `BaseOpenAIPdfClient` is scaffolding only.**
- Renamed `OpenAIPdfParser` → `ClientPdfParser`
  (`core/indexing/parsers/pdf/openai.py` → `pdf/client_based.py`); added
  `ClientAudioParser` at `core/indexing/parsers/audio/client_based.py`.
  Both accept any `BaseClientParser` and delegate `parse()`. "OpenAI"
  was a leaky model-specific label on a class that takes any
  HTTP-client-backed parser; whatever DotsOCR / Whisper-vLLM /
  Scaleway-Speech is called next quarter, the facade stays the same —
  what varies is the injected `BaseClientParser`.
- `BaseOpenAIPdfClient` provides reusable helpers (PDF page rendering,
  semaphore-protected `_ocr_one(page_img, prompt) → str | None`,
  JSON-fence stripping, JSON loading, picture-bbox cropping). It does
  **NOT** define `parse()`, a `PROMPT` class attribute, or abstract
  `_caption_images` / `_result_to_md` / `_parse_ocr_response` hooks.
  The file was renamed `_openai.py` → `_base_openai_parser.py` to
  match the new role.
- Why: The previous abstract pipeline imposed assumptions ("there's one
  OCR response per page", "captioning is a parser concern") that didn't
  generalise. Treat the base as a toolbox; let each concrete client
  (DotsOCR, future variants) drive its own `parse()` and block-emission
  strategy.
- Trade-off: more code per concrete subclass. Accepted —
  model-specific variation (response schema, block layout, bbox
  handling) lives in the subclass anyway.
- Alternative considered: keep one model-specific facade per backend.
  Rejected — duplicates the same isinstance + delegate boilerplate.

**11. DotsOCR response is validated through Pydantic.**
`DotsOCRElement` / `DotsOCRPage(RootModel[list[DotsOCRElement]])` /
`DotsOCRCategory` (Enum) capture the layout-element shape;
`DotsOCRPdfClient._parse_page` runs `model_validate` and returns `None`
on bad payloads. The `{"items": [...]}` envelope is tolerated alongside
a bare list.
- Why: Replaces dict shuffling (`page_res.get("category") == "Picture"`,
  `item.get("bbox")`) with typed access (`element.category is
  DotsOCRCategory.PICTURE`, `element.bbox`). Bad payloads fail loudly
  via `ValidationError` instead of silently returning empty markdown.

**12. `OpenAIAudioClient` keeps language detection as an injected callable, not a Ray ref-getter.**
Legacy `AudioTranscriber` looked up a `WhisperActor` Ray actor by name.
The new `OpenAIAudioClient` takes `language_detector: Callable[[Path],
Awaitable[str | None]] | None` in its constructor and skips detection
when `None` (vLLM auto-detects).
- Why: Keep the client free of Ray coupling so it can be instantiated
  and tested without a Ray cluster. The wiring layer passes a closure
  that calls the Whisper actor when `USE_WHISPER_LANG_DETECTOR=true`.
- Alternative considered: keep the Ray actor lookup inside the client
  guarded by a config flag. Rejected — pulls Ray into the
  `services/inference` layer where the rest of the file is plain HTTP.

---

## Phase 5E — Loader → Parser shims (2026-05-06)

**1. Legacy loaders are *adapter* shims, not re-export shims.**
The earlier compat-shim pass (commit `93476a6`) used pure `from X
import Y` re-exports because the symbols moved unchanged
(`ray_utils`, `text_sanitizer`, exceptions). The loader→parser move
can't do that: `BaseLoader.aload_document(file_path) → langchain
Document` and `DocumentParser.parse(document) → ProcessedDocument`
have different names *and* different contracts. Each legacy loader
becomes a `BaseLoader` adapter that reads the file into bytes, builds
a `CoreDocument`, calls the new parser, and maps `ProcessedDocument`
back to a langchain `Document`.
- Why: Preserves dynamic loader-discovery
  (`BaseLoader.__subclasses__()` in `loaders/__init__.py`) and the
  config-string lookup (`file_loaders.pdf: "MarkerLoader"`) without
  forcing every consumer to migrate at once.
- Alternative considered: pure re-exports aliasing `*Parser` as
  `*Loader`. Rejected — the discovery walk only finds `BaseLoader`
  subclasses, so an aliased `DocumentParser` would silently disappear
  from the loader registry.

**2. Shimmed in this pass: text/markdown, image, docx, doc, pptx, pymupdf, marker, local-whisper, openai-audio.**
Each adapter delegates to its core parser and, when the parser emits
`ImageBlock`s with `markdown_ref` set, layers VLM captioning on top
via the existing `BaseLoader` mixin (`self.image_captioning`,
`self.caption_images`, `self.replace_markdown_images_with_captions`).
- Why: Keeps the legacy contract intact (captioned markdown in
  `page_content`) while the canonical home is the parser. The
  `markdown_ref` substitution path is the same one the future
  caption-stage will use.

**3. `base.py` Stage 1: re-export the four image_preprocessor symbols already in core, leave the captioning mixin in place.**
`ensure_png_compatible_mode`, `HTTP_IMAGE_PATTERN`,
`DATA_URI_IMAGE_PATTERN`, `MIN_IMAGE_PIXELS` now point at the
canonical `core.indexing.image_preprocessor` symbols (class attrs
hold module-level references for `self.X` access).
`_pil_image_to_base64` rewritten on top of `pil_to_png_bytes`. The
VLM endpoint setup, `get_image_description`, `caption_images`,
`replace_markdown_images_with_captions` stay in `base.py` for now.
- Why: Mechanical, behavior-identical change. Stage 2 (move VLM
  captioning to `services/inference/captioning`) needs a design call
  (where it lives, how the shim acquires it) and is deferred.

**4. `PyMuPDFParser`: single dedicated thread + retain empty pages for 1-to-1 pagination.**
- PyMuPDF/pymupdf4llm are not thread-safe; concurrent calls raise
  `ValueError: not a textpage of this page`. Upstream maintainer
  (`pymupdf/PyMuPDF#3771`, closed wontfix) confirms this is documented
  behaviour, not a bug. The parser now uses a module-level
  `ThreadPoolExecutor(max_workers=1)` instead of `asyncio.to_thread`;
  concurrent `parse()` calls queue on the executor, eliminating the
  race against the default thread pool. The rest of the indexing
  pipeline still parallelizes — only the pymupdf step is serialized.
- Empty pages now produce a `TextBlock` with empty `text` (was
  previously dropped while keeping `page_count` accurate). Reverted so
  every page produces a `TextBlock`, keeping a 1-to-1 mapping with the
  source PDF's pagination — the legacy `\n[PAGE_N]\n` anchor format
  the loader-shim emits aligns exactly with the source.

**5. `TranscriberConfig.direct_upload_suffixes` got lost in the core/config migration; ported to `core/config/indexation.py`.**
The legacy `config/models.py:TranscriberConfig` had the field +
`|`-separated string validator + a default frozenset of audio
extensions. The active `core/config/indexation.py:TranscriberConfig`
(loaded via `openrag.core.config.loader.load_config`) was missing it,
producing `AttributeError: 'TranscriberConfig' object has no attribute
'direct_upload_suffixes'` when the audio shim accessed it.
- Why: `config/models.py` is now vestigial — kept for legacy imports
  but no longer drives `load_config()`. Fields added there but not
  mirrored to `core/config` are silently inactive at runtime.

**6. Skipped: eml, `pdf_loaders/openai.py`, `pdf_loaders/dotsocr.py`.**
- `eml_loader.py`: the new `EmlParser` takes `attachment_parsers:
  Mapping[str, DocumentParser]`, but the old loader dispatches
  attachments through `BaseLoader`-keyed `get_loader_classes` with a
  multi-tier PDF fallback chain (`MarkerLoader` → `PyMuPDFLoader` →
  `PyMuPDF4LLMLoader` → `DoclingLoader`). The contract bridge isn't
  trivial; deferred until services-side attachment-parser composition
  lands.
- `pdf_loaders/openai.py` + `pdf_loaders/dotsocr.py`: services-side
  `BaseOpenAIPdfClient` / `DotsOCRPdfClient` exist but require a
  concrete `core.vlm.VLM` to instantiate, and `vlm_registry` is empty
  (no concrete VLM impl exists yet). Both legacy classes are also dead
  code on this branch — not in any Hydra config, no external imports.
- Why: Both gaps need new services-side work (attachment-parser DI,
  `LangchainOpenAIVLM`-style concrete) before a meaningful shim is
  possible. Re-export-only "shims" would relocate the file without
  going through the new architecture, defeating the purpose.

**7. Stale files flagged for deletion (Phase 12 cleanup).**
- `components/indexer/loaders/CustomHTMLLoader.py` and
  `components/indexer/loaders/CustomDocLoader.py` — legacy
  `BaseLoader` subclasses, not referenced by any Hydra config or
  external import. Discoverable via `BaseLoader.__subclasses__()` but
  never instantiated. `CustomDocLoader` uses
  `UnstructuredWordDocumentLoader` / `UnstructuredODTLoader` — no
  clean parser equivalent in core (`DocxParser` uses MarkItDown).
- `config/models.py` (the whole file, incl. its `TranscriberConfig`)
  — superseded by `core/config/*`; kept only so legacy imports don't
  break. Drift between the two has already caused one runtime bug
  (entry 5).
- Why: Out of scope for the loader-shim pass; flagged here so they
  don't get re-shimmed by future passes. Removal coordinates with
  Phase 12 ("delete old re-export shims").

---

## Phase 6B — vLLM inference clients + legacy shims (2026-05-07)

**1. `VLLMVision(VLLMClient, VLM)` — multiple inheritance kept for nominal typing.**
`VLLMClient` provides the full implementation (httpx pool, retry,
circuit breaker, `aclose()`). `VLM` is a pure abstract mixin with no
conflicting methods, so the MRO is linear and clean. Adds only
`_max_tokens`, `caption_image()`, and `caption_images_batch()`.
- Why: VLM and LLM talk to the same vLLM OpenAI-compatible
  chat/completions endpoint, so `VLLMClient` is the right concrete
  base. Keeping `VLM` in the bases preserves nominal typing —
  `isinstance(vision, VLM)` works, and any future code that type-checks
  against the VLM ABC will accept `VLLMVision` without a cast.
- Alternative considered: single inheritance `VLLMVision(VLLMClient)`
  only, relying on structural/duck typing for registry lookup. Rejected
  — the registry is currently structurally typed, but explicit ABC
  conformance is cheap here (no diamond, no conflicting methods) and
  makes the intent clear to readers.

**2. `LLM.generate()` and `LLM.chat()` return `dict` (full OpenAI-compatible response body), not `str`.**
The original ABC typed both methods as `→ str`, which forced callers to
re-construct the surrounding OpenAI envelope when building RAG answers
(losing `model`, `usage`, `finish_reason`, etc.). The concrete vLLM
implementation already returned the full `httpx` JSON body; the `str`
annotation was aspirational, not real.
- Why: RAG answers are ultimately forwarded to the client in OpenAI format.
  Stripping to plain text at the LLM boundary means the pipeline has to
  re-wrap the content into `{"choices": [{"message": {"content": …}}]}`
  further up — metadata (token counts, model id, stop reason) is lost in
  the process. Returning `dict` preserves the full payload and keeps
  back-ends interchangeable without wrapping shims. Using bare `dict` (not
  a `TypedDict`) is a deliberate first step: it is backward-compatible with
  all current callers and eases compat-shim re-exports while the
  refactoring is still ongoing.
- Alternative considered: introduce typed response models (`ChatCompletion`,
  `CompletionResponse`, `ChatCompletionChunk`) immediately. Rejected as
  premature — Phase 6 adds the concrete client; Phase 10 (API layer
  clean-up) is the right time to freeze the contract with typed models.
  The `dict` annotation signals intent without coupling every caller to a
  model definition that will evolve.
- `stream_chat` stays `AsyncIterator[str]` yielding raw SSE lines
  (`data: {…}` strings). Parsing SSE chunks into typed dicts is Phase 10+
  work; the current shape keeps the streaming path consistent with
  OpenAI's SDK behaviour.
- Future normalisation — when the typed models land, callers will migrate
  to this pattern (TypedDict shown; Pydantic models are equally valid and
  would expose `chat_content` / `completion_text` as properties instead):

```python
from typing import TypedDict

class _Message(TypedDict):
    role: str
    content: str

class _Choice(TypedDict):
    index: int
    message: _Message        # chat completions
    finish_reason: str | None

class _CompletionChoice(TypedDict):
    index: int
    text: str                # text completions
    finish_reason: str | None

class _Usage(TypedDict):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

class ChatCompletion(TypedDict):
    id: str
    object: str              # "chat.completion"
    model: str
    choices: list[_Choice]
    usage: _Usage

class Completion(TypedDict):
    id: str
    object: str              # "text_completion"
    model: str
    choices: list[_CompletionChoice]
    usage: _Usage

# Convenience extractors at the pipeline boundary:
def chat_content(resp: ChatCompletion) -> str:
    return resp["choices"][0]["message"]["content"]

def completion_text(resp: Completion) -> str:
    return resp["choices"][0]["text"]
```

  Until then, callers that need the text can use
  `resp["choices"][0]["message"]["content"]` directly.

---

## Phase 7A.1 — Connection manager + schema (2026-05-12)

**1. Pulled the 7A.4 migration directory move forward into the 7A.1 commit set.**
The spec files the directory move (`scripts/migrations/alembic/` →
`services/persistence/migrations/`) and the env.py rewire under a separate
subsection (**7A.4 — Migrations**), distinct from 7A.1 (`connection.py` +
`schema.py`). The move was done in this commit set anyway.
- Why: The 7A.1 work creates `schema.py` whose entire purpose is to be
  Alembic's metadata target. Leaving env.py pointing at the legacy
  `components.indexer.vectordb.models.Base.metadata` would have created a
  short-lived intermediate state where the two metadata definitions had to
  stay byte-for-byte identical (or Alembic autogenerate would flag the
  schema as drifted). Moving env.py at the same time avoids that risk
  window — once schema.py exists, env.py points at it directly.
- Alternative considered: strict 7A.1-only — create `schema.py` but leave
  the old `scripts/migrations/alembic/env.py` importing `Base.metadata`
  until 7A.4. Rejected for the dual-source-of-truth risk above, and
  because the migration move is a pure `git mv` with no code changes
  beyond two import lines.

**2. Extended `RDBConfig` with `database`, `pool_min_size`, `pool_max_size`, `command_timeout`.**
The spec's `ConnectionManager.__init__` pseudocode reads `config.database`,
`config.pool_min_size`, `config.pool_max_size` directly. None of those
fields existed on OpenRAG's `RDBConfig`. Added them (with defaults
`pool_min_size=5`, `pool_max_size=20`, `command_timeout=30`, `database=None`)
plus matching `POSTGRES_DATABASE` / `POSTGRES_POOL_{MIN,MAX}_SIZE` /
`POSTGRES_COMMAND_TIMEOUT` env-var mappings in `core/config/loader.py`.
- Why: 7A.1 doesn't compile otherwise. The spec's "files to create" table
  lists only `connection.py` + `schema.py`, but the implementation it shows
  has a hard config-shape dependency. Treated as required scaffolding for
  7A.1 rather than as a 7E (DI) concern, since the new fields belong on
  the same config object that already carries `host`/`port`/`user`/`password`.
- Alternative considered: pass DSN + pool sizes as bare positional args
  to `ConnectionManager.__init__`, leaving `RDBConfig` untouched. Rejected
  — the spec's reference implementation accepts a `PostgresConfig` object
  and the 7E DI wiring (`create_catalog_store(config)`) hands the whole
  config in. Splitting fields across the call site and the config would
  diverge from that contract.

**3. `RDBConfig.database` stays optional; `ConnectionManager.__init__` raises if it's still `None`.**
The legacy code derives the Postgres database name from the Milvus
collection name (`f"partitions_for_collection_{collection_name}"`) at
`MilvusDB` actor startup. The new `RDBConfig` could either (a) require the
caller to set `database` explicitly, or (b) compute the name itself from
`VectorDBConfig.collection_name`. Chose (a) with a None default and a
constructor-time guard.
- Why: Crossing config sections (RDB reading VectorDB) would entangle two
  otherwise independent config blocks and make `RDBConfig` non-portable.
  The collection→database mapping is an integration concern that belongs
  in the 7E DI wiring (`create_catalog_store` will build the database
  name from `config.vectordb.collection_name` and inject it). The guard
  in `ConnectionManager` makes the missing-database case fail loudly at
  construction instead of silently producing a malformed DSN.
- Alternative considered: derive the database name inside
  `RDBConfig.model_post_init` from a separately-injected collection name.
  Rejected — adds two-way coupling between config sections for no gain;
  7E handles the wiring cleanly in one place.

**4. Programmatic schema-vs-ORM diff used as the acceptance check.**
After rewriting all 7 tables as `sa.Table(...)` on a shared `MetaData`,
ran a column-by-column / index-by-index / constraint-by-constraint diff
against `components.indexer.vectordb.models.Base.metadata`. Empty diff =
passes. No assertion is shipped — this was a one-time verification, not
runtime behaviour.
- Why: Alembic autogenerate will treat any divergence between the new
  metadata target and the live database (which was built from the legacy
  ORM) as a pending schema change. The diff confirms that won't happen
  on first run, and documents the methodology for the next migration:
  any future schema change must update both `schema.py` and the legacy
  `models.py` until Phase 9 deletes the latter.
- Alternative considered: ship the diff as a runtime test in 7F.
  Deferred — 7F's repo tests already need a live Postgres; a metadata
  diff doesn't need one and can live as a one-off check until the
  legacy `models.py` goes away in Phase 9.

---

## Phase 7A.2 — Repository implementations (2026-05-12)

**1. Added a `WorkspaceRepository` port + `Workspace` domain model.**
The Phase-4 ports list had no workspace abstraction even though the
Phase-7 spec explicitly enumerates `workspace_repo.py` as one of the six
"real" repos with ten methods extracted from `PartitionFileManager`.
Added `core/ports/workspace_repo.py`, `core/models/workspace.py`, and
exposed `CatalogStore.workspace_repo`.
- Why: skipping it would leave the `workspaces` + `workspace_files`
  tables strictly addressable only through the shim, defeating Phase
  8's point (orchestrators talk to ports, not the legacy actor). The
  legacy code's workspace surface is non-trivial (orphan-file
  detection, partition-scoped FK resolution) — it needs a first-class
  port.
- Alternative considered: fold workspace methods into `DocumentRepository`
  or `PartitionRepository`. Rejected — workspaces are a distinct
  aggregate (their own row + join table) and conflating them blurs the
  responsibility split that the rest of the ports layer enforces.

**2. Extended `OIDCSession` domain model with three optional `bytes` fields
for the encrypted IdP tokens.**
The Phase-4 model had `id`, `session_token_hash`, `user_id`, `sid`,
`sub`, two timestamps, `last_refresh_at`, `revoked_at` — no token
fields. The port `create_session(session: OIDCSession) -> OIDCSession`
must convey what to store, so omitting them was a port-shape bug.
Added `id_token_encrypted`, `access_token_encrypted`,
`refresh_token_encrypted` as `bytes | None`.
- Why: the auth layer (Phase 6F) encrypts tokens before storage and
  decrypts after read; the repo is intentionally byte-blind. Domain
  models that hide load-bearing storage fields force callers to
  bypass the port (e.g. with separate `set_tokens()` calls), defeating
  the point of the typed contract. The "encrypted blobs flow through
  the model verbatim" pattern is the same one the legacy
  `_oidc_session_to_dict()` already uses.
- Alternative considered: an internal `OIDCSessionWithTokens` model
  used only at the repo boundary. Rejected — duplicating models for
  the sake of pretending the encrypted bytes aren't part of the
  session is structural noise; Phase 8 callers don't read those
  fields anyway.

**3. Concrete repos expose two parallel surfaces: ABC methods + legacy
method names — both writing to the same rows.**
Each `Pg<Entity>Repository` implements the Phase-4 port ABC verbatim
(typed domain models in, typed domain models out) AND carries every
legacy `PartitionFileManager` method name with its original signature
and return shape (positional args, dict returns) as separate methods
marked `# TODO(phase-9): remove`. The legacy methods are NOT on the ABC.
- Why: Phase 8 orchestrators consume the port; Phase 7C shim must
  delegate to the existing 76 call sites without rewriting them. A
  single typed surface would force the shim to translate at every
  call boundary, multiplying the change set and the regression
  surface. Two surfaces on the same underlying SQL keeps both clients
  happy with zero behavioural drift. Phase 9 deletes the legacy
  surface after the shim goes away.
- Alternative considered: legacy method names only, postpone the
  typed port to Phase 8. Rejected — the typed port is what makes the
  ports/adapters split testable from `core/` unit tests; doing it now
  is the cheap moment.

**4. `users.password_hash`, `users.is_active`, `users.updated_at` exist on
the `User` domain model but not on the schema; mapped to defaults at the
repo boundary.**
The Phase-4 `User` model documents three auth modes (OIDC, token,
password+JWT) so it carries `password_hash`. The current schema only
supports OIDC + token. The user_repo silently drops `password_hash` on
write and synthesises `is_active=True` / `updated_at=created_at` on
read.
- Why: the alternative is dropping fields from the domain model, but
  password auth is a planned post-refactoring feature on the roadmap.
  Keeping the model field-complete means the orchestrator code can
  be written once and only the repo updates when the column lands.
- Alternative considered: add the missing columns now via a new
  Alembic migration. Rejected — Phase 7 is a structural refactor, not
  a feature expansion. Adding columns expands scope past the spec.

**5. Api-key repository methods raise `NotImplementedError`; stub repos
raise a `StubRepositoryError(NotImplementedError)` subclass.**
The `UserRepository.create_api_key` / `get_api_keys_by_prefix` /
`list_api_keys_for_user` / `delete_api_key` methods have no backing
table (`users.token` is a single-token field). Six entire ports
(`ChunkRepository`, `JobRepository`, `PromptRepository`,
`ConversationRepository`, `AuditLogRepository`,
`IdempotencyRepository`) plus four extras (`EntityRepository`,
`TopicTagRepository`, `ModelEndpointRepository`, `PresetRepository`)
are full-class stubs.
- Why: a silent fallback (empty list / `None`) is worse than an
  exception — an orchestrator that retrieves zero rows from a "doesn't
  exist yet" repo behaves indistinguishably from a real empty repo,
  hiding bugs. The dedicated `StubRepositoryError` subclass is loudly
  grep-findable (`grep -rn stub_not_implemented`) when the
  post-refactoring features come online.
- Alternative considered: leave the stub ports unimplemented (no Pg
  classes at all). Rejected — `CatalogStore` requires every port via
  abstract properties; a partial implementation can't satisfy the
  ABC, so Phase 7A.3 (composite store) wouldn't even instantiate.

**6. Registered `json` / `jsonb` codecs on every connection via the
asyncpg `init` callback so reads return Python dicts.**
The legacy schema stores `files.file_metadata` as `JSON`; asyncpg
returns JSON columns as strings by default. Without the codec every
repo would have to `json.loads()` every row read and `json.dumps()`
every parameter write.
- Why: one place to register, repos stay focused on SQL. The codec
  also covers `jsonb` so future migrations from `JSON` to `JSONB`
  don't ripple into repo code.
- Alternative considered: per-call `json.loads()` / `json.dumps()` in
  each repo. Rejected — duplicate boilerplate per repo, easy to forget
  in one spot. The connection-level codec is the asyncpg-recommended
  pattern.

---

## Phase 7A.3 — PostgresStore composite (2026-05-13)

**1. Placed `PostgresStore` in `services/storage/`, not alongside the
repositories in `services/persistence/`.**
The "storage" tier owns the high-level adapters that the rest of the
system depends on (`MilvusStore`, `PostgresStore`); "persistence" owns
the row-level repository implementations. Phase 8 orchestrators only
ever import from `storage` — they never reach into individual repo
modules. Keeping the composite outside `persistence/` makes that
boundary visible in the import graph.
- Why: the phase 7 plan explicitly names this split and it matches the
  already-existing `services/storage/milvus_store.py` placeholder. A
  future reader can tell the layers apart by directory.
- Alternative considered: `services/persistence/postgres_store.py`.
  Rejected — would conflate the composite with its parts and force the
  shim/orchestrators to import from `persistence/`, defeating the
  point of the directory split.

**2. Eagerly construct all fifteen repos in `__init__`; share one
`pool_getter` callable across them.**
Repos do not touch the pool until a query runs, so building them at
construction time is free and saves every caller from a lazy-init
dance. Passing a `_pool_getter` bound method (instead of the raw pool
reference) lets the store survive a `shutdown()`/`initialize()` cycle
in tests — repos always see the live pool.
- Why: matches the pool-getter pattern established in 7A.1 and keeps
  test fixtures simple (rebuild the store, not the repos).
- Alternative considered: build repos lazily on first property access.
  Rejected — extra branching with no measurable win, and the property
  getter would lose its read-only character.

**3. `initialize()` opens the pool *then* runs migrations; both behind
a single entry point.**
The legacy ORM bootstrapped tables synchronously via
`Base.metadata.create_all` before Alembic ever ran, which is why every
Phase 7 migration is idempotent (CLAUDE.md "Alembic Migration
Idempotency"). The composite keeps that ordering so the DI container
calls `await store.initialize()` once and gets a ready-to-query store.
A `run_migrations=False` flag is provided for fast unit tests against
an already-migrated database.
- Why: hiding the two-step lifecycle behind one method keeps the
  Phase 7E container wiring identical to the inference adapters and
  matches the `CatalogStore` ABC contract (one `initialize`, one
  `shutdown`).
- Alternative considered: expose `run_migrations()` separately and
  require the container to call both. Rejected — leaks an
  implementation detail across the layer boundary and makes every
  composition-root harder to write.

**4. Expose the raw asyncpg pool as a `pool` property on the concrete
class, not on the `CatalogStore` ABC.**
Phase 8 orchestrators need cross-repo transactions
(`async with store.pool.acquire() as conn: async with conn.transaction(): ...`).
That capability is not on the ABC because most consumers do single-repo
calls; adding it to the port would invite leaks of asyncpg specifics
into orchestrator code that doesn't need them. Concrete clients that
truly need transactional escape hatches can depend on `PostgresStore`
directly.
- Why: keeps the ABC minimal while still unlocking the transaction
  pattern. Phase 8 will decide whether to formalise a
  `UnitOfWork`-style port; until then the escape hatch is explicit and
  grep-findable (`grep -rn "store.pool"`).
- Alternative considered: add a `pool` property to `CatalogStore`.
  Rejected — turns the ABC into an asyncpg-shaped interface and makes
  it harder to swap in a non-Postgres backend (e.g. SQLite for tests).

---

## Phase 7B — Milvus vector store (2026-05-12)

**1. `hybrid_search(embedding, query_text, …)` is on the `VectorStore` ABC alongside `search`. Initial draft kept it Milvus-specific; reversed before the contract surface settled.**
The Phase 7 plan's `_hybrid_search` example (STRATEGY-adjacent doc `phase 7.md` lines 275–298) references an undeclared `query_text` argument — the doc tacitly admits the embedding-only `search` cannot drive Milvus 2.6's native BM25. Milvus's `Function(FunctionType.BM25)` computes the sparse vector server-side from the `text` field at both insert and query time, so the hybrid path *requires* the raw query text — not a pre-computed sparse vector and not an embedding.
- Why on the ABC: every realistic backend in the SaaS shortlist has a hybrid lexical-plus-dense path (Qdrant via sparse-vector points + fusion, Weaviate hybrid BM25+vector, Pinecone sparse-dense vectors, OpenSearch knn+match). Treating hybrid as a Milvus-only quirk would push an `isinstance(store, MilvusVectorStore)` branch straight to the contract boundary inside the Phase-8 retrieval orchestrator — exactly the leak the narrow store was supposed to prevent. `query_text` is wider than strictly needed for backends that accept a pre-computed sparse vector, but it is the only shape that fits Milvus's server-side BM25 without client-side tokenization, which would force a reindex and contradicts the spec's "Critical: preserve OpenRAG's current Milvus schema".
- Initial draft and reversal: the first pass put `hybrid_search` on `MilvusVectorStore` only and kept the ABC embedding-only — *"BM25 is a Milvus-specific implementation detail; bleeding it into the cross-store contract for one backend's quirk is the wrong direction."* Reversed once the SaaS-end-state backend list made hybrid look like a contract feature rather than a Milvus quirk. Backends without a hybrid path can raise `VDBSearchError` at call time — same shape `MilvusVectorStore` already uses when its backing collection was constructed with `hybrid_search=False`.
- Alternative considered: (a) pre-compute sparse vectors client-side via a tokenizer/IDF table — rejected, abandons Milvus's native BM25 (server-side analyzer + stop words) and would force a reindex; (b) `query_text: str | None = None` added to `search` itself (one method, optional arg) — rejected, conflates dense-only and hybrid call sites in the same method and forces every implementation to inspect both args.

**2. Embedding dimension comes in via a lazy `await store.initialize(dim)`, not a constructor arg or a config field.**
The Phase 7 plan's example constructor is `MilvusVectorStore(config: MilvusConfig)` and silently elides where `dim` comes from — the schema needs the dim, but the dim lives on the embedder.
- Why: Mirrors `PostgresStore.initialize()` shape so DI wiring (Phase 7E) has one consistent "construct cheap, materialise async" pattern across both stores. Construction stays I/O-free and embedder-free; the DI container resolves the embedder, reads `embedding_dimension`, and passes it to `initialize()`. Idempotent + double-checked-locked so concurrent first-callers don't race.
- Alternative considered: (a) explicit constructor arg `MilvusVectorStore(config, embedding_dimension=…)` — cleaner dependency but forces every test/composition root to resolve the embedder first; (b) `VectorDBConfig.embedding_dimension` — duplicates the embedder's `EmbedderConfig.embedding_dimension` value across two configs, drift risk.

**3. ABC `collection` arg = Milvus collection name (not partition row-value). Partition lives only in `filters["partition"]`. Added a Milvus-specific `delete_by_filter(filters)`.**
The Phase 7 plan (`phase 7.md` line 300) explicitly maps the ABC's `collection` argument to "the partition row-value", and proposes `drop_collection(name)` deleting rows where `partition == name`. The same word would then mean two different things across the codebase — Milvus's own vocabulary keeps *collection* (top-level container) and *partition* (row tag via `partition_key`) strictly distinct.
- Why: The end-state of this refactor is a multi-tenant SaaS product where each client gets its own Milvus collection (see [[project-saas-collection-per-tenant]] memory). In that world the ABC's `collection` arg is a real per-tenant Milvus collection name; conflating it with partition values would paint the future store-factory/pool into a corner. Strict separation makes the SaaS path a Phase-8+ wrapping layer ("`client_id → MilvusVectorStore`") on top of an unchanged narrow store.
- Concrete shape: `MilvusVectorStore._resolve_collection(name)` accepts only `self._collection_name` or the ABC sentinel `"default"`; anything else raises `ValueError`. `drop_collection(name)` drops the whole Milvus collection (admin/test). Partition-level row deletion (used by the 7C shim's `delete_partition`) goes through a new Milvus-specific public method `delete_by_filter(filters)`, with an explicit guard that refuses empty/wildcard expressions so a typo cannot nuke the entire collection.
- Alternative considered: (a) spec-faithful overload — rejected, conflates two distinct Milvus concepts in code that has to survive the SaaS pivot; (b) ignore the `collection` arg entirely instead of validating — rejected, silently accepting wrong names is the same forward-compat hazard.

**4. No manual reconnect / retry logic against Milvus — trust pymilvus + gRPC internals.**
The Phase 7 plan's design note recommends `MilvusVectorStore` carry its own retry/reconnect logic, citing `ConnectionNotExistException` and double-checked locking in `_ensure_loaded()`.
- Why: That guidance comes from the pre-2.4 ORM-style `connections.connect(alias=…)` API where named aliases needed explicit re-establishment. Pymilvus 2.6's `MilvusClient(uri=…)` / `AsyncMilvusClient(uri=…)` — per [v2.6.x API reference](https://milvus.io/api-reference/pymilvus/v2.6.x/MilvusClient/Client/MilvusClient.md) and [AsyncMilvusClient v2.6.x](https://milvus.io/api-reference/pymilvus/v2.6.x/MilvusClient/Client/AsyncMilvusClient.md) — expose **no** public retry / reconnect / keepalive knobs and own their gRPC channel internally. The legacy `MilvusDB` does no manual reconnect either. Reintroducing client-side teardown-and-recreate logic risks racing pymilvus's internal channel state for no documented benefit. Documented inline in `MilvusVectorStore.__init__` so the plan's note doesn't get reintroduced later without evidence.
- Alternative considered: (a) lightweight retry without client recreation (sleep + retry-once on connection-error message match) — rejected, no documented gRPC-level guarantee that a fresh call sees a healed channel any sooner than gRPC's own backoff; (b) full client teardown + recreate with double-checked locking — drafted, then dropped after reading the pymilvus 2.6 reference; pure complexity for an unproven failure mode.

**5. Store surface kept narrow: `VectorStore` ABC + Milvus-only `delete_by_filter`. File/chunk conveniences are 7C shim's job.**
The legacy `MilvusDB` exposes `get_file_chunks`, `get_chunk_by_id`, `get_file_chunk_ids`, `list_all_chunks`, `get_related_chunks`, `get_ancestor_chunks`, `get_surrounding_chunks` — all file-scoped or relationship-scoped reads.
- Why: Each of those is either (a) a thin wrapper over `query_chunks_by_filter` (file-scoped reads) or (b) domain logic that belongs in `core/retrieval/hydration.py` per the spec (surrounding/related/ancestor chunks). Putting them on the store widens the surface only to delete them again in Phase 8. The 7C shim builds the file-scoped variants from `query_chunks_by_filter` (two RPCs vs one — accepted cost for a narrow ABC-aligned store).
- Alternative considered: add the convenience methods directly on the store. Easier 7C shim (one-line delegate per method) but a wider surface to maintain and to migrate again in Phase 8. Rejected.

---

## Phase 7A.4 — Unified migrations namespace (2026-05-12)

**1. Nest Postgres alembic and Milvus migrations as siblings under `services/persistence/migrations/{alembic,milvus}/`, not as two parallel roots.**
Person A's pulled-forward 7A.4 (commit `91f7078`, logged in [Phase 7A.1 §1](#phase-7a1--connection-manager--schema-2026-05-12)) placed alembic directly at `services/persistence/migrations/`. The phase 7 plan is silent on Milvus migrations — they are an OpenRAG-specific addition (Milvus schema-version property + generic runner under `openrag/scripts/migrations/milvus/`) not contemplated by the spec. The first take on the Milvus side put them at `services/storage/migrations/` next to `milvus_store.py` (commits `b40de00` + `26311f0`, since reset out of history); reshuffled to a unified namespace before push.
- Why: One root with backend subdirs ("where do I run migrations?" → `services/persistence/migrations/`, "for which backend?" → `alembic/` or `milvus/`) is easier to reason about than two roots that split "schema evolution" across two services-layer namespaces purely because their adapters happen to live in different sub-folders. The unified shape also leaves room for additional backends (S3 lifecycle, future tenancy stores) without re-litigating where migrations live each time. Aligns with the SaaS end-state ([[project-saas-collection-per-tenant]] memory) where per-tenant collection lifecycle and per-tenant schema versioning will both grow inside this same namespace.
- Alternative considered: (a) spec-plus-by-symmetry layout — alembic under `services/persistence/migrations/`, Milvus under `services/storage/migrations/`. Rejected: makes the storage layer carry a `migrations/` peer to `milvus_store.py`, conflating "the adapter" with "the adapter's schema-evolution scripts", and forces every reader to know which backend hides where. (b) flatten everything under one dir without backend subdirs. Rejected: alembic's filename conventions (`<hash>_<slug>.py`) and the Milvus runner's `N.description.py` discovery rules would step on each other.

**2. Milvus migration runner imports `SCHEMA_VERSION_PROPERTY_KEY` from `services.storage.milvus_store`, not from `components.indexer.vectordb.vectordb`.**
The constant exists identically in both modules right now (the new store copied it byte-for-byte from the legacy MilvusDB during 7B). Either import resolves; we picked the new home.
- Why: The migration set, the constant, and the new adapter all live under `services/` after this move. Importing from the legacy module would create a backwards dependency from the new namespace into the deprecation-path module, pinning `components.indexer.vectordb.vectordb` alive past Phase 9's planned deletion. Updating the import now is a zero-risk follow-up to the move (legacy still defines the constant with the same value, so behaviour is identical) and makes the legacy deletion a single grep-and-delete in Phase 9 rather than "deletion + N migrate.py import updates".
- Alternative considered: leave the import on the legacy module until Phase 9 forces the issue. Rejected — no benefit, and `9` is the wrong phase to be hunting incidental imports.

---

## Phase 7E — DI wiring (2026-05-13)

**1. Made `ServiceContainer(settings=None)` optional so the pre-Phase-7E
test paths keep working.**
The container's pre-existing job was to populate inference registries
(`ServiceContainer()` with no arguments). Phase 7E adds storage adapter
wiring that needs a `Settings` instance, but rewriting every legacy
test to pass settings is busy-work and risks scope creep. The settings
argument is therefore optional; the storage accessors raise a clear
`RuntimeError` ("ServiceContainer was constructed without a Settings
instance — pass Settings to wire storage adapters") when reached
without one.
- Why: keeps the Phase 7E commit a strict superset of the previous
  container behaviour and surfaces the misuse with a message that
  points at the fix.
- Alternative considered: split into `ServiceContainer` (registries
  only) and `AppContainer(settings)` (registries + storage). Rejected
  — Phase 8 orchestrators will pull both layers from one container,
  and a hard split now would invite a refactor at the very next phase.

**2. Centralised the "database name from collection name" idiom in
`create_catalog_store`.**
The legacy `MilvusDB.__init__` derives the Postgres database name from
the Milvus collection name (`partitions_for_collection_<name>`,
vectordb.py:238). That contract is duplicated in `scripts/backup.py`,
`scripts/restore.py`, `scripts/check_file_counts.py`, and the new
Alembic `env.py`. The Phase 7E factory keeps the fallback in one place
so DI wiring code never mentions the prefix; an explicit
`rdb.database` still wins.
- Why: the database name resolution is policy, not orchestrator code
  — putting it in the factory keeps `di/container.py` mechanical and
  prevents the prefix from drifting into half the call sites.
- Alternative considered: resolve in `PostgresStore.__init__`.
  Rejected — pushes a Settings dependency down into the store, which
  is happy taking just `RDBConfig` today and shouldn't have to grow a
  `vectordb` parameter to learn the collection name.

**3. `create_vector_store` wires to the real `MilvusVectorStore` (Phase 7B)
in this same branch.**
The factory was first drafted as a fail-loud stub (`raise
NotImplementedError("Phase 7B deliverable")`) so 7E could land
independently of 7B. With 7B already on the branch when 7E was
rebased, the factory's body swapped to its final shape: `return
MilvusVectorStore(settings.vectordb)`. Construction stays I/O-free
and embedder-free; the composition root resolves the embedder and
calls `await store.initialize(embedding_dimension)` later, matching
the lifecycle pattern :class:`PostgresStore` already follows.
- Why: keeps the DI factory shape stable across the 7B handoff —
  no caller has to know whether 7B was merged first. The lazy
  `initialize(dim)` keeps the construction step out of any async
  context and avoids a circular dependency on the embedder factory.
- Alternative considered: pass the embedder dimension into the
  factory itself. Rejected — forces every test/composition root to
  resolve the embedder before the store, defeating the construct-
  cheap / materialise-async split that the Phase 7B store relies on.

**4. Aligned `services/persistence/` and `services/storage/` imports
to the project's short-form convention (`from core.X`, not
`from openrag.core.X`).**
Pytest sets `pythonpath = ./openrag`, so `openrag/core/foo.py` is
importable as both `core.foo` and `openrag.core.foo` (the editable
install also exposes the `openrag` package). Python treats those as
two distinct modules, so a class defined once but imported via both
paths fails `isinstance` checks — which is exactly how the Phase 7E
container test first surfaced the dual-import bug
(`PgDocumentRepository` not isinstance `DocumentRepository`). Picking
one convention everywhere fixes the bug, and `CLAUDE.md` already
mandates the short form (`from components.ray_utils import ...`).
- Why: matches the rest of the codebase and removes a class of
  isinstance bugs that would otherwise dog every Phase 8 orchestrator
  test.
- Alternative considered: leave the `openrag.X` prefix in place and
  ban the short form in tests. Rejected — the short form is already
  load-bearing in `di/`, `core/llm/`, `core/embeddings/`, and the
  Phase 6 inference adapters; changing all of those to the long form
  would be a much larger and riskier sed pass.

---

## Phase 7F — Integration tests (2026-05-13)

**1. Integration tests land at `tests/integration/`, not colocated with the SUT and not under `tests/api_tests/`.**
The project today has tests in two places: colocated `openrag/**/test_*.py` (the long-standing convention) and `tests/api_tests/` (HTTP-style black-box tests against a running OpenRAG server). STRATEGY §13C describes a target end-state of `tests/{unit,integration,load}/` with everything under one root. 7F lands integration tests for both the asyncpg repos and `MilvusVectorStore`; the unified layout doesn't arrive until Phase 13C.
- Why: Integration tests for both stores need to escape `pytest.ini`'s `testpaths = openrag` so a bare `uv run pytest` (the default unit run) does not drive real infrastructure. The cleanest interim home is exactly where Phase 13C will park them anyway — `tests/integration/` — so the file lands once and does not need to move during the sweep. Colocating per-repo tests under `openrag/services/persistence/test_*_repo.py` would also force every unit-test invocation to either skip (silently masking failures) or fail (in CI without infra) depending on how the skip is wired, neither of which is a good default.
- Alternative considered: (a) park integration tests under `tests/api_tests/` to match the existing infra-test sibling layout. Rejected — `api_tests/` is HTTP-API-style by convention (httpx against a running server); adapter-level integration tests don't fit that mould, and the strategy doc has already settled the end-state location. (b) keep them colocated and rely on the `integration` pytest marker for deselection. Rejected — relies on every CI invocation remembering `-m "not integration"` and still pulls pymilvus/Postgres imports into the default unit run.

**2. One ephemeral test database per session, truncate between tests.**
The fixture creates `openrag_phase7_test` on session start with a clean
schema, runs migrations once via `PostgresStore.initialize()`, and lets
every test share the same store. An autouse fixture truncates the seven
user-modifiable tables with `RESTART IDENTITY CASCADE` so each test
starts with primary keys at 1.
- Why: dropping/recreating per test would multiply the session cost by
  the test count; a per-test truncate is microseconds. The fresh-DB-
  per-session contract gives us migration coverage for free (any
  Alembic regression surfaces at fixture setup, not in production).
- Alternative considered: rollback-per-test via savepoints. Rejected
  — asyncpg pools cycle connections, so a top-level rollback only
  cleans the one connection used; the next test could see partial
  state on a different connection. Truncate is straightforward and
  matches what Phase 8 orchestrator tests will want.

**3. Session-scoped loop and fixtures (`loop_scope="session"`).**
`pytest-asyncio`'s default `function`-scope event loop conflicts with a
session-scoped async fixture: the asyncpg pool binds to the loop it was
created on, and each test then gets a different loop. Marking both the
fixtures and the tests with `loop_scope="session"` keeps everything on
one loop, which is also what asyncpg expects.
- Why: a session-scoped pool is the whole reason to run an integration
  suite — function-scoped pools would defeat the purpose. The
  `loop_scope` knob is the official pytest-asyncio 1.x API for this.
- Alternative considered: function-scoped pools (one per test).
  Rejected — every test would pay the connection-establishment cost,
  inflating the suite from ~5s to easily 30s+.

**4. `tests/integration/test_stores.py` is `xfail(strict=True)` until the
cross-store fixture lands in Phase 7C.**
The Phase 7F plan calls for a cross-store full-cycle test
(`create partition → upsert pre-embedded chunks → search → delete`).
:class:`MilvusVectorStore` is in place (Phase 7B) and reachable through
DI, but the existing `postgres_store` fixture does not yet hand out a
companion Milvus store on the same collection — the combined fixture
lands as part of Phase 7C (shim) so the assertion matches the legacy
flow byte-for-byte. The strict marker means the day the fixture is
wired and the body filled in, the unintended pass trips a clear
failure rather than silently turning green.
- Why: the file exists so the eventual diff is just a fixture + body
  replacement. Strict xfail prevents "forgot to remove xfail" rot.
- Alternative considered: skip with `pytest.skip(...)` or leave the
  file out entirely. Rejected — skips are too easy to forget, and an
  absent file means the cross-store test has to remember to be
  created.

**5. Fixed two bugs surfaced by writing the tests, in the same diff.**
The integration suite caught two tz-naive/tz-aware mismatches in code
written during 7A.2:

* `PgOIDCSessionRepository.delete_expired` constructed
  `datetime.now(UTC) - _EXPIRED_RETENTION` (tz-aware) and bound it
  against the tz-naive `session_expires_at` column. asyncpg refuses
  the cast at bind time. Fix: strip `tzinfo` from the cutoff before
  the query.
* `services/persistence/migrations/alembic/env.py` unconditionally clobbered
  `sqlalchemy.url` with one derived from `load_config()`, ignoring
  the DSN that `ConnectionManager.run_migrations()` had just set —
  which is why the first run of the suite tried to resolve the docker
  hostname `rdb` from a host process. Fix: defer to the preset URL
  unless it's the alembic.ini placeholder.

Both fixes are tiny but load-bearing for any non-default deployment
(test DBs, named environments, CI overrides). I kept them in this
commit rather than splitting them out — they only show up against a
real database, and splitting would mean landing the new tests broken.
- Why: the alternative (separate fix commits) would have the
  integration tests fail on first introduction, which is a bad signal
  in `git log`.
- Alternative considered: hard-code a workaround in the test fixture.
  Rejected — the underlying bugs would still be there waiting for
  Phase 8 orchestrator tests to hit them.

---

## Phase 7C — God-object shim (2026-05-13)

**1. Kept the "create PG database if missing" bootstrap in the shim, not
in `PostgresStore`.**
The legacy `PartitionFileManager.__init__` auto-created the per-collection
database via `sqlalchemy_utils.create_database` (`utils.py:39-40`) — a
side-effect callers depended on without ever opting in. The Phase 7C
replay needed equivalent behaviour, and the natural architectural home
is `PostgresStore.initialize()` (symmetric with the Alembic-migration
step it already runs, and with `MilvusVectorStore.initialize()` which
materialises its collection). It was instead placed on the shim as
`MilvusDB._ensure_pg_database`, called from `MilvusDB.initialize()`
between the vector-store and catalog-store bootstrap steps.
- Why: Phase 7C's scope contract is "only `components/indexer/vectordb/vectordb.py`
  is modified". Pushing the helper down into `PostgresStore` would have
  expanded the blast radius into 7A.3 territory while the shim was still
  the only caller. Punting the design to Phase 7E (DI wiring, where the
  `create_catalog_store` factory already centralises the database-name
  derivation) means one place owns both naming and provisioning once
  the shim is gone.
- Alternative considered: move it to `PostgresStore.initialize()` now
  (cleanest layering). Rejected for the scope reason above — but **flag
  for 7E/8 follow-up**: when DI wiring lands the store-creation path
  for orchestrators, the database-existence check should move into
  `PostgresStore.initialize()` (or `ConnectionManager.initialize()`)
  so the shim's `_ensure_pg_database` helper can be deleted in Phase 9
  without leaving a regression behind. Track this together with the
  `scripts/*.py` callers that already duplicate the
  `partitions_for_collection_<name>` derivation (see Phase 7E entry 2).

---
## Phase 8 — Orchestrators (2026-05-19)

**1. Provider sourcing: routers read `request.app.state.container`.**
- Why: the 8 Phase-8 doc shows providers as one-line accessors; the
  ServiceContainer is not attached to the live app until Phase 11. We
  attach it best-effort in `main.py` now (not `initialize()`d), so the
  thinned routers resolve services but the DB-backed flows stay dormant
  until Phase 11. Token-mode auth routes already 400 before any service.
- Alternative considered: a lazy module-level container singleton in
  `di/providers.py` (mirrors `components/auth/deps.py`). Rejected — the
  one-liner/app.state shape matches the doc and Phase-11 cutover.

**2. OIDCConfig built from env in the container, not wired into Settings.**
- Why: keeps 8A.1 scoped; `OIDCConfig` exists but is not in root
  `Settings`. Added a missing `auto_provision_login` field.
- Alternative: wire it into `Settings` now — deferred (config refactor).

**3. Thin routers: typed core exceptions propagate to the global
`OpenRAGError` handler; `HTTPException` kept only where the legacy body
must stay byte-identical** (id==1 / 409-exists / file|workspace 404s).
- Why: the legacy `_check_*` guards already surfaced 404s via the global
  handler, so propagating `UserNotFoundError`/`PartitionNotFoundError`/
  `ValidationError` reproduces them; only the non-bracketed
  `{"detail": ...}` `HTTPException` bodies need to stay verbatim.

**4. Constructor extensions over the plan's prescribed signatures.**
- Why: to preserve legacy behaviour without reaching into Ray/config,
  orchestrators take extra container-supplied args: `collection`
  (`settings.vectordb.collection_name` — Partition/Workspace/Retrieval),
  `user_repo` (PartitionService, to reproduce `VDBUserNotFound`),
  `default_file_quota` (UserService), and orchestrator→orchestrator
  injection (UserService←PartitionService for the delete-user cascade).
- Alternative: hold the plan signatures exactly — rejected, would drop
  behaviour (cascade) or smuggle config/Ray into the service.

**5. 8A.2 delete-user owner-partition cascade deferred, then restored in
8B.1.** UserService.delete_user was a plain repo delete in 8A.2 (gap
logged); 8B.1 composes PartitionService so it deletes owned partitions
(vectors + rows) first, matching the legacy Ray `delete_user`.

**6. 8C searcher backing: inject the Ray-backed `MilvusRayShim` behind
the `RetrievalSearcher` port during the Phase-8 shim.**
- Why: the only `RetrievalSearcher` impl is `MilvusRayShim` (Ray
  `Vectordb` actor — embeds + hybrid-searches internally). The 8C plan
  text says "retriever calls `vector_store.search()` directly (no Ray)",
  but the dev-workflow doc puts Ray cleanup in Phase 9 and allows
  orchestrators to call Ray *behind a port* during the shim. Injecting
  the shim keeps the orchestrator file Ray-free (8H satisfied: no
  Ray remote-call, no Ray import — Ray is behind the port) and avoids a
  risky reimplementation of hybrid-search / surrounding-chunks /
  similarity behaviour in the hot search path.
- Alternative considered: write a new `VectorStoreSearcher` on the clean
  `VectorStore` port now (embed via Embedder factory + `search`).
  Rejected for this phase — real regression surface in core search; the
  clean adapter lands in Phase 9 when the shim is deleted. **Flag for
  Phase 9:** add `VectorStoreSearcher`, swap the container wiring, delete
  `MilvusRayShim`.
- Consequence: `RetrievalService.__init__` deviates from the plan's
  `(vector_store, embedder_factory, reranker_factory, llm_factory,
  document_repo, config)` — with the shim searcher those are unused;
  it takes the built `searcher` / `reranker` / `llm` + `config`.

**7. 8C.2 structured output: core LLM + JSON-mode prompt + parse (no
LangChain).** `RagPipeline.generate_query` and `map_reduce` used a
LangChain structured-output chain for `SearchQueries` / `SummarizedChunk`.
QueryService instead calls the injected core `LLM.chat` with a
JSON-instructed prompt + `response_format={"type":"json_object"}` and
`json.loads` (+ `_json_slice` brace-extraction) into the Pydantic model,
preserving the legacy fallbacks (query-gen: retry once → raw user query;
map-reduce: relevancy=False on any parse error).
- Why: 8H bans LangChain in orchestrators; the plan's explicit "remove
  ChatOpenAI — use LLM factory" intent.
- Alternative: wrap the LangChain chain behind a core-LLM-shaped helper
  outside orchestrators. Rejected — only partially meets the intent and
  keeps a LangChain dependency on the hot path.

**8. 8C.2 streaming + citations live in QueryService; the router is pure
transport.** `chat_stream` drives the proven
`components.utils.stream_with_source_filtering` (reused verbatim — the
100-char buffer that strips `[Sources: N]` mid-stream is delicate);
`chat`/`complete` return the finalized OpenAI dict with the
citation-filtered `extra`. The router maps partition, wraps
`StreamingResponse`/`JSONResponse`, and passes a request-bound
`prepare_sources` callable so `request.url_for` stays in transport.
- Why: the plan's "service owns streaming" Q&A; keeps the proven SSE
  buffer logic intact (no rewrite) while ownership moves to the service.
- Constructor takes `workspace_service` beyond the plan's four (the
  legacy `_prepare_for_chat_completion` validated the workspace via the
  Ray actor; reusing WorkspaceService.get_workspace keeps it Ray-free).
- Consequence: legacy `components/pipeline.py` (RagPipeline /
  RetrieverPipeline) + `map_reduce.py` + `components/retriever.py` are
  now dead code (no router imports `RagPipeline`); **flag for Phase 12
  cleanup** to delete them with the other shims.

**9. 8D.1 indexing dispatch sits behind an `IndexingDispatcher` port +
Ray shim, not direct Ray calls in the service.** The plan's
`IndexingService.__init__(document_repo, workspace_repo, vector_store,
config)` can't reach the `Indexer` / `TaskStateManager` actors. Mirroring
the proven 8C searcher pattern, a new `core/indexing/dispatcher.py` ABC
defines the worker operations the service needs and
`services/storage/indexer_ray_shim.py` adapts the two Ray actors to it;
the container injects it via `from_ray_namespace()` (lazy, like
RetrievalService). `vector_store` and `config` are dropped from the ctor
(the file delete is owned by the Indexer worker; the only config the
legacy router used — data dir, vectordb timeout — is a transport/shim
concern); `dispatcher` is the added arg.
- Why: 8H bans Ray remote calls under `services/orchestrators/` (only
  JobService is excepted); the established way to keep an orchestrator
  Ray-free during the shim is a core port + `services/storage/` shim.
- Alternative: leave the dispatch in the thin router. Rejected — 8G/8D.1
  explicitly move "inline upload+dispatch → indexing_service.add_file()".
- File save to disk + the byte-identical `HTTPException` guards (409
  exists, 404 not-found, 400 bad workspace_ids, 404 unknown workspace,
  404 no object ref) stay in the router (transport + exact legacy body),
  matching the workspaces.py thinning style.
- **Flag for Phase 9:** delete `indexer_ray_shim.py`, have IndexingService
  call the pipeline stages directly.

**10. 8D.2 JobService keeps Ray `.remote` calls (the one 8H exception).**
Per the plan ctor `JobService(task_state_manager)` and the 8H carve-out,
JobService wraps the `TaskStateManager` actor directly — no shim. The
status rollup and `?task_status=` filtering (business logic) move into
the service; `request.url_for` link building stays in the thin queue
router. Container resolves the actor lazily in the cached property
(deferred `utils.dependencies` import) so it is only needed at first
request.
- Why: introducing a shim here would contradict the plan and the
  explicit 8H exception; JobService is the documented hook point for the
  post-refactor DB-backed job tracking (Phase 9).

**11. 8D.1 reuses `components.indexer.utils.files.extract_temporal_fields`
as-is, carrying a transitive module-level `load_config()` and an
`HTTPException`.** IndexingService imports that helper for ISO-8601
metadata parsing. Its module body runs `config = load_config()` at
import time, and the helper raises FastAPI `HTTPException` on a bad
datetime — a transport type leaking into a service.
- Why: it is a pure parsing helper under the shim-exempt `components`
  layer (8H greps the orchestrator file itself, which stays clean), and
  reusing it verbatim keeps the bad-datetime 400 response byte-identical
  with the legacy router. Rewriting it now would risk behavioural drift
  for no Phase-8 benefit.
- Alternative: reimplement the parse in `core/` raising `ValidationError`
  and map it in the router. Rejected for Phase 8 (byte-identical bodies
  are the priority); it is the right move once the shim is gone.
- **Flag for Phase 9:** when `indexer_ray_shim.py` is deleted, move the
  temporal-field parsing into core (raise `ValidationError`, drop the
  module-level `load_config` and the `HTTPException`).

**12. 8E ConversionService — serializer behind a `FileSerializer` port +
shim; chunk lookup through the clean `VectorStore`.** Same pattern as
8C/8D.1: new `core/indexing/serializer.py` ABC + `services/storage/
serializer_ray_shim.py` wrapping the `DocSerializer` actor (it reuses
the legacy `components...serialize_file` helper so the
`call_ray_actor_with_timeout` behaviour the tools router relied on is
preserved). `get_chunk` ports the legacy `get_chunk_by_id` onto
`VectorStore.query_chunks_by_filter({"_id": int(chunk_id)})` and returns
a plain `{"page_content", "metadata"}` dict (no LangChain `Document`),
exactly as PartitionService reads chunks. Ctor deviates from the plan's
`ConversionService(config=config)` → `(serializer, vector_store,
collection)` (the `collection` extra is the established
settings-supplied vector-store name); `config` dropped.
- Why: the plan ctor is underspecified and the 8C/8D shim+port approach
  is the established way to keep the orchestrator Ray-free (8H).
- File save + cleanup IO, tool dispatch, and the byte-identical
  `HTTPException` bodies (404 not-found, 403 forbidden, the tool-error
  4xx/5xx mapping) stay in the thin routers.
- Note: the legacy router's `task_id` came from
  `ray.get_runtime_context().get_task_id()`; outside a Ray task that is
  the driver context, so the shim passes a fixed `"tools-extract"`
  fallback label (the serializer only uses it for logging / state keys —
  no behavioural change).
- **Flag for Phase 9:** delete `serializer_ray_shim.py`, have
  ConversionService call the serializer stage directly.

**13. 8F keeps the lazy-property container (no eager ordered `__init__`);
8H closeout pulls the last Ray call out of a thinned router.** The
plan's 8F snippet builds every orchestrator eagerly in
`ServiceContainer.__init__` in a hand-ordered block. We did *not* adopt
that — the lazy cached-property convention set in 8A.1 (decision 1)
already satisfies "correct instantiation order": dependency resolution
happens on first access (`user_service` → `auth_service` /
`partition_service` / `job_service`; `query_service` →
`retrieval_service` / `workspace_service`), there are no cycles, and the
Ray shims stay un-touched until first request. 8F is therefore a
verification pass, not a rewrite: all nine orchestrators are wired
identically (9 `_x_service` cache slots, 9 properties, 9 providers),
covered by a new `TestPhase8OrchestratorWiring` in `di/test_container.py`.
- During the 8H sweep, `routers/users.py` `/users/info` was found still
  calling `task_state_manager.get_user_pending_task_count` directly (a
  Ray `.remote` + quota math left in a thinned router by 8A.2). It
  passed the literal 8H greps (`vectordb` only) but contradicted "routers
  are thin". Fixed per the plan's "Day 3: fix remaining router rewrites":
  added `JobService.get_user_pending_task_count`, injected `JobService`
  into `UserService` (orchestrator-to-orchestrator, same pattern as
  UserService←PartitionService), moved the quota-usage computation into
  `UserService.get_current_user_info` (byte-identical response), thinned
  the handler. UserService stays Ray-free — the count goes through
  JobService, the one 8H-excepted Ray wrapper.
- `routers/actors.py` still imports `utils.dependencies` Ray handles —
  intentionally out of scope: it is the Ray actor/health admin router,
  not one of the 12 fat routers in the 8G table; it belongs to Phase 9.
- 8H #7 ("no router > 120 lines") is not literally met (indexer 516,
  partition 453, …) and is treated as aspirational, consistent with
  every prior slice: the verbose OpenAPI `description=` docstrings
  dominate the line count; the business-logic extraction — the actual
  goal — is complete and grep-verified (#1–#6 clean).

**14. Phase-8 CI fixes — the §1 "DB-backed flows dormant until Phase 11"
deferral was wrong and is corrected here.** The unit + API CI jobs were
red after Phase 8; four root causes, all behaviour-preserving fixes:
- *Container never initialised (API tests, ~80 failures).* The thinned
  routers resolve repos through the container's *own* `PostgresStore`,
  a separate instance from the one the legacy Ray `Vectordb` actor owns
  and `initialize()`s (`vectordb.py:188`). §1 attached the container but
  never opened its asyncpg pool, so every catalog-backed route 500'd
  against an uninitialised pool. Fix: `main.py` startup/shutdown hooks
  call `container.initialize()/shutdown()` (best-effort, guarded). The
  asyncpg layer is idempotent (Phase 7), so a second store against the
  same DB alongside the actor's is safe. This pulls a thin slice of
  Phase 11 forward — the original deferral broke the live app, which
  Phase 8 must not. Phase 11 still folds this into a real lifespan.
- *Auth router 503 instead of 400 in token mode (3 unit failures).*
  FastAPI resolves `Depends` in declaration order; `Depends(get_auth_service)`
  ran (and 503'd on the absent container) before the in-body
  `_require_oidc_mode()` 400 gate. Fix: `_require_oidc_mode` is now a
  `Depends` declared *before* the service on login/callback/
  backchannel-logout/logout, so token mode 400s without touching the
  container. No behaviour change in oidc mode.
- *`components/utils.py:get_num_tokens` needs an OpenAI key (1 unit
  failure, latent keyless-deploy defect).* It built `ChatOpenAI(...)`
  just to count tokens; client construction requires a non-empty
  api_key (CI mock-vLLM env has none). Fix: fall back to a local
  tiktoken `cl100k_base` encoder when the client can't be built. Prod
  (key present) behaviour is unchanged; the count is equivalent for the
  GPT-3.5/4 family. Also trims LangChain off the QueryService hot path
  (aligned with the 8H intent).
- *Stale `routers/test_auth_router.py` (17 collection errors).* The
  882-line file tested the pre-8A.1 fat router (`routers.auth.OIDCClient`,
  full OIDC/JWT flow). 8A.1 thinned the router but never updated its
  companion test — a Phase-8 omission. All that logic moved to
  `AuthService` and is covered by `services/orchestrators/
  test_auth_service.py`. Replaced with a lean transport test (stub
  service via `dependency_overrides`: AUTH_MODE gate, delegation,
  cookie set/clear, `OIDCFlowError` mapping), matching the phase
  principle "logic tests move to the service".
- *Search response lost `metadata.file_id` (5 API `test_search`
  filtering failures, second pass).* `Chunk.from_langchain` lifts
  `file_id`/`partition`/`page`/`_id` out of the free-form metadata into
  typed `Chunk` fields; the thinned `routers/search.py:_documents`
  returned only `Chunk.metadata`, so the API contract dropped
  `metadata.file_id` (filter tests saw `file_id == None`; `origin` /
  temporal fields survived because they stay free-form). Fix: shape the
  response via `Chunk.to_langchain().metadata`, which merges the typed
  fields back — reproducing the pre-Phase-8 router that returned the raw
  Document metadata. Transport-only shaping, stays in the thin router.

All five CI jobs (Layer guard, Linting, Unit, Integration, API) are
green on the branch after these fixes.

---
## Phase 15 — OIDC group → partition authorization (2026-06-12)

**1. Variable names uniformized onto the shipped `OIDC_*` surface; the
strategy doc's draft names are not reintroduced.**
- Why: `REFACTORING_STRATEGY_v1.md` §Phase 15 (and `REFACTORING_DEV_WORKFLOW.md`)
  sketch a *stateless raw-JWT-bearer* design with its own env vocabulary
  (`OIDC_ENABLED`, `OIDC_ISSUER_URL`, `OIDC_AUDIENCE`, `OIDC_JWKS_CACHE_TTL`,
  `VITE_OIDC_*`). The shipped OIDC is the **session-cookie Authorization
  Code + PKCE** flow (`docs/oidc.md`), which already reads `AUTH_MODE=oidc`,
  `OIDC_ENDPOINT`, `OIDC_REDIRECT_URI`, … The two never coexisted in code, so
  Phase 15 adopts the shipped names as canonical and only adds vars for the
  one genuinely-missing half (group → partition mapping):
  `OIDC_CLAIM_GROUPS`, `OIDC_GROUP_PREFIX`, `OIDC_GROUP_PATTERN`,
  `OIDC_GROUP_SYNC_PRUNE`. Draft names like `OIDC_ISSUER_URL` are *not* read.
- Alternative considered: a reconciliation note in `docs/oidc.md` pointing
  operators at the draft↔shipped mapping. Rejected — `docs/oidc.md` is
  operator-facing; an operator never read the internal drafts, so the note
  was pure noise. The reconciliation lives here and in `phase15.md` §A
  (developer-facing), not in the operator guide.

**2. `OIDC_GROUP_SYNC_PRUNE` is an opt-in flag (default off), not the
strategy doc's unconditional "remove stale".**
- Why: the doc (provisioner step 4) says *"Keycloak is source of truth — add
  missing, update changed, remove stale"* — i.e. delete any membership absent
  from the token on every login. That assumes a greenfield where the IdP is
  the *only* membership source. We're adding to a shipped system where
  memberships are also granted manually via the `/partition/{p}/users` admin
  API; unconditional prune would silently wipe those on the next login. So
  sync is additive + role-update by default, and prune is behind
  `OIDC_GROUP_SYNC_PRUNE=true` for operators who want the doc's literal
  "sole source of truth" behavior. The var name itself is ours (not in any
  doc); only the behavior traces to the strategy doc.
- Alternative considered: prune-by-default to match the doc verbatim.
  Rejected as data-loss-prone for mixed manual+SSO deployments.
- Follow-up (mass-revocation guard): even with prune *on*, removal runs only
  when the token actually **asserts** the groups claim. A missing claim
  (mapper disabled, wrong `OIDC_CLAIM_GROUPS`, scope not granted, transient
  omission) produces an empty ``desired`` that means "no signal", not "revoke
  everything" — without the guard a single config typo would wipe every
  synced user's access on next login (the classic directory-sync footgun). An
  explicit empty list (``groups: []``) is a real signal and still prunes. The
  two failure directions are asymmetric: silent mass-revocation is
  catastrophic, a lingering stale membership is trivially fixable — so we fail
  safe on the missing-claim case.

**3. role → `is_admin` mapping (`OIDC_CLAIM_ROLES` / `OIDC_ADMIN_ROLE`)
deferred, not implemented.**
- Why: the strategy doc puts it in scope, but `docs/oidc.md` documents a
  *hard* boundary — `is_admin` is never writable via claims (same whitelist
  that blocks it in `OIDC_CLAIM_MAPPING`). Honoring the doc here would breach
  a shipped security guarantee. Admin stays an explicit operator action.
- Alternative considered: ship it behind a loud opt-in. Left as an open
  decision for the maintainers rather than bundled into this phase.

**4. Group sync hooks into `AuthService.handle_oidc_callback`, not the
strategy doc's new `jwt_validator`/`oidc_mapper`/`oidc_provisioner` files
or an `api/dependencies/auth.py` dispatch.**
- Why: those files target the stateless-bearer path the session-cookie flow
  obviates. Authentication already runs in `AuthMiddleware` →
  `AuthService.handle_oidc_callback`, where the verified ID-token claims and
  the (already-injected) `membership_repo` are in hand. The work is one pure
  mapper (`services/auth/oidc_groups.py`) plus a `_sync_oidc_memberships`
  step after the existing claim-mapping call — no new dispatch layer, no
  extra IdP round-trip. An unknown partition (FK violation) is logged and
  skipped per-membership so it can never block an otherwise-valid login.

---
## Template for future entries

```
## Phase N — [short title] ([YYYY-MM-DD])

**K. [decision in one line].**
- Why: [what forced the call, what the docs didn't cover].
- Alternative considered: [what else was on the table, why it was rejected].
```
