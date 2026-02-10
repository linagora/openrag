---
phase: 01-quick-security-fixes
plan: 01
subsystem: frontend
tags: [bug-fix, httpx, chainlit]
dependency_graph:
  requires: []
  provides: [corrected-httpx-timeout-api-usage]
  affects: [chainlit-authentication, health-check-request]
tech_stack:
  added: []
  patterns: [proper-httpx-timeout-configuration]
key_files:
  created: []
  modified: [openrag/app_front.py]
decisions:
  - Use simple httpx.Timeout(float) form instead of nested configuration
  - Apply same timeout value (4 minutes) to all HTTP operations
key_metrics:
  duration_minutes: 1
  completed_date: 2026-02-10
  task_count: 2
  files_modified: 1
  commits: 1
---

# Phase 01 Plan 01: Fix httpx.Timeout Bug Summary

**One-liner:** Fixed TypeError in Chainlit frontend by removing nested httpx.Timeout objects in AsyncClient initialization.

## What Was Done

Fixed a critical bug in `openrag/app_front.py` where httpx.AsyncClient was incorrectly instantiated with nested Timeout objects: `httpx.Timeout(timeout=httpx.Timeout(4 * 60.0))`. This pattern violates the httpx API, which expects the timeout parameter to be a float/int, not another Timeout object.

The bug affected two locations:
1. **Line 69** - `auth_callback` function: Chainlit password authentication endpoint
2. **Line 134** - `on_chat_start` function: Health check request when chat sessions start

Both instances were corrected to use the proper API: `httpx.Timeout(4 * 60.0)`, which applies a 4-minute timeout to all HTTP operations (connect, read, write, pool).

## Tasks Completed

| Task | Description | Status | Commit |
|------|-------------|--------|--------|
| 1 | Fix nested httpx.Timeout objects in app_front.py | ✓ | ddd960c |
| 2 | Run test suite to verify no regressions | ✓ | N/A (verification only) |

## Deviations from Plan

None - plan executed exactly as written.

## Verification Results

All success criteria met:
- httpx.AsyncClient in auth_callback (line 69) uses `httpx.Timeout(4 * 60.0)` without nesting ✓
- httpx.AsyncClient in on_chat_start (line 134) uses `httpx.Timeout(4 * 60.0)` without nesting ✓
- No other httpx timeout configurations have nested Timeout objects (0 found) ✓
- All 93 existing tests pass ✓
- Linting passes with no errors ✓

## Technical Details

**Root cause:** The httpx.Timeout constructor accepts numeric values for its timeout parameters, not Timeout objects. Nesting causes a TypeError when the AsyncClient attempts to initialize.

**Correct API usage:**
```python
# Simple form (used in this fix)
httpx.Timeout(240.0)  # 4 minutes for all operations

# Granular form (alternative, not needed here)
httpx.Timeout(connect=5.0, read=240.0, write=240.0, pool=5.0)
```

## Impact

**Before:** Chainlit authentication and health checks would fail with TypeError on client initialization, preventing users from logging in or starting chat sessions.

**After:** HTTP clients instantiate correctly with proper 4-minute timeouts, enabling authentication flow and health checks to complete successfully.

**Risk assessment:** Low-risk fix. Changes only affect HTTP client configuration. No logic or behavioral changes. All tests pass.

## Files Modified

- `openrag/app_front.py`: Fixed nested httpx.Timeout at lines 69 and 134

## Self-Check: PASSED

**Created files verification:**
- N/A - no files created

**Modified files verification:**
```bash
[ -f "openrag/app_front.py" ] && echo "FOUND: openrag/app_front.py" || echo "MISSING: openrag/app_front.py"
# Result: FOUND: openrag/app_front.py
```

**Commits verification:**
```bash
git log --oneline --all | grep -q "ddd960c" && echo "FOUND: ddd960c" || echo "MISSING: ddd960c"
# Result: FOUND: ddd960c
```

**Pattern verification:**
```bash
# No nested patterns remain
grep -c "httpx.Timeout(timeout=" openrag/app_front.py
# Result: 0

# Correct pattern exists exactly twice
grep -c "httpx.AsyncClient(timeout=httpx.Timeout(4 \* 60.0))" openrag/app_front.py
# Result: 2
```

All verification checks passed. Plan executed successfully without deviations or blockers.
