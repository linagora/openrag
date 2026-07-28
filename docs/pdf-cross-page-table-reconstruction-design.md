# Cross-page table row reconstruction during PDF indexing

## Goal

Improve retrieval from PDFs in which a logical table row spans multiple pages. Continuation pages may contain text in only one or two columns while visually inheriting identifying values such as the row title, reference, or category from an earlier page.

The solution should reconstruct logical rows before chunking, preserve their inherited context, and remain independent of any one PDF parser.

This capability must be optional and controlled by the partition's indexing preset. Existing presets must keep the current indexing behavior unless an administrator explicitly enables reconstruction.

## Current indexing pipeline

OpenRAG currently processes an uploaded document through the following stages:

```text
Upload
  → resolve the partition indexing preset
  → dispatch to an indexing worker
  → parse into page-level text blocks
  → optionally caption images
  → chunk the extracted text or Markdown
  → optionally contextualize and tag chunks
  → embed chunk text
  → store chunks and metadata
```

Marker, PyMuPDF, and Docling ultimately expose page-level text blocks. These blocks preserve text and page numbers but do not describe columns, tables, rows, or relationships across page boundaries.

The current chunker joins page blocks using synthetic page markers and recognizes complete Markdown tables. It can group continuation rows when an empty first column already exists inside the same valid Markdown table. It cannot restore a relationship once the parser has emitted the continuation as a separate table or as ordinary prose.

Table splitting also happens between row groups. A single oversized logical row is therefore not subdivided safely while retaining its identity.

Embedding and reranking operate on the final chunk text. Metadata alone cannot compensate for missing row context.

## Findings from the sample PDF

The supplied `LEGITEXT000006070158-1.pdf` contains 904 pages.

### Primary regression: pages 803–805

The original regression is the Annex 10 table across PDF pages 803–805:

- Page 803 contains the table header and starts row 1: `CST portant la mention "salarié"`, reference `L. 421-1`.
- Page 804 contains only the continuation of row 1's `Pièces justificatives` cell. Its first four columns are visually inherited from page 803. PyMuPDF's table detector does not identify a table on this page.
- Page 805 begins with the final continuation of row 1 in the fifth column.
- Row 2, `CST portant la mention "travailleur temporaire"`, reference `L. 421-3`, begins later on page 805.

This is the required regression fixture for the first delivery.

### Secondary example: pages 872–877

Pages 872–877 contain another instance of the same document pattern:

- Row 53 starts on page 873 and continues at the beginning of page 874.
- Row 54 starts later on page 874 and continues throughout page 875.
- On page 875, PyMuPDF emits the continuation as ordinary prose without its table or row identity.
- Row 55 begins on page 876 and continues onto page 877.
- Page 877 begins with four empty cells containing the continuation of row 55, followed by the next complete row.

This produces two different failure modes:

1. The continuation remains table-shaped, but its inherited columns are missing.
2. The continuation no longer looks like a table and becomes ordinary page text.

Improving Markdown table parsing alone cannot handle both cases.

## Best integration point

Add a structural-normalization stage after optional image captioning and before chunking:

```text
Parse
  → caption
  → normalize document structure
  → chunk
  → contextualize
  → embed
  → store
```

At this point:

- The complete parsed document is still available.
- Image placeholders have already been resolved.
- Page relationships have not yet been destroyed by chunking.
- The original PDF remains available for obtaining layout evidence.
- The implementation can work independently of the selected parser.

Reconstruction should not live inside the parser because that would duplicate behavior across Marker, PyMuPDF, and Docling. It should not live only inside the chunker because detecting page relationships and splitting content are different responsibilities.

The pipeline should resolve the reconstruction policy from the effective indexation configuration before running the stage. In disabled mode, the stage must be bypassed so the resulting behavior is equivalent to the current pipeline.

## Preset configuration and operating modes

The complete feature should provide three modes:

| Mode | Behavior |
| --- | --- |
| Disabled | Bypass reconstruction and preserve the current parser and chunker behavior |
| Automatic | Detect likely cross-page tables and reconstruct only high-confidence cases |
| Strict | Require reliable reconstruction for detected complex tables and fail indexing when safe reconstruction is not possible |

The initial default must be disabled for backward compatibility. Existing presets and configuration snapshots that do not contain the option should resolve to disabled.

Automatic mode may become the recommended default after accuracy, latency, and retrieval-quality evaluation. Changing the recommendation later must not silently modify existing presets or previously stored indexation snapshots.

