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

## Template for future entries

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

## Phase 5D — Indexing domain logic + parsers (2026-04-30)

**1. Strip ALL FastAPI from `core/indexing/validators.py`, not just `Depends()`.**
The Phase 5D task description says "Remove FastAPI `Depends()` wrappers", but
the validators also pulled in `Form`, `UploadFile`, `HTTPException`, and
`status`. The new core module imports none of them — pure functions on plain
types (`str`, `dict`, `Iterable[str]`).
- Why: A core-layer module that imports FastAPI is not framework-free. It
  would block reuse from non-HTTP entry points (CLI ingestion, batch jobs,
  unit tests) and re-introduce the boundary violation the refactor exists to
  fix. Removing only `Depends()` would leave the boundary half-broken.
- Alternative considered: literal interpretation — strip `Depends()` only,
  keep `UploadFile` / `HTTPException`. Rejected because it ships a "core"
  module that still depends on the web framework.

**2. Config-driven values become parameters instead of being imported.**
`ACCEPTED_FILE_FORMATS` / `DICT_MIMETYPES` / `FORBIDDEN_CHARS_IN_FILE_ID`
were read from Hydra `config` at module import. The new `validate_file_format`
takes `accepted_formats` and `accepted_mimetypes` as args; the routers supply
them.
- Why: Same boundary reason — `core` should not reach into the global Hydra
  config. Passing values in keeps the validators trivially testable and
  reusable across configurations.
- Alternative considered: keep the module-level config reads inside
  `core/indexing/validators.py`. Rejected — it embeds the infrastructure
  config object into core.

**3. `ValidationError` accepts a custom `status_code` (and `code`) kwarg.**
Phase 1 hardcoded 422 on `ValidationError`. The original validators raised
HTTP 400 (invalid `file_id` / metadata JSON) and HTTP 415 (unsupported file
format). Preserving those codes from a pure-domain exception requires a
status-code override.
- Why: This is a pure refactor; HTTP behaviour must not change. Existing
  precedent in the same module (`LLMParsingError` overrides `status_code`
  after `super().__init__`) shows the pattern is already accepted.
