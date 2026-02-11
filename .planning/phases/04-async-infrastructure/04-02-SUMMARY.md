---
phase: 04-async-infrastructure
plan: 02
subsystem: loaders
tags: [async, performance, file-io]
dependency-graph:
  requires: [04-01]
  provides: [async-loader-file-io]
  affects: [loaders]
tech-stack:
  added: []
  patterns: [asyncio-to-thread-for-blocking-io]
key-files:
  created: []
  modified:
    - path: openrag/components/indexer/loaders/eml_loader.py
      changes: Wrapped file reads, temp file ops, and cleanup with asyncio.to_thread
    - path: openrag/components/indexer/loaders/docx.py
      changes: Wrapped MarkItDown converter and zipfile extraction with asyncio.to_thread
    - path: openrag/components/indexer/loaders/doc.py
      changes: Wrapped Spire.Doc operations with asyncio.to_thread
    - path: openrag/components/indexer/loaders/image.py
      changes: Wrapped PIL.Image.open and cairosvg.svg2png with asyncio.to_thread
    - path: openrag/components/indexer/loaders/pdf_loaders/pymupdf.py
      changes: Wrapped pymupdf4llm.to_markdown with asyncio.to_thread and updated save_content calls
    - path: openrag/components/indexer/loaders/pptx_loader.py
      changes: Wrapped PPTXConverter.convert with asyncio.to_thread
decisions:
  - Use asyncio.to_thread pattern from media_loader.py for consistency
  - Create sync helper methods (_read_file_bytes, _write_temp_file, _convert_doc_to_docx, _load_image) to encapsulate blocking operations
  - Update all save_content calls to use await (inheriting async behavior from Plan 01)
metrics:
  duration: 3 min
  tasks-completed: 2
  files-modified: 6
  commits: 2
  tests-verified: 93
completed: 2026-02-11
---

# Phase 04 Plan 02: Async Loader File I/O Summary

**One-liner:** Converted 6 loaders with blocking file I/O to use asyncio.to_thread, eliminating event loop blocking during file operations

## Objective

Convert all 6 loaders with blocking file I/O (EmlLoader, DocxLoader, DocLoader, ImageLoader, PyMuPDF4LLMLoader, PPTXLoader) to use asyncio.to_thread, eliminating event loop blocking during file operations (PERF-02 from research).

## Work Completed

### Task 1: Convert EmlLoader, DocxLoader, and DocLoader to non-blocking I/O (Commit: 6574e1e)

**EmlLoader changes:**
- Added `import asyncio` to file imports
- Created `_read_file_bytes(file_path)` sync helper for initial email file reading
- Created `_write_temp_file(suffix, data)` sync helper for attachment temp file creation
- Replaced `with open(file_path, "rb")` with `await asyncio.to_thread(self._read_file_bytes, file_path)` (line 33)
- Replaced `tempfile.NamedTemporaryFile` write with `await asyncio.to_thread(self._write_temp_file, file_ext, attachment["raw"])` (line 146)
- Wrapped `os.path.exists` and `os.unlink` in finally block with asyncio.to_thread (lines 251-252)
- Replaced manual markdown file write with `await self.save_content(content_body, str(file_path))` (line 335)

**DocxLoader changes:**
- Added `import asyncio` to file imports
- Wrapped MarkItDown converter: `convert_result = await asyncio.to_thread(self.converter.convert, file_path)` (line 32)
- Wrapped zipfile operations: `images = await asyncio.to_thread(self.get_images_from_zip, file_path)` (line 36)
- Updated save_content call: `await self.save_content(result, str(file_path))` (line 59)

**DocLoader changes:**
- Added `import asyncio` to file imports
- Created `_convert_doc_to_docx(file_path)` sync helper that wraps all Spire.Doc operations (LoadFromFile, SaveToFile)
- Wrapped conversion: `document, temp_path = await asyncio.to_thread(self._convert_doc_to_docx, file_path)`
- Wrapped cleanup: `await asyncio.to_thread(os.remove, temp_path)` in finally block
- Proper try/finally ensures temp file cleanup even on errors

### Task 2: Convert ImageLoader, PyMuPDF4LLMLoader, and PPTXLoader to non-blocking I/O (Commit: a927cbf)

**ImageLoader changes:**
- Added `import asyncio` to file imports
- Created `_load_image(path)` sync helper for PIL.Image.open and cairosvg.svg2png operations
- Replaced direct image loading with `img = await asyncio.to_thread(self._load_image, path)` (line 28)
- Updated save_content call: `await self.save_content(description, str(path))` (line 57)

**PyMuPDF4LLMLoader changes:**
- Added `import asyncio` to file imports
- Wrapped pymupdf4llm.to_markdown: `pages = await asyncio.to_thread(pymupdf4llm.to_markdown, file_path, write_images=False, page_chunks=True)` (lines 37-39)
- Updated save_content call in PyMuPDF4LLMLoader: `await self.save_content(s, str(file_path))` (line 46)
- Updated save_content call in PyMuPDFLoader: `await self.save_content(s, str(file_path))` (line 27)

**PPTXLoader changes:**
- Added `import asyncio` to file imports
- Wrapped PPTXConverter.convert: `md_content, imgs = await asyncio.to_thread(self.converter.convert, local_path=file_path)` (line 153)
- Updated save_content call: `await self.save_content(md_content, str(file_path))` (line 172)

## Deviations from Plan

