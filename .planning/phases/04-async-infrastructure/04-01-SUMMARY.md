---
phase: 04-async-infrastructure
plan: 01
subsystem: loaders
tags: [async, performance, ray]
dependency-graph:
  requires: [phase-03]
  provides: [async-save-content, async-restore-script]
  affects: [loaders, scripts]
tech-stack:
  added: [asyncio.to_thread]
  patterns: [async-file-io, non-blocking-ray-calls]
key-files:
  created: []
  modified:
    - path: openrag/components/indexer/loaders/base.py
      changes: Converted save_content to async with asyncio.to_thread
    - path: openrag/components/indexer/loaders/txt_loader.py
      changes: Updated TextLoader and MarkdownLoader to await save_content
    - path: openrag/components/indexer/loaders/media_loader.py
      changes: Updated VideoAudioLoader to await save_content
    - path: openrag/components/indexer/loaders/pdf_loaders/marker.py
      changes: Updated MarkerLoader to await save_content
    - path: openrag/components/indexer/loaders/pdf_loaders/openai.py
      changes: Updated OpenAILoader to await save_content
    - path: openrag/components/indexer/loaders/pdf_loaders/docling.py
      changes: Updated DoclingLoader to await save_content
    - path: openrag/scripts/restore.py
      changes: Converted main() to async and replaced ray.get with await
decisions:
  - Use asyncio.to_thread for file I/O delegation (matches existing VideoAudioLoader pattern)
  - Create _write_file sync helper to encapsulate blocking file operations
  - Remove unused ray import from restore.py (Ray initialized via MilvusDB import)
metrics:
  duration: 3 min
  tasks-completed: 2
  files-modified: 7
  commits: 2
  tests-verified: 93
completed: 2026-02-11
---

# Phase 04 Plan 01: Async Save Content Foundation Summary

**One-liner:** Converted BaseLoader.save_content to async using asyncio.to_thread and eliminated blocking ray.get() in restore script

## Objective

Establish async foundation for file I/O in loaders by converting BaseLoader.save_content() to async and updating simple callers, plus eliminate blocking ray.get() in restore script (PERF-01 from research).

## Work Completed

### Task 1: Convert BaseLoader.save_content to async (Commit: 96b1afa)

**Changes:**
- Created `_write_file(path, content)` sync helper in BaseLoader for actual file write operation
- Converted `save_content()` to async method using `await asyncio.to_thread(self._write_file, ...)`
- Updated 5 simple loader callers to use `await self.save_content()`:
  - `txt_loader.py`: TextLoader (line 43) and MarkdownLoader (line 79)
  - `media_loader.py`: VideoAudioLoader (line 231)
  - `marker.py`: MarkerLoader (line 263)
  - `openai.py`: OpenAILoader (line 61)
  - `docling.py`: DoclingLoader (line 83)

**Additional fixes:**
- Fixed `base64.binascii.Error` → `binascii.Error` (proper import usage)
- Removed unnecessary f-string prefix in marker.py
- Fixed import ordering in media_loader.py (ruff auto-fix)

**Pattern established:**
```python
def _write_file(self, path: str, content: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

async def save_content(self, text_content: str, path: str):
    path = re.sub(r"\..*", ".md", path)
    await asyncio.to_thread(self._write_file, path, text_content)
    logger.debug(f"Document saved to {path}")
```

### Task 2: Convert restore script to async Ray calls (Commit: 8707576)

**Changes:**
- Added `import asyncio` to restore.py
- Converted `def main()` → `async def main()`
- Replaced blocking `ray.get(vdb_tmp.__ray_ready__.remote())` with `await vdb_tmp.__ray_ready__.remote()`
- Updated entry point: `sys.exit(main())` → `sys.exit(asyncio.run(main()))`
- Removed unused `import ray` (Ray initialized via MilvusDB import)