The first delivery implements disabled and automatic modes. Strict mode remains part of the target design but is deferred until automatic reconstruction has been evaluated. The Admin UI must not present strict mode before the backend implements its failure contract.

### Backend ownership

The backend is the source of truth for this capability. It must:

- Validate the selected mode.
- Validate confidence and detection thresholds against safe server-side ranges.
- Resolve missing values to backward-compatible defaults.
- Persist the effective values in the document's indexation configuration snapshot.
- Pass the effective configuration through the existing dispatcher and indexing worker.
- Apply the same validation regardless of whether a preset is created through the Admin UI or the API.

Thresholds should have safe backend defaults. The UI does not need to expose every internal heuristic initially; exposing the operating mode is required, while expert threshold controls can remain optional.

Persisting the effective configuration is important because reconstruction changes indexed content. Operators must be able to determine which policy and algorithm version produced a document's chunks.

### Admin UI

The indexing preset editor should expose the mode under advanced PDF parsing or table-processing settings.

The interface should:

- Explain that disabled preserves current behavior.
- Describe automatic as conservative and fail-open.
- Keep the option independent of the selected primary PDF parser.
- Avoid suggesting that automatic mode guarantees reconstruction for every table.

When strict mode is implemented in a later delivery, the interface must warn that it can cause indexing jobs to fail.

The Admin UI sends the requested preset configuration, but it must not duplicate or replace backend validation.

## Architecture options

| Architecture | Advantages | Limitations |
| --- | --- | --- |
| Stitch Markdown tables inside the chunker | Small and inexpensive change | Depends on valid Markdown and fails when continuation pages become ordinary prose |
| Enhance each PDF parser | Can use parser-specific structural data | Duplicates logic and produces inconsistent behavior across parsers |
| Add a parser-independent structural-normalization stage | Generic, deterministic, testable, and reusable | Requires a logical table representation and an additional layout-analysis step |
| Use a VLM or external document-analysis service | Can resolve difficult visual cases | More expensive, slower, and non-deterministic |

The parser-independent normalization stage is the recommended foundation. A VLM or external service should only be an optional resolver for ambiguous cases.

Parser-independent means that the reconstruction algorithm is not coupled to Marker, PyMuPDF, or Docling. It does not mean that every parser provides equal evidence. Reconstruction quality depends on the text and layout evidence available for a document and must degrade safely when that evidence is incomplete.

## Recommended architecture

### Structural normalizer

Introduce a document-structure normalizer that receives both the parsed document and the original document. Its responsibility is to identify table fragments, resolve page-boundary relationships, and produce logical table-row blocks.

The normalizer must preserve the existing parser contract. It returns another `ProcessedDocument`; it does not introduce a parallel indexing pipeline.

Resolved rows are exposed through the existing block contract as `TextBlock` instances with `block_type="table_row"` and typed table-row data. This lets the current chunker consume normalized blocks without changing parser outputs.

The normalizer should receive a validated policy rather than reading environment variables or UI state directly. This keeps behavior deterministic for a given indexation snapshot and makes the stage straightforward to test.

### Exact input and output contract

The normalizer interface receives:

- The original `Document`, including content type and PDF bytes.
- The parser-produced `ProcessedDocument`.
- The validated table-reconstruction configuration from the effective indexing preset.

It returns a `ProcessedDocument` with the following guarantees:

- `raw_text_blocks` contains the untouched parser output captured before image captions can replace placeholders. It is populated only when reconstruction is enabled.
- `text_blocks` remains the existing working block view, including any caption substitutions already performed by the current pipeline.
- `normalized_text_blocks` is `None` when no safe normalization was produced.
- When normalization succeeds, `normalized_text_blocks` is a complete chunkable view of the document in source order. It contains unaffected text, residual text around table regions, and reconstructed `table_row` blocks.
- Every normalized block references its source slices through typed source fragments.
- The chunker uses `normalized_text_blocks` when present and otherwise uses the raw `text_blocks`.
- A normalization report records decisions and fallback reasons without replacing or mutating source content.

The existing document models should be extended with the following exact contract:

| Model | Required data |
| --- | --- |
| `SourceFragment` | Source block index, source page number, inclusive start and exclusive end character offsets, and an optional page-normalized bounding box |
| `PageBoundaryDecision` | Previous and next page numbers, separate same-table and row-continuation confidence values, decision (`merged` or `preserved`), and reason |
| `TableCellData` | Column index, optional column name, text, source fragments, and cell-assignment confidence |
| `TableRowData` | Deterministic table and row identifiers, algorithm version, inferred identity-column indexes, optional table title, section path, ordered cells, start and end pages, and the page-boundary decisions used to reconstruct it |
| `NormalizationReport` | Algorithm version, status (`unchanged`, `normalized`, or `partial_fallback`), all page-boundary decisions, reconstructed-row count, and fallback reasons |

