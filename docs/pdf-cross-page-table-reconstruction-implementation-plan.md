# Cross-page table reconstruction implementation plan

## Objective

Add an optional structural-normalization stage that reconstructs table rows spanning adjacent PDF pages before chunking. The feature must improve retrieval without changing existing indexing unless an administrator enables it.

The detailed architecture and model analysis are documented in [pdf-cross-page-table-reconstruction-design.md](pdf-cross-page-table-reconstruction-design.md).

## First delivery

The first PR includes:

- A structural-normalizer interface.
- A PyMuPDF layout-evidence adapter.
- Deterministic reconstruction across adjacent pages.
- Row-aware chunking for oversized cells.
- Source-fragment provenance.
- Automatic fail-open behavior.
- Backend configuration and snapshot propagation.
- Disabled and automatic choices in the Admin UI.
- Regression, pipeline, and configuration tests.

Strict indexing, VLM fallback, additional layout adapters, automatic enablement, and advanced rollout controls remain follow-up work.

## Pipeline integration

The new stage runs after optional image captioning and before chunking:

1. Parse the document.
2. Preserve the raw parser blocks when automatic reconstruction is enabled.
3. Apply optional image captions to the existing working blocks.
4. Normalize cross-page table structure.
5. Chunk ordinary blocks and reconstructed rows.
6. Continue through contextualization, embedding, and storage unchanged.

Disabled mode bypasses normalization and does not create the additional raw-block snapshot.

## Implementation steps

### 1. Add the backend configuration

Introduce `TableReconstructionConfig` under `IndexationPipelineConfig`.

The first-delivery configuration contains:

| Field | Behavior |
| --- | --- |
| `mode` | `disabled` or `automatic`; defaults to `disabled` |
| `same_table_min_confidence` | Independent threshold; defaults to `0.90` |
| `row_continuation_min_confidence` | Independent threshold; defaults to `0.90` |
| `cell_assignment_min_confidence` | Independent threshold; defaults to `0.90` |
| `algorithm_version` | Backend-owned value; initially `adjacent-layout-v1` |

Each threshold must be between `0.80` and `1.00`. Unknown fields in this nested configuration must be rejected.

The effective configuration is passed through the existing dispatcher and retained in the document indexation snapshot.

### 2. Preserve raw parser blocks

Extend `ProcessedDocument` with an optional `raw_text_blocks` collection.

When automatic mode is enabled, the parse stage captures the parser-produced blocks before captioning can replace image placeholders. The existing `text_blocks` collection remains the working representation used by the current pipeline.

Raw blocks are never modified. They provide the stable source against which reconstructed content and character offsets are verified.

### 3. Extend the processed-document contract

Add typed models for:

- `SourceFragment`
- `PageBoundaryDecision`
- `TableCellData`
- `TableRowData`
- `NormalizationReport`

Extend `TextBlock` with optional provenance and table-row data. Extend `ProcessedDocument` with:

- `raw_text_blocks`
- `normalized_text_blocks`
- `normalization_report`
- `effective_text_blocks()`

The chunker reads `normalized_text_blocks` when normalization produced a safe complete view. Otherwise, it reads the existing `text_blocks`.

### 4. Introduce the normalizer interfaces

`DocumentStructureNormalizer` receives:

- The original `Document`, including the PDF bytes.
- The current `ProcessedDocument`.
- The validated table-reconstruction configuration.

It returns a `ProcessedDocument` and never mutates the raw parser blocks.

`TableLayoutEvidenceProvider` supplies layout evidence independently from the selected primary parser. The reconstruction algorithm must depend on this interface rather than on PyMuPDF directly.

### 5. Build the PyMuPDF evidence adapter

The initial adapter extracts evidence only; it does not decide whether rows should be merged.

For candidate pages it collects:

- Page dimensions and orientation.
- Words and normalized bounding boxes.
- Table regions and column boundaries.
- Cell content and coordinates.
- Distance from content to page boundaries.
- Repeated headers and footers.

PyMuPDF access must use the existing serialized execution mechanism because the library is not thread-safe.

### 6. Implement deterministic adjacent-page reconstruction

Process candidate pages in source order with a table state machine.

For each open table:

1. Record its column schema and inferred identity columns.
2. Keep an unfinished logical row open at the bottom of a page.
3. Inspect only the immediately following page.
4. Determine whether both fragments belong to the same table.
5. Determine whether the leading fragment continues the open row.
6. Assign every contributing text fragment to a column.
7. Inherit empty identity cells from the open row.
8. Close the row when populated identity columns establish a new row.

