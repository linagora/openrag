# Per-chunk source visualization (chat-openrag)

Date: 2026-06-26
Status: approved

## Goal

In the chat-openrag sources list, render **each retrieved chunk as its own
source entry** instead of deduplicating chunks into one card per file. Each
chunk shows its page in the subtitle and is independently expandable to its own
chunk text.

## Scope

- In scope: per-chunk rendering for document sources, page in subtitle,
  per-chunk expansion, chip counts chunks.
- Out of scope (deferred): twake-specific behavior (file-level dedup + a working
  Cozy file link). Twake sources render per-chunk like everything else for now.
- Web sources unchanged: deduped by URL, no page.

## Behavior

- The sources chip label counts **chunks** ("N sources").
- Expanding the chip lists one card per chunk, in retrieval order.
- Each card: file basename as title; subtitle = `Page {page} · {dir}` when both
  present → `Page {page}` (page only) → `{dir}` (dir only) → empty.
- Clicking a card expands it to that chunk's text, fetched via the chunk's own
  `chunk_url` (`/extract/<chunk _id>`).
- A page of `null` (e.g. docx/text chunks) omits the "Page" prefix.

## Changes

### `src/openrag/normalizeSources.ts`
- `RawDoc` / `NormalizedSource` gain:
  - `page?: number` — read `s.page ?? s.page_number` (wire field is `page`,
    set in `milvus_store.py` as `"page": chunk.page_number`; `page_number`
    kept as a defensive fallback).
  - `chunkId?: string` — from `s._id`, for a stable React key and exact-dup
    dedup.

### `src/components/OpenRagSources.tsx` (`toDisplay`)
- Stop deduping document chunks by file. Emit one `DisplaySource` per chunk.
- Dedupe only *exact* repeats (same chunk id / `chunk_url`).
- `DisplaySource` gains `page?: number`.
- `meta` holds the directory; `page` is structured. Subtitle is built by an
  exported pure helper `formatSubtitle(t, page, dir)` per the rule above, so the
  formatting is unit-testable without rendering. "Page" via i18n key
  `openrag.sources.page` ("Page %{page}").
- `SourceCard` renders `ListItemText primary={name} secondary={formatSubtitle(...)}`;
  expansion uses the chunk's own `extractPath`.

### `src/providers/AppProviders.tsx`
- Add an `openrag` i18n namespace (`openrag.sources.page`) merged into
  `dictRequire` alongside the cozy-search dictionary (no key collision). en/fr
  provided; other langs fall back to en.

### Tests
- `OpenRagSources.spec`: invert the dedup test (3 chunks of one file → 3
  sources); add a case asserting the page subtitle and that null page omits the
  "Page" prefix.
- `normalizeSources.spec`: assert `page` parsed (incl. `page_number` fallback)
  and `chunkId` captured.

## Data flow (plumbing unchanged)

SSE `extra.sources[]` (each a chunk with `page`, `_id`, `source`, `chunk_url`,
`doctype`, `file_id`, `partition`) → `normalizeSources` → `toDisplay` (one card
per chunk) → cozy-ui `List`/`ListItem`.

## Edge cases

- `page` null → omit "Page" prefix.
- Missing `chunk_url` → card renders but is not expandable.
- Web sources → unchanged (dedup by URL, no page).
- Ordering → retrieval order preserved.