`TextBlock` gains optional `source_fragments` and `table_row` fields. `ProcessedDocument` gains optional `raw_text_blocks`, `normalized_text_blocks`, and `normalization_report` fields plus an `effective_text_blocks()` accessor. All new fields have backward-compatible defaults.

Source references must point to character ranges in `raw_text_blocks`. If layout or captioned text cannot be aligned reliably with parser text, the cell-assignment confidence must remain below the automatic threshold and that region must use the existing working block without reconstruction.

This makes normalization reversible and debuggable: an operator or test can compare the normalized row with the exact parser slices that produced it, and the algorithm version defines the deterministic whitespace and joining rules used to derive the row.

### Evidence providers

The normalizer should combine:

- Page text and Markdown from the selected parser.
- Lightweight PDF layout evidence such as word positions, normalized column boundaries, table regions, and page coordinates.

PyMuPDF can provide the first layout adapter because it is already available, but it should remain behind an interface. It supplies evidence and is not expected to reconstruct the table perfectly.

Future adapters could consume richer Docling output, Marker structure, OCR results, or another document-analysis service without changing the reconstruction algorithm.

Only the PyMuPDF layout-evidence adapter is included in the first delivery. Additional adapters are follow-up work.

### Candidate detection

Detailed layout processing should run only on likely table regions and their adjacent pages. Candidate signals include:

- Markdown or geometric table-like structure.
- Text aligned into stable column bands.
- A table approaching the lower page boundary.
- Content beginning near the upper boundary of the following page.
- Continuation text concentrated in only a subset of the previous table's columns.

Candidate windows should expand until the end of the logical table is found. This avoids an expensive second table-analysis pass over every page of large documents.

### Deterministic continuation resolver

The default resolver should operate as a page-boundary state machine. A boundary can be considered a continuation when several signals agree:

- Pages are adjacent and have compatible dimensions and orientation.
- Column boundaries remain similar after normalization against page width.
- The preceding table reaches the lower content area.
- The next fragment begins near the upper content area.
- Headers either match or are recognized as repeated headers.
- Previously identifying columns are empty while content continues in another column.
- No heading, caption, or unrelated section occurs between the fragments.

Repeated document headers and footers must be excluded before matching.

The resolver must not assume that the first four columns identify a row or that only the final column can continue. Identity columns should be inferred from the table's observed row starts and value patterns.

When the next fragment has empty identity cells, their values are inherited from the open logical row. A new logical row begins when those identity columns become populated again.

### Confidence model

Confidence must not be represented by one aggregate score. The resolver records and evaluates three independent values:

- **Same-table confidence:** whether fragments on adjacent pages belong to the same logical table.
- **Row-continuation confidence:** whether the leading fragment on the next page continues the currently open logical row.
- **Cell-assignment confidence:** whether a text fragment was assigned to the correct table column and aligned to the correct parser-text range.

Automatic reconstruction occurs only when the same-table and row-continuation thresholds pass and every cell assignment used by the merge passes its own threshold. Scores must not be averaged because a strong table match must not hide an uncertain row or cell assignment.

The initial automatic thresholds are backend-owned and independently configurable. The first delivery uses defaults of `0.90` for all three thresholds and validates each value within the safe range `0.80–1.00`.

The persisted nested configuration is:

| Field | First-delivery contract |
| --- | --- |
| `mode` | `disabled` or `automatic`; default `disabled` |
| `same_table_min_confidence` | Float from `0.80` to `1.00`; default `0.90` |
| `row_continuation_min_confidence` | Float from `0.80` to `1.00`; default `0.90` |
| `cell_assignment_min_confidence` | Float from `0.80` to `1.00`; default `0.90` |
| `algorithm_version` | Backend-owned literal identifying the deterministic algorithm; initial value `adjacent-layout-v1` |

Unknown fields are rejected inside this nested configuration. The algorithm version is persisted with the effective indexation snapshot so a document can be reproduced after heuristics evolve.

### Logical table representation

The normalized representation should preserve:

- Stable table and row identifiers.
- Table title and section path.
- Column schema.
- Reconstructed cell values.
- Source fragments and their page provenance.
- Start and end pages.
- Reconstruction method, confidence, and evidence.

