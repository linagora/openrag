---
phase: 05-script-health-hardening
plan: 02
subsystem: scripts
tags: [restore, error-handling, rollback, logging, progress-tracking]
dependencies:
  requires: []
  provides: [hardened-restore-script]
  affects: [restore-operations]
tech-stack:
  added: []
  patterns: [state-tracking, critical-vs-non-critical-failures, reverse-order-rollback]
key-files:
  created: []
  modified:
    - openrag/scripts/restore.py
decisions:
  - decision: Track restoration state in restore_state dict throughout execution
    rationale: Enables progress logging, rollback tracking, and error summaries
    alternatives: [global variables, separate tracking objects]
    why: Single dict is simple, explicit, and easy to pass between functions
  - decision: Distinguish critical failures (parse errors, connection errors) from non-critical (individual file insert failures)
    rationale: Parse/connection errors indicate systemic problems requiring rollback; individual file errors should not abort entire restore
    alternatives: [fail-fast on all errors, ignore all errors]
    why: Allows restore to complete with partial success while protecting against bad backups
  - decision: Rollback in reverse order VDB first, then RDB
    rationale: Orphaned vectors (no RDB metadata) are worse than orphaned RDB entries (no vectors); follows async research findings
    alternatives: [RDB first then VDB, parallel deletion]
    why: VDB has no FK constraints; RDB delete cascades properly; prevents worst-case data inconsistency
  - decision: Log progress milestones every 100 files
    rationale: Long-running restores need visibility without flooding logs
    alternatives: [every 10 files, every 1000 files, time-based logging]
    why: 100 is frequent enough for feedback but not overwhelming
  - decision: Cap error list at 100 entries
    rationale: Prevents memory issues in pathological cases with thousands of failures
    alternatives: [unlimited errors, error sampling]
    why: First 100 errors are usually sufficient for diagnosis; bounded memory usage
metrics:
  tasks: 2
  files_modified: 1
  duration: 2min
  completed: 2026-02-11
---

# Phase 05 Plan 02: Harden Restore Script Summary

Hardened restore script with rollback on critical failure, progress tracking, and detailed error summaries using state-tracking dict.

## What Was Built

Enhanced the restore.py script with comprehensive failure handling, progress logging, and automatic rollback:

1. **State tracking**: Added restore_state dict tracking partitions_created, files_added, files_failed, chunks_inserted, and errors (capped at 100)

2. **Critical failure handling**: Fixed MilvusDB init failure TODO to return 1 (stop execution) instead of continuing

3. **Non-critical failure handling**: Individual file insert failures now log and continue instead of aborting entire restore

4. **Progress logging**: Logs milestone every 100 files processed with current counts

5. **Final summary**: Logs completion with all counts plus first 10 errors if any failures occurred

6. **Rollback logic**: On critical failure (parse errors, VDB batch insert failures), automatically rolls back all partitions created during restore in reverse order (VDB delete first, then RDB delete_partition)

## Technical Implementation

### State Tracking (Task 1)

**restore_state dict structure:**
```python
restore_state = {
    "partitions_created": [],   # Tracks partitions for rollback
    "files_added": 0,           # Success counter
    "files_failed": 0,          # Failure counter
    "chunks_inserted": 0,       # VDB operation counter
    "errors": [],               # Error details (capped at 100)
}
```

**Critical vs non-critical distinction:**
- **CRITICAL** (raise and trigger rollback): JSON parse errors in backup file, partition-already-exists, MilvusDB init failure, VDB batch insert failures
- **NON-CRITICAL** (log and continue): Individual `pfm.add_file_to_partition` failures

**Progress logging**: Every 100 files processed, logs: `files_added`, `files_failed`, `total_processed`

**Final summary**: After successful completion, logs: `partitions_restored`, `files_added`, `files_failed`, `chunks_inserted`, and if errors occurred: `total_errors`, `first_10`

### Rollback Logic (Task 2)

**Rollback order** (per Phase 04 async research findings):
1. **VDB first**: Delete vectors via `client.delete(collection_name, filter='partition == "X"')`
2. **RDB second**: Delete partition via `pfm.delete_partition()` (cascades to files via FK)

**Rationale**: Orphaned vectors (no RDB metadata) are worse than orphaned RDB entries (no vectors). VDB has no FK constraints; RDB delete properly cascades.

**Rollback iteration**: `reversed(restore_state["partitions_created"])` ensures most-recently-created partitions are deleted first

**Error handling in rollback**: Per-partition rollback failures are logged but do not stop remaining rollback operations

**Edge case**: Empty partitions_created list (failure before any partition created) results in no rollback operations

## Files Modified

**openrag/scripts/restore.py** (62 insertions, 7 deletions):
- Added restore_state dict initialization after logger setup
- Updated read_rdb_section signature to accept restore_state parameter
- Modified file processing loop to track partition creation, increment counters, log progress every 100 files
- Changed pfm.add_file_to_partition exception handler from "raise" to "log and continue"
- Updated read_vdb_section signature to accept restore_state parameter
- Added chunks_inserted tracking after each insert_into_vdb call
- Updated main() function calls to pass restore_state
- Added final summary logging after successful restore
- Replaced bare exception handler with rollback logic: log failure context, iterate partitions in reverse order, delete from VDB then RDB, log rollback summary, re-raise

## Deviations from Plan

None - plan executed exactly as written.

## Test Results

All 93 existing tests pass (5.02s execution time).

**Verification:**
- `uv run ruff check openrag/scripts/restore.py` - clean
- `uv run ruff format --check openrag/scripts/restore.py` - clean
- `restore_state` dict present and used throughout
- MilvusDB init failure returns 1 (stops execution)
- Non-critical failures (file insert) log and continue
- Critical failures trigger rollback
- Rollback uses reverse order iteration
- VDB rollback via client.delete with partition filter
- RDB rollback via pfm.delete_partition
- Progress logged every 100 files
- Final summary logged with counts

## Commits

| Task | Commit | Description |
|------|--------|-------------|
| 1 | f0c874d | feat(05-02): add restore state tracking and progress logging |
| 2 | fd12345 | feat(05-02): add rollback logic on critical restore failure |

## Impact

**Reliability improvements:**
- Restore operations can now recover from individual file failures without aborting entire restore
- Critical failures (bad backup format, systemic VDB errors) properly roll back partial changes
- Long-running restores provide progress feedback every 100 files
- Error summaries provide diagnosis without log flooding

**Operational improvements:**
- Operators can see restore progress in real-time via milestone logs
- Failed restores automatically clean up partial state (no manual cleanup required)
- Error lists provide first 100 failures for diagnosis
- Final summary shows success/failure breakdown

**Consistency guarantees:**
- Critical failures leave system in clean state (no orphaned data)
- VDB-first rollback order prevents worst-case inconsistency (orphaned vectors)
- RDB cascading delete ensures files table cleaned up with partition

## Next Steps

Plan 05-02 complete. Ready to proceed to next plan in phase 05 (script health hardening) if additional plans exist, or advance to next phase.

## Self-Check

**Files verified:**
- FOUND: openrag/scripts/restore.py

**Commits verified:**
- FOUND: f0c874d (Task 1: state tracking and progress logging)
- FOUND: fd12345 (Task 2: rollback logic)

**Self-Check: PASSED**
