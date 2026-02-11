# Roadmap: OpenRAG Codebase Hardening

## Overview

This roadmap systematically addresses reliability and security issues across the OpenRAG codebase. Starting with isolated quick fixes, we progressively harden exception handling across API, core services, and pipeline layers. Then we address async infrastructure issues before hardening scripts and cleaning up configuration tech debt. Every fix maintains existing external API behavior and passes all existing tests.

## Completed Milestones

<details>
<summary><strong>v1.0 — Codebase Hardening</strong> (6 phases, 15 plans — completed 2026-02-11)</summary>

### Phase 1: Quick Security Fixes (3/3 plans)
Fixed nested httpx.Timeout bug, replaced unsafe PostgreSQL URL interpolation with SQLAlchemy URL.create(), added Pydantic schema validation for file upload metadata.

### Phase 2: Exception Handling - API Layer (3/3 plans)
Replaced 18 broad exception handlers across OpenAI, indexer, tools, utils, actors, and extract routers with tiered exception handling (HTTPException → specific types → generic fallback).

### Phase 3: Exception Handling - Core Services (4/4 plans)
Replaced 51 broad exception handlers across vectordb, indexer, loaders, pipeline, and LLM components with typed exceptions (VDBError, EmbeddingError, httpx errors, asyncio.CancelledError).

### Phase 4: Async Infrastructure (2/2 plans)
Converted BaseLoader.save_content and 6 loaders to async with asyncio.to_thread. Converted restore script from blocking ray.get() to async Ray actor calls.

### Phase 5: Script & Health Hardening (2/2 plans)
Enhanced /health_check with concurrent LLM/VLM probes, response time metrics, and HTTP 503/degraded status. Hardened restore script with state tracking, rollback, and progress logging.

### Phase 6: Configuration Cleanup (1/1 plan)
Fixed Hydra version_base to None for forward compatibility. Added DeprecationWarning for legacy "ragondin-" partition prefix.

**Stats:** 74 source files modified, +3,658 / -1,917 lines, 98 tests passing

</details>

## Current Work

No active milestone. See `.planning/milestones/v1.0-REQUIREMENTS.md` for deferred v2 requirements.

## Progress

| Milestone | Phases | Plans | Status |
|-----------|--------|-------|--------|
| v1.0 Codebase Hardening | 6/6 | 15/15 | Complete |