Identifiers should be deterministic so reindexing the same document produces comparable metadata.

### Row-aware chunking

The chunker should handle normalized table rows separately from ordinary text.

A normal-sized row should produce one table chunk containing labelled cells.

For an oversized cell:

- Reserve space for a compact context prefix.
- Include the section, table identity, row identity, and inherited column values.
- Split content on headings, numbered sections, list items, paragraphs, and sentences.
- Use token-based splitting only as a final fallback.
- Repeat the context prefix in every generated chunk.
- Keep each final chunk within the configured token budget.

A self-contained chunk should convey the following information:

```text
Section: …
Table: …
Row: Number 54; Category …; Title …; Reference L. 426-7
Supporting documents, part 2 of 4: …
```

The final representation does not need to remain a Markdown table. Labelled text is generally clearer for embeddings and reranking.

## Metadata and downstream behavior

Each generated table chunk should carry compact metadata describing:

- Table and row identifiers.
- Table title and section.
- Row identity.
- Content column.
- Source page start and end.
- Chunk part number and total parts.
- Reconstruction method and confidence.
- Source-fragment provenance for the content included in that chunk.

For compatibility, the existing page value should remain the starting page. The ending page can be added as metadata and later exposed in source citations.

The row context must also be included in the chunk text because embeddings operate on text, not metadata. The embedding and storage stages otherwise require no behavioral change.

Optional LLM contextualization can continue to run after chunking. It may enrich the chunk, but it should not be responsible for reconstructing table relationships.

## Ambiguity and failure handling

False merges can be more damaging than missed merges, so reconstruction should be conservative.

- High-confidence boundaries are reconstructed automatically.
- When row identity is certain but exact cell alignment is uncertain, preserve the fragment and attach only the confirmed context.
- When row identity is uncertain, preserve the original extraction and record the ambiguity.
- Never introduce content that cannot be traced back to an extracted source fragment.
- Never silently merge unrelated rows.

The resolver should expose an extension point for advanced decisions. A future VLM could inspect only ambiguous page pairs, while the deterministic resolver remains the default.

Failure behavior must follow the configured mode:

- Disabled mode does not run detection or reconstruction.
- Automatic mode fails open. Uncertain boundaries and reconstruction errors preserve the original parser output and are reported through logs and metrics.
- Strict mode fails the indexing job when a detected complex table cannot be reconstructed with the required confidence. The error should identify the reconstruction stage and affected page boundary without exposing document content unnecessarily.

Strict mode should not reject an ordinary document merely because no complex table was detected. It applies the reliability requirement only after the detector identifies a table that requires cross-page reconstruction.

Strict mode is outside the first delivery. The first delivery must still define the normalizer and stage interfaces so strict failure behavior can be added without changing their input or output contracts.

## Scalability and observability

The normalizer should process candidate page windows incrementally and avoid loading rendered images for deterministic reconstruction.

The raw parser-block snapshot is created only in automatic mode and released with the in-memory processed document after indexing, so disabled mode has no additional document-copy cost.

Operational metrics should include:

- Structural-normalization duration.
- Candidate boundaries detected.
- Logical rows reconstructed.
- Continuations merged.
- Ambiguous boundaries.
- Reconstruction fallbacks and failures.

Reconstruction metadata and the persisted indexation snapshot should include the selected mode, effective thresholds, and algorithm version so that changes can be evaluated and documents can be selectively reindexed.

## First delivery scope

The first delivery is intentionally limited to:

- The structural-normalizer interface.
- The PyMuPDF layout-evidence adapter.
- Deterministic reconstruction across adjacent pages.
- Row-aware chunking of oversized cells.
- Source-fragment provenance.
- Automatic fail-open behavior.
- Backend validation and snapshot propagation required to keep the feature optional.
- Disabled and automatic choices in the Admin UI.
- Regression and pipeline tests.

The first delivery does not include:

- VLM fallback.
- Strict indexing behavior.
- Layout adapters other than PyMuPDF.
- Automatic activation for existing presets.
- Extensive rollout controls or confidence-tuning controls in the Admin UI.

## Exact implementation plan

### Domain models and interfaces