None - plan executed exactly as written. All blocking file operations identified in the plan were successfully wrapped with asyncio.to_thread, and all save_content calls were updated to use await.

## Verification

All verification criteria met:

✅ **Tests:** All 93 tests pass (both tasks)
```
============================== 93 passed in 4.81s ==============================
============================== 93 passed in 4.76s ==============================
```

✅ **Linting:** All modified files pass ruff checks
```
All checks passed!
```

✅ **asyncio.to_thread usage verified:** All 6 target loaders now use asyncio.to_thread
- EmlLoader: 4 occurrences (file read, temp file write, os.path.exists, os.unlink)
- DocxLoader: 2 occurrences (converter.convert, get_images_from_zip)
- DocLoader: 2 occurrences (conversion helper, os.remove)
- ImageLoader: 1 occurrence (_load_image helper)
- PyMuPDF4LLMLoader: 1 occurrence (pymupdf4llm.to_markdown)
- PPTXLoader: 1 occurrence (converter.convert)

✅ **save_content calls verified:** All calls use await
```
eml_loader.py:335:  await self.save_content(content_body, str(file_path))
docx.py:59:         await self.save_content(result, str(file_path))
image.py:57:        await self.save_content(description, str(path))
pymupdf.py:27:      await self.save_content(s, str(file_path))
pymupdf.py:46:      await self.save_content(s, str(file_path))
pptx_loader.py:172: await self.save_content(md_content, str(file_path))
```

✅ **No blocking file operations remain in async methods:** All blocking I/O delegated to thread pool via sync helper methods or direct asyncio.to_thread calls

## Technical Details

### Design Patterns

**Sync helper method pattern:**
- Used for complex blocking operations (multi-step file I/O, library calls)
- Examples: `_read_file_bytes`, `_write_temp_file`, `_convert_doc_to_docx`, `_load_image`
- Benefits: Clean separation, testable, reusable

**Direct asyncio.to_thread pattern:**
- Used for simple single-operation calls
- Examples: `os.remove`, `os.path.exists`, `converter.convert`
- Benefits: Less code, clear intent

### Dependencies Satisfied

**From must_haves:**
- ✅ All 6 loaders with blocking file I/O now use asyncio.to_thread for file operations
- ✅ No blocking file operations (open, read, zipfile, os.remove, PIL.Image.open) occur directly in async methods
- ✅ All loaders correctly await the async save_content from Plan 01
- ✅ All 93 existing tests continue passing

**Key links verified:**
- ✅ `asyncio\.to_thread.*converter\.convert` pattern found in docx.py and pptx_loader.py
- ✅ `asyncio\.to_thread.*_convert_doc_to_docx` pattern found in doc.py
- ✅ All 6 artifacts contain `asyncio.to_thread` usage

### Impact

**Immediate:**
- Email file processing (EmlLoader) no longer blocks event loop on file reads or attachment handling
- Office document processing (DocxLoader, DocLoader, PPTXLoader) offloads heavy library operations to thread pool
- Image processing (ImageLoader) offloads PIL and cairosvg operations to thread pool
- PDF processing (PyMuPDF4LLMLoader) offloads pymupdf4llm markdown conversion to thread pool

**Performance:**
- Event loop remains responsive during file I/O and library operations
- Multiple loaders can process files concurrently without blocking each other
- Follows established pattern from VideoAudioLoader (media_loader.py)

**Next steps enabled:**
- Phase 4 async infrastructure work complete
- All loaders now use async patterns consistently
- Foundation ready for Phase 5 and beyond

## Self-Check: PASSED

**Files verified to exist:**
```bash
$ ls -la openrag/components/indexer/loaders/eml_loader.py
-rw-r--r-- 1 paul paul 14289 Feb 11 09:29 openrag/components/indexer/loaders/eml_loader.py

$ ls -la openrag/components/indexer/loaders/docx.py
-rw-r--r-- 1 paul paul 3077 Feb 11 09:29 openrag/components/indexer/loaders/docx.py

$ ls -la openrag/components/indexer/loaders/doc.py
-rw-r--r-- 1 paul paul 1094 Feb 11 09:29 openrag/components/indexer/loaders/doc.py

$ ls -la openrag/components/indexer/loaders/image.py
-rw-r--r-- 1 paul paul 2055 Feb 11 09:30 openrag/components/indexer/loaders/image.py

$ ls -la openrag/components/indexer/loaders/pdf_loaders/pymupdf.py
-rw-r--r-- 1 paul paul 1381 Feb 11 09:30 openrag/components/indexer/loaders/pdf_loaders/pymupdf.py

$ ls -la openrag/components/indexer/loaders/pptx_loader.py
-rw-r--r-- 1 paul paul 5898 Feb 11 09:30 openrag/components/indexer/loaders/pptx_loader.py
```

**Commits verified:**
```bash
$ git log --oneline | grep -E "6574e1e|a927cbf"
a927cbf feat(04-02): convert ImageLoader, PyMuPDF4LLMLoader, and PPTXLoader to non-blocking I/O
6574e1e feat(04-02): convert EmlLoader, DocxLoader, and DocLoader to non-blocking I/O
```

**Modified file counts:**
- Task 1: 3 files (eml_loader.py, docx.py, doc.py)
- Task 2: 3 files (image.py, pymupdf.py, pptx_loader.py)
- Total: 6 files modified across 2 commits

**Test execution:**
- All 93 tests passing after each task
- No new test failures introduced
- Linting passing on all modified files