- Alternative considered: (a) consolidate everything on 422 — rejected,
  observable behaviour change; (b) introduce specific subclasses
  (`UnsupportedFileFormatError`, etc.) — rejected as premature, only two
  call sites need non-default codes today. Phase 10's API error-handler
  layer can re-evaluate once it owns the status-code mapping (see Phase 1
  decision #1 follow-up).

**4. HTTP translation flows through the global `openrag_exception_handler`,
not local `HTTPException` raises.**
Validators now raise `ValidationError` (an `OpenRAGError`); `main.py`'s
existing `@app.exception_handler(OpenRAGError)` serialises it.
- Why: Core can't import FastAPI (decision #1), and the handler was already
  wired up in Phase 1.
- Observable change: error body becomes `{"detail": "[CODE]: msg", "extra": {}}`
  instead of FastAPI's `{"detail": "..."}` — matches every other `OpenRAGError`.
- Alternative considered: catch and re-raise as `HTTPException` in the
  router wrappers. Rejected — duplicates the global handler.

**5. Exception shims in `utils/exceptions/` use `core.X`, not `openrag.core.X`.**
The legacy shims imported via `openrag.core.utils.exceptions`. With both
`/app` and `/app/openrag/` reachable, Python loads the same file as two
distinct modules, producing two distinct `OpenRAGError` classes —
`isinstance` failed and decision #4's handler never fired.
- Why: Unifying on the bare `core.X` path matches `pythonpath = ./openrag`
  and the relative-imports-within-`core/` convention (commit 4528c71).
- Follow-up: ~20 other `from openrag.X` imports across `core/`, `config/`,
  and a few components are latent dual-import traps and should be migrated
  in a separate pass.

**6. Native-bytes parsers live in `core/`; service-/Ray-backed parsers live in `services/workers/parsers/`.**
PyMuPDF, html_to_markdown, chardet, and the VLM-caption image parser are
in `core/indexing/parsers/`. Marker (Ray) and LocalWhisper (Ray) live in
`services/workers/parsers/`.
- Why: Ray actors carry `@ray.remote` decoration that imports infrastructure
  at class-definition time. You can't hide that behind a port. Native-bytes
  parsers have no infrastructure dependencies and stay in core.
- Alternative considered: all parsers in core with Ray injected via DI.
  Rejected — class-level decoration can't be deferred to composition.

**7. Type-marker base classes `BasePooledParser` / `BaseClientParser` in core, no vendor names.**
Empty marker subclasses of `DocumentParser` in
`core/indexing/parsers/document_parser.py`. They categorize parsers
(actor-pool vs HTTP-client) without naming the implementation.
- Why: A core base class called `RayPoolParser` or `OpenaiClientParser`
  leaks vendor/infrastructure into the framework-free layer and forecloses
  swapping the backend. Markers carry the capability, not the brand.

**8. Core facades for service-bound parsers receive their pool via DI.**
`core/indexing/parsers/pdf/marker.py` and
`core/indexing/parsers/audio/local_whisper.py` accept any
`BasePooledParser` in `__init__`. The composition layer wires the actual
Ray actor handle (held in services) without core importing Ray.
- Why: Keeps the core facade testable with an in-memory fake and lets
  services own Ray-specific lifecycle.
- Alternative considered: have core facades resolve the actor by name
  themselves. Rejected — couples core to Ray's named-actor registry.

**10. Image preprocessing helpers extracted to
`core/indexing/image_preprocessor.py`.**
Pure helpers (`ensure_png_compatible_mode`, `pil_to_png_bytes`,
`pil_to_base64`, `is_http_url`, `is_data_uri`, `HTTP_IMAGE_PATTERN`,
`DATA_URI_IMAGE_PATTERN`, `MIN_IMAGE_PIXELS`). Used by the core image
parser and by Marker captioning in services.
- Why: Both layers need PNG normalization and markdown image-reference
  detection. Sharing via core (no VLM, no langchain imports) avoids
  services depending on `components/indexer/loaders/base.py`.
- Alternative considered: leave helpers in
  `components/indexer/loaders/base.py`. Rejected — services-importing-
  components is a layering violation, and `base.py` drags in langchain.

**11. `services/workers/ray_utils.py` keeps function AND decorator forms
in one module (no `_retry.py` / `_timeout.py` split).**
STRATEGY plans `services/inference/` to split into `_retry.py` and
`_timeout.py`. For workers we collapsed both back into `ray_utils.py`:
`call_ray_actor_with_timeout` / `@with_timeout` and `retry_with_backoff` /
`@with_retry` (with jitter).
- Why: Workers need both forms in practice — decorator for static-param
  call sites at class definition, function form for dynamic per-call
  values. The decorators delegate to the function form internally;
  splitting across two files would duplicate that wiring.
- Alternative considered: mirror inference verbatim. Rejected — adds
  files that would just import from each other, and the decorator form
  alone can't replace the function form for callsite-resolved timeouts.

**12. Decorator `description` accepts a callable resolved per call.**
`@with_timeout(description=lambda self, path: f"…({path})")` and likewise
for `@with_retry`. The decorator passes the wrapped function's
`(*args, **kwargs)` to the callable at call time.
- Why: Log lines need per-call values (file paths, chunk labels). A
  static string would erase that, sending us back to the function form.
- Alternative considered: keep description static, drop to function form
  when dynamic. Rejected — re-introduces the verbose
  `call_ray_actor_with_timeout(...)` call sites the decorator was meant
  to remove.

**13. Inline `.remote()` calls in workers are extracted into one-line
`@with_timeout`-decorated helpers.**
Each previously inline `call_ray_actor_with_timeout(worker.X.remote(...))`
is now a one-line helper method (`_transcribe_chunk`, `_check_pool_broken`,
`_reset_worker_pool`, `_run_chunk`, `_convert_pdf`) returning the
`ObjectRef`; the decorator awaits it with timeout.
- Why: Eliminates the function-form `call_ray_actor_with_timeout` call
  sites in `whisper_workers.py` and `marker_workers.py` so the worker
  files use only decorator form. Retry-around-timeout semantics
  preserved: `@with_retry` outer, `@with_timeout` inner — `TimeoutError`
  propagates from the inner helper and the outer decorator re-runs the
  whole method body (slot pick, fresh `.remote()`, fresh timeout).
- Alternative considered: keep function form for these inline cases.
  Rejected — leaves a mix of styles in the same file with no clear rule
  for when to use which.

**14. `ray_utils` canonical home moved from `components/` to
`services/workers/`.**
`components/ray_utils.py` is now a back-compat shim re-exporting from
`services.workers.ray_utils`.
- Why: Ray-actor concurrency primitives belong in the services layer,
  not in `components/` (which is on the deprecation path). Routers and
  pipeline still import via the components shim during the transition.
- Follow-up: migrate the remaining `components.ray_utils` imports
  (pipeline, search router, indexer router, workspaces router, indexer
  utils) and delete the shim in Phase 5E.

**17. Captioning is stripped from every parser; it's a downstream stage's job.**
Every parser — generic (Image, Markdown, Docx, Pptx, Eml, Marker) and
VLM-PDF (``DotsOCRPdfClient``) alike — emits ``ImageBlock`` with
``caption=None``. The downstream caption stage fills it in. For
VLM-PDF specifically, that means the picture-bbox crop becomes an
``ImageBlock(image_bytes=…, page_number=N)`` and the parser never
issues the second VLM call.
- Why: One uniform contract beats per-parser carve-outs. Layout-aware
  text extraction and picture-region extraction stay co-resident in
  the parser; only the caption call moved.
- Implication for chunking: the chunker sees the same ``ImageBlock``
  shape from every parser, including ``DotsOCRPdfClient``.

**18. ``ImageBlock.metadata['markdown_ref']`` is the parser→caption contract.**
When a parser puts an image placeholder in its markdown (data-URI,
``![](pptx-image-N)``, ``![](docx-image-N)``, ``![](marker-key)``), it
stores the exact placeholder in ``markdown_ref`` so the caption stage
can ``str.replace`` it. No placeholder ⇒ no ``markdown_ref`` ⇒ caption
stage emits a free-standing ``TextBlock``. Contract is documented on
``ImageBlock`` itself.
- Why: Refs are per-image-unique and chunk-stable. Positional matching
  was rejected as fragile; embedding image bytes inside ``TextBlock``
  was rejected as a heavier model change.

**19. ``ImageBlock`` carries ``source_url`` + an ``image_url`` property; HTTP image refs emit pending-fetch blocks.**
``image_bytes`` defaults to ``b""``. Locally-extracted images set
bytes; HTTP refs (``![alt](https://…)``) leave bytes empty and set
``source_url``. The ``image_url`` property returns
``data:{mime};base64,…`` when bytes are present, else ``source_url``.
- Why: Legacy ``MarkdownLoader`` captioned HTTP images (langchain
  ``ChatOpenAI`` accepts URLs natively). The new VLM ABC takes bytes
  only, so a fetch stage has to populate them — but the parser still
  emits one ``ImageBlock`` per in-text image, keeping the contract
  uniform. Consumers read ``image_url`` regardless of shape.

**20. Paginated parsers emit `list[TextBlock]` with `page_number`; in-band `[PAGE_N]` markers are gone.**
Marker and PPTX previously concatenated all page content into one
``TextBlock`` with ``[PAGE_N]`` markers between pages. They now emit
one ``TextBlock`` per page with ``page_number`` set, matching what
PyMuPDF already does. Parsers without natural pagination
(text/html/md/docx/doc/eml/whisper/image) still emit a single
``page_number=1`` block.
- Why: Pagination is metadata, not content. Leaking ``[PAGE_N]``
  markers into chunk text forced every consumer to know the marker
  syntax; ``TextBlock.page_number`` is the canonical channel and was
  already half-used.
- Implication for chunking: the chunker must NOT scan for
  ``[PAGE_N]`` markers. Iterate ``ProcessedDocument.text_blocks`` and
  carry ``block.page_number`` onto every emitted chunk. Page boundaries
  are block boundaries.

**16. Docling and DoclingV2 PDF backends deferred — not migrated in Phase 5D.**
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

**21. ``ClientPdfParser`` / ``ClientAudioParser`` as generic client-backed facades.**
The PDF facade was renamed ``OpenAIPdfParser`` → ``ClientPdfParser``
(``core/indexing/parsers/pdf/openai.py`` → ``pdf/client_based.py``);
an analogous ``ClientAudioParser`` was added at
``core/indexing/parsers/audio/client_based.py``. Both accept any
``BaseClientParser`` and delegate ``parse()``.
- Why: "OpenAI" was a leaky model-specific label on a class that takes
  any HTTP-client-backed parser. Whatever DotsOCR / Whisper-vLLM /
  Scaleway-Speech is called next quarter, the facade stays the same —
  what varies is the injected ``BaseClientParser``.
- Alternative considered: keep one model-specific facade per backend.
  Rejected — duplicates the same isinstance + delegate boilerplate.

**22. ``BaseOpenAIPdfClient`` is scaffolding only — no opinionated pipeline.**
Provides reusable helpers (PDF page rendering, semaphore-protected
``_ocr_one(page_img, prompt) → str | None``, JSON-fence stripping,
JSON loading, picture-bbox cropping). It does **not** define
``parse()``, a ``PROMPT`` class attribute, or abstract
``_caption_images`` / ``_result_to_md`` / ``_parse_ocr_response``
hooks. The file was renamed ``_openai.py`` → ``_base_openai_parser.py``
to match the new role.
- Why: The previous abstract pipeline imposed assumptions ("there's
  one OCR response per page", "captioning is a parser concern") that
  didn't generalise. Treat the base as a toolbox; let each concrete
  client (DotsOCR, future variants) drive its own ``parse()`` and
  block-emission strategy.
- Trade-off: more code per concrete subclass. Accepted — model-specific
  variation (response schema, block layout, bbox handling) lives in
  the subclass anyway.

**23. DotsOCR response is validated through Pydantic.**
``DotsOCRElement`` / ``DotsOCRPage(RootModel[list[DotsOCRElement]])``
/ ``DotsOCRCategory`` (Enum) capture the layout-element shape;
``DotsOCRPdfClient._parse_page`` runs ``model_validate`` and returns
``None`` on bad payloads. The ``{"items": [...]}`` envelope is
tolerated alongside a bare list.
- Why: Replaces dict shuffling (``page_res.get("category") ==
  "Picture"``, ``item.get("bbox")``) with typed access
  (``element.category is DotsOCRCategory.PICTURE``, ``element.bbox``).
  Bad payloads fail loudly via ``ValidationError`` instead of silently
  returning empty markdown.

**24. ``OpenAIAudioClient`` keeps language detection as an injected callable, not a Ray ref-getter.**
Legacy ``AudioTranscriber`` looked up a ``WhisperActor`` Ray actor by
name. The new ``OpenAIAudioClient`` takes
``language_detector: Callable[[Path], Awaitable[str | None]] | None``
in its constructor and skips detection when ``None`` (vLLM
auto-detects).
- Why: Keep the client free of Ray coupling so it can be instantiated
  and tested without a Ray cluster. The wiring layer passes a closure
  that calls the Whisper actor when ``USE_WHISPER_LANG_DETECTOR=true``.
- Alternative considered: keep the Ray actor lookup inside the client
  guarded by a config flag. Rejected — pulls Ray into the
  ``services/inference`` layer where the rest of the file is plain
  HTTP.

---

## Template for future entries

```
## Phase N — [short title] ([YYYY-MM-DD])

**K. [decision in one line].**
- Why: [what forced the call, what the docs didn't cover].
- Alternative considered: [what else was on the table, why it was rejected].
```