| File | Change |
| --- | --- |
| `openrag/core/models/document.py` | Add `SourceFragment`, `PageBoundaryDecision`, `TableCellData`, `TableRowData`, and `NormalizationReport`. Extend `TextBlock` with provenance and optional row data. Extend `ProcessedDocument` with `raw_text_blocks`, `normalized_text_blocks`, `normalization_report`, and `effective_text_blocks()`. |
| `openrag/core/indexing/structure_normalizer.py` | Add the `DocumentStructureNormalizer` and `TableLayoutEvidenceProvider` interfaces and the page/table evidence models exchanged between them. |
| `openrag/core/indexing/table_normalizer.py` | Add `DeterministicTableNormalizer`, candidate-window detection, adjacent-page state handling, independent confidence decisions, parser-text alignment, raw-block preservation, and fail-open output. |

`DocumentStructureNormalizer.normalize` accepts a `Document`, a `ProcessedDocument`, and a validated table-reconstruction configuration. It returns a `ProcessedDocument` under the contract defined above.

### PyMuPDF evidence

| File | Change |
| --- | --- |
| `openrag/core/indexing/parsers/pdf/pymupdf_runtime.py` | Centralize serialized PyMuPDF execution so parsing and layout evidence cannot call the non-thread-safe library concurrently. |
| `openrag/core/indexing/parsers/pdf/pymupdf.py` | Use the shared PyMuPDF runtime without changing parser output. |
| `openrag/services/workers/layout/__init__.py` | Introduce the layout-adapter package. |
| `openrag/services/workers/layout/pymupdf_table_evidence.py` | Add `PyMuPDFTableEvidenceProvider`, extracting normalized page geometry, table fragments, cells, and text bands only for candidate page windows. |

The adapter supplies evidence; it does not decide whether rows should be merged.

### Pipeline integration

| File | Change |
| --- | --- |
| `openrag/services/workers/stages/parse.py` | Add a `preserve_raw_blocks` option. When automatic reconstruction is enabled, snapshot parser-produced blocks into `raw_text_blocks` before captioning. Disabled mode keeps the current allocation and behavior. |
| `openrag/services/workers/stages/normalize_structure.py` | Add the pipeline stage. Disabled mode bypasses it. Automatic mode catches detection, alignment, and timeout failures and preserves the current processed document. |
| `openrag/services/workers/pipeline_builder.py` | Inject the normalizer, add a structure-normalization timeout to `PipelineTimeouts`, run the stage after captioning and before chunking, include its timing, and pass the validated policy from the effective indexation config. |
| `openrag/services/workers/indexer_pool.py` | Construct one PyMuPDF evidence provider and deterministic normalizer per indexing worker, inject them into the pipeline, and use the existing loader parse timeout as the initial normalization bound. |
| `openrag/services/workers/stages/chunk.py` | Continue passing a `ProcessedDocument`; no parallel chunking pipeline is introduced. |

### Row-aware chunking files

| File | Change |
| --- | --- |
| `openrag/core/chunking/recursive.py` | Read the processed document's effective block view. Keep the existing path for ordinary blocks and route `table_row` blocks through row-aware chunking. |
| `openrag/core/chunking/table_rows.py` | Add deterministic row serialization and oversized-cell splitting. Reserve space for repeated row context, split semantically, enforce the token limit, and attach provenance and page-range metadata to each table chunk. |

Every emitted table chunk includes row identity in its text and the relevant source fragments in its metadata.

### Configuration, validation, and snapshot propagation

| File | Change |
| --- | --- |
| `openrag/core/config/table_reconstruction.py` | Add `TableReconstructionConfig`. The first delivery accepts `disabled` and `automatic`, defaults to disabled, and validates the three independent thresholds. |
| `openrag/core/config/indexation_pipeline.py` | Add the nested `table_reconstruction` field to `IndexationPipelineConfig`. |
| `openrag/api/schemas/admin/preset_schemas.py` | Add available reconstruction modes to the preset-options response. |
| `openrag/api/routers/admin/presets.py` | Return backend-supported reconstruction modes from the preset-options endpoint. |
| `openrag/services/orchestrators/preset_service.py` | Continue validating through `IndexationPipelineConfig`; add coverage that invalid modes, unknown nested fields, and unsafe thresholds are rejected. Existing seed dictionaries remain sparse and therefore resolve to disabled. |
| `openrag/services/orchestrators/indexing_service.py` | No new dispatch path. Confirm by test that the effective nested configuration is included in the existing `model_dump` passed to the dispatcher. |
| `openrag/services/workers/indexer_actor.py` | No new persistence path. Confirm by test that the existing file snapshot stores the effective nested configuration. |

The nested table-reconstruction model should reject unknown fields so API typos cannot be silently ignored.

### Admin UI files

