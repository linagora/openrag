---
phase: 01-quick-security-fixes
plan: 03
subsystem: API Security
tags: [security, validation, pydantic, metadata]
dependency_graph:
  requires: []
  provides:
    - FileMetadataSchema for structured metadata validation
  affects:
    - openrag/routers/utils.py:validate_metadata
    - openrag/models/indexer.py
tech_stack:
  added:
    - Pydantic field_validator for domains validation
  patterns:
    - Schema-based validation with descriptive error messages
    - Backward-compatible validation (extra: "allow")
key_files:
  created: []
  modified:
    - openrag/models/indexer.py: Added FileMetadataSchema class
    - openrag/routers/utils.py: Integrated schema validation in validate_metadata()
decisions:
  - decision: Use Pydantic schema validation with extra: "allow"
    rationale: Allows backward compatibility for unknown fields while validating known fields
    alternatives: ["Strict schema (extra: forbid)", "Manual dict validation"]
  - decision: Validate domains as list of non-empty strings
    rationale: Prevents type confusion and injection attacks via domain filtering
    alternatives: ["No validation", "Regex validation"]
metrics:
  duration_minutes: 1
  tasks_completed: 3
  files_modified: 2
  tests_passing: 93
  completed_at: "2026-02-10T16:40:47Z"
---

# Phase 01 Plan 03: Add Pydantic schema validation for file upload metadata

**One-liner:** Structured metadata validation using FileMetadataSchema with field validators to prevent malformed domains and type confusion attacks.

## Objective

Add Pydantic schema validation for file upload metadata to prevent malicious or malformed metadata from bypassing validation. Previously, validate_metadata() only validated JSON syntax, allowing arbitrary fields with arbitrary types to pass through unchecked.

## Tasks Completed

### Task 1: Create FileMetadataSchema in models/indexer.py
**Status:** ✅ Complete
**Commit:** ddd960c
**Files:** openrag/models/indexer.py

Created Pydantic schema with:
- `mimetype: str | None` - Optional MIME type override
- `domains: list[str]` - List of domain strings for filtering
- `model_config = {"extra": "allow"}` - Backward compatibility
- `@field_validator('domains')` - Validates non-empty strings only

### Task 2: Integrate FileMetadataSchema validation in routers/utils.py
**Status:** ✅ Complete
**Commit:** 3875f0c
**Files:** openrag/routers/utils.py

Updated validate_metadata() function to:
- Import FileMetadataSchema and ValidationError from pydantic
- Validate parsed JSON against schema: `FileMetadataSchema(**parsed)`
- Return dict via `validated.model_dump()` for backward compatibility
- Catch ValidationError and format user-friendly error messages
- Maintain separate JSON syntax error handling

### Task 3: Run test suite to verify metadata validation works
**Status:** ✅ Complete
**Result:** All 93 tests passed

Verified:
- Empty metadata `{}` passes validation (optional fields, default factory)
- File upload endpoints work with schema validation
- No breaking changes to existing functionality
- Backward compatibility maintained

## Deviations from Plan

None - plan executed exactly as written.

## Verification Results

**Schema Creation:**
```bash
$ grep -A 20 "class FileMetadataSchema" openrag/models/indexer.py
# Confirmed: FileMetadataSchema with mimetype, domains, validator, and model_config
```

**Schema Integration:**
```bash
$ grep -n "FileMetadataSchema" openrag/routers/utils.py
# Line 9: from models.indexer import FileMetadataSchema
# Line 206: validated = FileMetadataSchema(**parsed)
```

**Linting:**
```bash
$ uv run ruff check openrag/models/indexer.py openrag/routers/utils.py
# All checks passed!
```

**Test Suite:**
```bash
$ uv run pytest
# 93 passed in 4.02s
```

## Security Impact

**Before:**
- Metadata validation only checked JSON syntax
- Arbitrary fields with arbitrary types could pass through
- `domains` field could be string, int, dict, etc.
- Potential for type confusion attacks downstream

**After:**
- Structured schema validation with Pydantic
- `domains` must be list of non-empty strings
- `mimetype` must be string or None
- Malformed metadata rejected with HTTP 400 and descriptive errors
- Unknown fields still allowed (backward compatibility)

**Example rejections:**
- `{"domains": "not-a-list"}` → HTTP 400: "domains must be a list"
- `{"domains": ["valid", 123]}` → HTTP 400: "All domains must be strings"
- `{"domains": [""]}` → HTTP 400: "Domain names cannot be empty"

## Self-Check

### Verify Created Files
No new files created (schema added to existing models/indexer.py).

### Verify Modified Files Exist
```bash
$ [ -f "openrag/models/indexer.py" ] && echo "FOUND: openrag/models/indexer.py" || echo "MISSING: openrag/models/indexer.py"
FOUND: openrag/models/indexer.py

$ [ -f "openrag/routers/utils.py" ] && echo "FOUND: openrag/routers/utils.py" || echo "MISSING: openrag/routers/utils.py"
FOUND: openrag/routers/utils.py
```

### Verify Commits Exist
```bash
$ git log --oneline --all | grep -q "ddd960c" && echo "FOUND: ddd960c" || echo "MISSING: ddd960c"
FOUND: ddd960c

$ git log --oneline --all | grep -q "3875f0c" && echo "FOUND: 3875f0c" || echo "MISSING: 3875f0c"
FOUND: 3875f0c
```

## Self-Check: PASSED

All claimed files, commits, and test results verified successfully.

## Next Steps

This completes plan 01-03. The metadata validation hardening is complete. Next plans in phase 01 (Quick Security Fixes) should address:
- SQL injection prevention
- Broad exception handler replacement
- Input validation for other endpoints

## Summary

Successfully added Pydantic schema validation for file upload metadata. The FileMetadataSchema enforces type safety for `domains` (list of non-empty strings) and `mimetype` (optional string) while maintaining backward compatibility via `extra: "allow"`. All 93 tests pass, confirming no breaking changes to existing functionality.