The implementation must not assume a fixed number of columns or that only the last column can continue.

### 7. Keep confidence decisions independent

Record separate confidence for:

- Whether adjacent fragments belong to the same table.
- Whether the next fragment continues the open row.
- Whether each fragment was assigned to the correct cell and raw-text range.

Automatic reconstruction proceeds only when both boundary decisions and every involved cell assignment pass their respective thresholds. Scores are never averaged.

Contradictory evidence, ambiguous raw-text alignment, a new heading, or incompatible columns must preserve the original content.

### 8. Build a complete normalized block view

Each reconstructed row becomes a `TextBlock` with `block_type="table_row"` and typed `TableRowData`.

The normalized block view must also contain:

- Unaffected content.
- Residual text surrounding reconstructed regions.
- Original content for uncertain regions.

Every reconstructed value references the contributing raw block, page, character range, and optional normalized bounding box. The normalization report records decisions, confidence values, fallback reasons, and algorithm version.

### 9. Add row-aware chunking

A normal-sized row produces one labelled-text chunk.

When a cell exceeds the token budget:

- Reserve room for a compact row-context prefix.
- Split on headings, numbered items, lists, paragraphs, and sentences.
- Use token splitting only as the final fallback.
- Repeat section, table, row identity, inherited values, and content-column name in every part.
- Attach only the source fragments contributing to that part.

This repeated context ensures that every chunk remains useful to embedding, retrieval, and reranking without depending on neighboring chunks.

### 10. Integrate fail-open behavior

Automatic mode catches evidence-extraction failures, timeouts, alignment failures, and unexpected normalizer errors.

An uncertain region keeps the current parser output. A stage-level failure keeps the complete current processed document. The failure reason is recorded without inventing content or silently merging unrelated rows.

### 11. Expose the capability in the Admin UI

Add an advanced PDF/table-processing setting to the indexing preset editor with:

- Disabled
- Automatic

The interface explains that automatic mode is conservative and preserves parser output when reconstruction is uncertain. Threshold controls and strict mode are not exposed in the first PR.

## Regression behavior

The primary regression fixture is pages 803–805 from `LEGITEXT000006070158-1.pdf`:

- Page 803 opens row 1 with `CST salarié` and reference `L. 421-1`.
- Page 804 continues its supporting-documents cell.
- The beginning of page 805 completes row 1.
- Row 2 begins separately later on page 805.

Pages 872–877 are a secondary example covering both table-shaped continuations and continuations emitted as ordinary prose.

The full 904-page local document must not be required by CI. A minimal three-page fixture should cover the primary regression.

## Validation

The test suite must demonstrate:

- Disabled mode preserves current behavior.
- Automatic mode reconstructs the primary regression.
- Raw parser blocks remain unchanged.
- Every reconstructed value is traceable to source fragments.
- All three confidence decisions are enforced independently.
- Ambiguous boundaries fail open.
- Row 2 is not merged with row 1.
- Oversized cells remain within the configured token budget.
- Every split chunk repeats the row identity.
- Non-PDF and ordinary PDF indexing remain unchanged.
- Preset validation, dispatch, and configuration snapshots retain the effective policy.
- The Admin UI reads and submits the automatic mode correctly.

## Migration impact

No PostgreSQL migration is required because presets and indexation snapshots are JSONB.

No Milvus migration is required because table identifiers and provenance use existing dynamic chunk metadata.

Existing presets resolve to disabled, and existing indexed documents remain unchanged. A document must be reindexed with automatic mode enabled to benefit from reconstruction.

## Initial local evaluation

The implementation was evaluated on 28 July 2026.

The three-page regression fixture reconstructed row 1 across source pages 803–805, kept row 2 separate, retained provenance from all three pages, and produced table chunks within the configured test budget. Every chunk for row 1 repeated `CST salarié` and `L. 421-1`.

The complete 904-page source PDF produced:

- 54 normalized logical rows.
- 71 merged adjacent-page boundaries.
- Two uncertain cases preserved through fail-open fallback.
- The expected row starting on page 803 and ending on page 805.

On the development machine, complete PyMuPDF Markdown parsing and normalization took approximately 96.5 seconds with about 150 MB peak resident memory. This is an opt-in cost; disabled mode does not run the evidence adapter or create the raw-block snapshot.

The automated results establish structural correctness for the primary regression. A representative manual sample of the other reconstructed rows is still required before recommending automatic mode for production presets.