| File | Change |
| --- | --- |
| `ui/src/lib/api/presets.ts` | Add reconstruction modes to `PresetOptionsResponse`, keeping the field optional for rolling-deployment compatibility. |
| `ui/src/pages/admin/preset-config.ts` | Add pure helpers for reading and updating the nested reconstruction mode. A missing value displays as disabled. |
| `ui/src/pages/admin/presets.tsx` | Add an Advanced PDF/Table processing section with Disabled and Automatic choices and concise fail-open guidance. Do not expose thresholds or strict mode in the first delivery. |

The backend remains authoritative even when an older or modified client submits the configuration directly.

### Tests

| Test file | Coverage |
| --- | --- |
| `tests/resources/cross_page_table_rows_803_805.pdf` | Minimal three-page regression fixture extracted from source pages 803–805. |
| `tests/unit/core/indexing/test_table_normalizer.py` | Candidate detection, page 803→804 and 804→805 decisions, inherited cells, row 2 beginning mid-page, three independent confidence thresholds, provenance, reversibility, and ambiguous fail-open behavior. |
| `tests/unit/services/workers/layout/test_pymupdf_table_evidence.py` | Geometry and cell evidence from the regression fixture, including page 804 where no complete table is detected. |
| `tests/unit/core/chunking/test_table_rows.py` | Oversized-cell semantic splitting, repeated row identity, token budgets, page ranges, and per-chunk source fragments. |
| `tests/unit/core/chunking/test_recursive.py` | Raw-block fallback and selection of normalized blocks when present. |
| `tests/unit/services/workers/stages/test_parse.py` | Raw parser blocks are captured before captioning only when automatic reconstruction is enabled. |
| `tests/unit/services/workers/stages/test_normalize_structure.py` | Disabled bypass, successful automatic normalization, timeout, exception, and fail-open preservation. |
| `tests/unit/services/workers/test_pipeline_builder.py` | Stage ordering, effective preset policy, PDF-only invocation, and unchanged non-PDF behavior. |
| `tests/unit/services/orchestrators/test_preset_service.py` | Default-disabled behavior and rejection of invalid modes, thresholds, and unknown nested fields. |
| `tests/unit/services/orchestrators/test_indexing_service.py` | Effective reconstruction configuration reaches the existing dispatcher. |
| `tests/unit/services/workers/test_indexer_worker.py` | Effective reconstruction configuration is retained in new-file and replacement snapshots. |
| `tests/integration/api/test_presets.py` | Preset options, automatic-mode round trip, validation errors, and backward-compatible omission. |
| `ui/src/pages/admin/preset-config.test.ts` | Missing configuration displays disabled and updates remain immutable. |
| `ui/src/pages/admin/presets.test.tsx` | Mode rendering, user guidance, and submitted automatic configuration. |

The local 904-page PDF must not be required by CI. The committed three-page fixture is the reproducible regression input.

## Migration impact

No relational database migration is required. Pipeline presets and document indexation snapshots are already stored as JSONB.

No Milvus collection migration is required. Reconstruction provenance and table identifiers use the existing dynamic chunk metadata.

Existing presets without `table_reconstruction` resolve to disabled. Existing indexed chunks are unchanged; documents benefit only after being reindexed with automatic mode enabled.

The processed-document changes are in-memory model extensions with backward-compatible defaults. The preset-options API receives an additive field, and the Admin UI treats it as optional for mixed-version deployments.

Strict mode will require a later additive configuration and UI change, but no database migration.

## Acceptance criteria

- A logical row spanning multiple pages is reconstructed before chunking.
- Empty continuation columns inherit the values of the open row.
- A new logical row beginning partway through a later page is detected correctly.
- Oversized cells produce bounded semantic chunks.
- Every chunk remains understandable without its neighboring chunks.
- Table identity, row identity, section, and page range are preserved.
- The behavior is independent of the selected primary PDF parser.
- Non-table documents and ordinary single-page tables retain their current behavior.
- Ambiguous cases do not result in invented or silently reassigned content.
- Retrieval improves for queries combining row identity with continuation content.
- Disabled is the initial default and preserves existing indexing behavior.
- Automatic mode reconstructs only high-confidence cases and otherwise preserves the original parser output.
- The first-delivery Admin UI exposes disabled and automatic modes.
- The backend validates the mode and thresholds and remains authoritative.
- The effective policy and algorithm version are retained in the indexation configuration snapshot.
- Raw parser blocks remain unchanged and every reconstructed row is traceable to them.
- Same-table, row-continuation, and cell-assignment confidence are evaluated independently.
