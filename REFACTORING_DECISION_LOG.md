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

## Template for future entries

```
## Phase N — [short title] ([YYYY-MM-DD])

**K. [decision in one line].**
- Why: [what forced the call, what the docs didn't cover].
- Alternative considered: [what else was on the table, why it was rejected].
```