**Impact:**
- Eliminated primary blocking ray.get() call identified in PERF-01
- Script now uses async Ray ObjectRef resolution (non-blocking)
- All other functions remain sync (they don't use Ray actors or async I/O)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed binascii import usage**
- **Found during:** Task 1 linting
- **Issue:** Code used `base64.binascii.Error` but should use `binascii.Error` directly
- **Fix:** Changed exception handler to catch `binascii.Error` instead of `base64.binascii.Error`
- **Files modified:** openrag/components/indexer/loaders/base.py
- **Commit:** 96b1afa (included with Task 1)

**2. [Rule 3 - Blocking] Fixed pre-existing linting issues in modified files**
- **Found during:** Task 1 linting
- **Issue:** Modified files had linting errors (unused f-string, import order) that would fail CI
- **Fix:** Removed f-string prefix in marker.py, auto-fixed import order in media_loader.py
- **Files modified:** marker.py, media_loader.py
- **Commit:** 96b1afa (included with Task 1)

**3. [Rule 3 - Blocking] Removed unused ray import**
- **Found during:** Task 2 linting
- **Issue:** `import ray` flagged as unused after converting to async
- **Fix:** Removed ray import (Ray runtime initialized via MilvusDB import)
- **Files modified:** openrag/scripts/restore.py
- **Commit:** 8707576 (included with Task 2)

## Verification

All verification criteria met:

✅ **Tests:** All 93 tests pass
```
============================== 93 passed in 4.92s ==============================
```

✅ **Linting:** All modified files pass ruff checks
```
All checks passed!
```

✅ **Pattern verification:**
- `async def save_content` exists in base.py (line 59)
- `asyncio.to_thread` usage confirmed (line 61)
- No `ray.get` calls remain in restore.py
- `async def main` confirmed in restore.py (line 193)
- `asyncio.run(main())` entry point confirmed (line 333)

✅ **Grep verification:**
```bash
# All save_content calls now use await:
$ grep "self.save_content" txt_loader.py media_loader.py marker.py openai.py docling.py
txt_loader.py:43:            await self.save_content(content, str(path))
txt_loader.py:79:            await self.save_content(text_content=content, path=str(path))
media_loader.py:231:            await self.save_content(content, str(file_path))
marker.py:263:                await self.save_content(markdown, file_path_str)
openai.py:61:                await self.save_content(markdown, file_path)
docling.py:83:            await self.save_content(enriched_content, str(file_path))
```

## Technical Details

### Design Decisions

1. **asyncio.to_thread pattern:** Chose to match existing VideoAudioLoader pattern for consistency
2. **_write_file helper:** Extracted sync file write to separate method for clean separation of concerns
3. **Simple callers only:** Intentionally excluded docx.py, image.py, pptx_loader.py, pymupdf.py (handled in Plan 02)
4. **Restore script scope:** Only converted async flow, kept helper functions sync (no Ray/async I/O)

### Dependencies Satisfied

**From must_haves:**
- ✅ BaseLoader.save_content() is async and uses asyncio.to_thread
- ✅ All 5 simple loaders correctly await save_content
- ✅ Restore script uses await on Ray ObjectRefs instead of blocking ray.get()
- ✅ All 93 existing tests continue passing

**Key links verified:**
- ✅ `asyncio\.to_thread.*_write_file` pattern found in base.py
- ✅ `await vdb_tmp\.__ray_ready__` pattern found in restore.py

### Impact

**Immediate:**
- Async foundation established for all loaders
- Restore script no longer blocks on Ray actor initialization
- File I/O properly delegated to thread pool

**Next steps enabled:**
- Plan 02 can now convert complex loaders (docx, image, pptx, pymupdf) with confidence
- All loaders will inherit async save_content behavior
- Pattern established for other blocking I/O operations

## Self-Check: PASSED

**Files verified to exist:**
```bash
$ ls -la openrag/components/indexer/loaders/base.py
-rw-r--r-- 1 paul paul 9842 Feb 11 09:23 openrag/components/indexer/loaders/base.py

$ ls -la openrag/scripts/restore.py
-rw-r--r-- 1 paul paul 11934 Feb 11 09:25 openrag/scripts/restore.py
```

**Commits verified:**
```bash
$ git log --oneline | grep -E "96b1afa|8707576"
8707576 feat(04-async-infrastructure): convert restore script to async Ray calls
96b1afa feat(04-async-infrastructure): convert BaseLoader.save_content to async
```

**Modified file counts:**
- Task 1: 6 files (base.py, txt_loader.py, media_loader.py, marker.py, openai.py, docling.py)
- Task 2: 1 file (restore.py)
- Total: 7 files modified across 2 commits

**Test execution:**
- All 93 tests passing
- No new test failures introduced
- Linting passing on all modified files
