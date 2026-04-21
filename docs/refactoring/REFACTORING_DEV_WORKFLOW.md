# OpenRAG Hexagonal Refactoring — Development Workflow

> **Strategy:** Hybrid — merge from dev during safe phases,
> forward-port only critical fixes during transformation phases,
> feature freeze during cutover.

## Branch Layout

```
main (production releases)
  |
  +-- v1.1.9 (tag, frozen)
  |     |
  |     +-- refactor/hexagonal  (long-lived refactoring branch)
  |           |
  |           +-- refactor/phase-5-retrieval    (short-lived, 1-3 days)
  |           +-- refactor/phase-7-persistence  (short-lived, 1-3 days)
  |           +-- ...
  |
  +-- dev (continues only on urgent bug fixes )
        |
        +-- fix/abc 
```

**Rules:**

- `refactor/hexagonal` is branched from `v1.1.9` tag (frozen, tested, deployed)
- Per-task branches are branched from `refactor/hexagonal`, merged back via PR
- `dev` continues independently — no one works on `dev` AND `refactor/hexagonal` simultaneously on the same file
- `refactor/hexagonal` never merges into `dev`. The relationship is one-directional until the final cutover.

---

## Three Modes of Operation

The refactoring goes through three modes. Each mode has different rules for
how `dev` and `refactor/hexagonal` interact.

```
Timeline:

Phase 0-4         Phase 5-9            Phase 10-12       Phase 13-15
(Foundation)      (Transformation)     (Cutover)         (Post-cutover)
|                 |                    |                  |
v                 v                    v                  v
+--------+        +-----------+        +--------+         +--------+
| MODE 1 |        |  MODE 2   |        | MODE 3 |         | NORMAL |
| MERGE  |        | ISOLATE   |        | FREEZE |         |  DEV   |
+--------+        +-----------+        +--------+         +--------+

dev merges into   Forward-port         Feature freeze     refactor/hexagonal
refactor weekly.  critical fixes       on dev. Final      becomes the new
Zero conflicts    only. No merges.     merge + cutover.   dev branch.
(additive only).  Features wait.                          Phases 13-15 as
                                                          normal features.
```

---

## MODE 1 — MERGE (Phases 0-4, ~2 days)

### What's happening

Phases 0-4 are purely additive — creating new files in `core/`, `services/`,
`api/`, `di/`. No existing file is modified or deleted. Zero conflict risk.

### Rules

| Rule              | Detail                              |
| ----------------- | ----------------------------------- |
| Merge frequency   | Once at the end of Mode 1 (day 2)   |
| Conflict expected | None (refactor only adds new files) |
| Bug fixes on dev  | Continue normally                   |

### Workflow

```bash
# Start of refactoring
git checkout v1.1.9
git switch -c refactor/hexagonal
git push -u origin refactor/hexagonal

# Person A works on Phase 0-2
git switch refactor/hexagonal
git switch -c refactor/phase-0-scaffold
# ... create directories, __init__.py files, import guard ...
# PR into refactor/hexagonal

# Weekly sync (or after each phase)
git switch refactor/hexagonal
git pull
git merge origin/dev
# No conflicts — our new files don't overlap with dev changes
git push
```

### What gets merged from dev

Everything. Bug fixes, new features, dependency updates — all merge cleanly
because the refactoring hasn't touched any existing files yet.

### Exit criteria for Mode 1

All of these exist and pass:

- `core/utils/registry.py` — Registry[T] generic
- `core/utils/exceptions.py` — Exception hierarchy
- `core/models/*.py` — All domain models
- `core/config/*.py` — All config schemas + loader
- `core/embeddings/`, `core/rerankers/`, `core/llm/`, `core/vlm/` — ABCs + registries
- `core/vector_stores/`, `core/catalog/` — ABCs
- `core/ports/*.py` — All repository port ABCs
- `core/chunking/`, `core/indexing/parsers/` — ABCs + registries
- `scripts/check_layer_imports.py` passes
- `python -c "from openrag.core.models import Chunk, Document, User"` works
- All existing tests still pass

---

## MODE 2 — ISOLATE (Phases 5-9, ~2 weeks)

### What's happening

This is the core transformation. Existing files are being rewritten, gutted,
shimmed, and replaced. File paths change. Imports change. The god object gets
decomposed. This is where merging from `dev` would create painful conflicts.

### Rules

| Rule                | Detail                                                            |
| ------------------- | ----------------------------------------------------------------- |
| Merge frequency     | **Never.** No merges from `dev`.                                  |
| Forward-port        | Critical bug fixes only (security, data loss, production outages) |
| Features on dev     | **No features on dev**                                            |
| Dev changes tracked | Maintain a `FORWARD_PORT_LOG.md` tracking what landed on dev      |

### Forward-porting process

When a critical fix lands on `dev`:

```bash
# 1. DON'T merge. Read the diff on dev.
git log origin/dev --oneline -20   # see what landed

# 2. Understand the fix (read the PR, understand the intent)

# 3. Re-implement the fix in the new architecture on refactor/hexagonal
git switch refactor/hexagonal
git switch -c fix/forward-port-xyz
# ... write the fix against the new code structure ...
# PR into refactor/hexagonal

# 4. Log it
echo "- 2026-04-20: Forward-ported fix XYZ (dev commit abc123) -> refactor commit def456" >> FORWARD_PORT_LOG.md
```

### FORWARD_PORT_LOG.md

Keep a running log so nothing is forgotten:

```markdown
# Forward Port Log

Tracks dev changes during Mode 2 isolation (Phases 5-9).
Each entry: what changed on dev, whether it was forward-ported or deferred.

## Forward-ported (critical)

- 2026-05-05: Security fix — regenerate_token missing auth check
  dev: commit abc123, PR #42
  refactor: commit def456

- 2026-05-12: Bug fix — Milvus search crash on empty partition
  dev: commit ghi789, PR #45
  refactor: commit jkl012

## Deferred to cutover (features)

- 2026-05-08: New endpoint GET /partition/{name}/stats (PR #43)
  -> Will re-implement in api/routers/admin/partitions.py

- 2026-05-15: Added Docling v3 PDF loader (PR #47)
  -> Will re-implement in services/inference/ or core/indexing/parsers/

- 2026-05-20: Updated pymilvus to 2.5.0 (PR #50)
  -> Will update dependencies during Mode 3
```

### Team coordination during Mode 2

**All team:** Works exclusively on `refactor/hexagonal`. Does not touch `dev`.

**Urgent bug fixes:** should land on  `dev` but keep in mind:

- the bug fixes will need to be re-implemented on the new architecture
- only small, well-documented PRs are allowed so forward-porting is easy
- avoid large structural changes to existing files (creates harder forward-ports)

**Communication:** Daily standup to review:

- What landed on `dev` today
- What needs forward-porting (critical fixes)
- What's deferred (logged for Mode 3)

### Parallel work within refactor branch

During Mode 2, multiple people can work on different phases in parallel:

```
refactor/hexagonal
  |
  +-- Person A: refactor/phase-5-retrieval-core
  |     (Phases 5A-5C: retrieval, chunking, prompt builders)
  |
  +-- Person B: refactor/phase-7-persistence
  |     (Phase 7: god object decomposition)
  |
  +-- Both merge back into refactor/hexagonal via PR
  |
  +-- Then Person A + B converge on Phase 8 (orchestrators)
```

**Dependency order:**

```
Phase 5 (core logic)  -----+
Phase 6 (inference)   -----+--> Phase 8 (orchestrators) --> Phase 9 (workers)
Phase 7 (persistence) -----+
```

Phases 5, 6, 7 can run in parallel. Phase 8 needs all three. Phase 9 needs Phase 8.

### Exit criteria for Mode 2

- All core domain logic lives in `core/` (no business logic in `components/`)
- All adapters live in `services/` (inference, storage, persistence)
- All orchestrators live in `services/orchestrators/`
- Ray actors are thin wrappers in `services/workers/`
- Old `components/` files are either deleted or gutted to re-export shims
- Import guard passes
- Integration tests pass (full upload -> search -> chat cycle)
- `FORWARD_PORT_LOG.md` is complete — all dev changes accounted for

---

## MODE 3 — FREEZE (Phases 10-12, ~3-4 days)

### What's happening

The API layer is being restructured (routers, middleware, schemas), the DI
container is being wired, and old shims are deleted. This is the final
cutover — after this, the old code is gone.

### Rules

| Rule                           | Detail                                                            |
| ------------------------------ | ----------------------------------------------------------------- |
| Feature freeze on dev          | **Mandatory.** No new features on dev. Critical bug fixes only.   |
| Merge direction                | Selected dev changes cherry-picked into refactor (not full merge) |
| Re-implement deferred features | Work through FORWARD_PORT_LOG.md deferred list                    |
| Duration                       | 3-4 days maximum — freeze must be time-boxed                      |

### The freeze announcement

```
Subject: Feature freeze on dev starting [date] — Hexagonal cutover

Duration: ~3-4 days
What's frozen: New features on dev branch
What's allowed: Critical bug fixes only (security, production outages)
Why: We're cutting over to the new architecture. Parallel changes would
     create unmergeable conflicts.
What to do: 
  - Finish in-progress work before [date] or park it
  - Critical fixes: PR into dev as usual, we'll cherry-pick into refactor
```

### Phase 10-12 workflow

```bash
# Phase 10: API layer restructure
git switch refactor/hexagonal
git switch -c refactor/phase-10-api-layer
# Create api/main.py, api/routers/*, api/middleware/*, api/schemas/*
# Mount new routers alongside old ones (parallel operation)
# Test each new router
# Remove old routers one at a time
# PR into refactor/hexagonal

# Phase 11: Composition root
git switch -c refactor/phase-11-di-container
# Create di/container.py, di/providers.py, di/factories.py
# Wire ServiceContainer into api/main.py lifespan
# Update routers to use Depends() from providers
# Remove global singletons
# PR into refactor/hexagonal

# Phase 12: Cleanup
git switch -c refactor/phase-12-cleanup
# Delete all old shims, components/, routers/, models/
# Update Dockerfile, docker-compose, pyproject.toml
# Final verification
# PR into refactor/hexagonal
```

### Re-implementing deferred features

Work through `FORWARD_PORT_LOG.md` deferred list. Each deferred feature
is implemented directly in the new architecture:

```bash
# Example: re-implement "GET /partition/{name}/stats" from dev PR #43
git switch refactor/hexagonal
git switch -c feature/partition-stats
# Read the original PR on dev for intent
# Implement in api/routers/admin/partitions.py (new structure)
# Wire through services/orchestrators/partition_service.py
# PR into refactor/hexagonal
```

### Dependency sync

Update all dependencies to match current dev (or newer):

```bash
git switch refactor/hexagonal
# Compare pyproject.toml between dev and refactor
diff <(git show origin/dev:pyproject.toml) pyproject.toml
# Update versions, add new deps, remove unused
uv sync
uv run pytest -m unit
```

### Exit criteria for Mode 3

- `openrag/components/` directory does not exist
- `openrag/routers/` directory does not exist (replaced by `openrag/api/routers/`)
- `openrag/models/` directory does not exist (replaced by `openrag/core/models/` + `openrag/api/schemas/`)
- `di/container.py` ServiceContainer is the composition root
- No module-level `config = load_config()` anywhere
- No module-level Ray actor singletons
- Import guard passes with zero violations
- All deferred features from FORWARD_PORT_LOG.md re-implemented
- Full integration test suite passes
- Docker build succeeds
- `docker compose up` + manual smoke test passes

---

## CUTOVER — Replacing dev

Once Mode 3 is complete and everything passes:

### Step 1 — Final verification

```bash
# On refactor/hexagonal
uv run pytest                                    # all tests
python scripts/check_layer_imports.py            # layer guard
docker compose -f infra/compose/docker-compose.yaml build   # docker build
docker compose -f infra/compose/docker-compose.yaml up -d   # full stack
# Run integration tests against running stack
# Manual smoke test: upload document, search, chat, manage users
```

### Step 2 — Replace dev

```bash
# Rename branches
git branch -m dev dev-legacy
git branch -m refactor/hexagonal dev
git push origin dev --force-with-lease
git push origin dev-legacy
```

### Step 3 — Communicate

```
Subject: Hexagonal refactoring complete — dev branch replaced

The dev branch now contains the new 3-layer architecture.
dev-legacy preserves the old branch for reference.

Key changes for developers:
- Import paths changed: components.retriever -> openrag.core.retrieval
- No global singletons: use ServiceContainer + Depends()
- New project layout: see updated README.md and CLAUDE.md
- Tests: uv run pytest -m unit (fast), uv run pytest -m integration (needs stack)

Please pull fresh and read the updated CLAUDE.md before starting work.
```

### Step 4 — Delete the refactoring branch

```bash
git push origin --delete refactor/hexagonal  # remote
git branch -d refactor/hexagonal             # local
```

---

## POST-CUTOVER — Normal Development (Phases 13-15)

After cutover, development resumes on `dev` with the new architecture.

Phases 13-15 are developed as normal feature branches on the new `dev`.
They can run in parallel if different people own them — no dependencies
between them.

```bash
# Phase 13: Project layout restructure
git switch dev
git switch -c feature/phase-13-project-layout
# Move Dockerfiles -> infra/docker/, docker-compose -> infra/compose/
# Move openrag/scripts/ -> scripts/
# Unify tests/ (unit + integration + load)
# Move prompts/ -> openrag/prompts/
# Move extern/indexer-ui -> ui/
# Update pyproject.toml, CI, README
# PR into dev

# Phase 14: Per-partition presets
git switch dev
git switch -c feature/phase-14-presets
# Flesh out core/config/{indexation,retrieval,partition,presets}.py
# Alembic migration: presets table + partition config columns
# services/persistence/preset_repo.py
# services/orchestrators/preset_service.py
# Update IndexingService + RetrievalService for per-partition config
# api/routers/admin/presets.py (CRUD endpoints)
# Wire into ServiceContainer, seed defaults
# PR into dev

# Phase 15: OIDC / Keycloak SSO
git switch dev
git switch -c feature/phase-15-oidc-sso
# Add OIDCConfig to core/config/auth.py
# Create services/auth/{jwt_validator,oidc_mapper,oidc_provisioner}.py
# Add get_user_by_external_id() to UserRepository
# Update api/dependencies/auth.py with dual-auth dispatch
# Wire OIDC into ServiceContainer (conditional on OIDC_ENABLED)
# Update indexer-ui: oidc-client-ts, SSO login button, /auth/callback
# Update .env.example + docker-compose with OIDC env vars
# PR into dev
```

No special workflow needed — these are standard features on a clean codebase.

---

## Timeline Summary

```
Day 1-2      MODE 1: Phases 0-4 (Foundation)
             - 1-2 people, intensive
             - Merge dev once at end (zero conflicts)
             |
Day 2        Last merge from dev -> refactor/hexagonal
             Enter MODE 2
             Announce feature freeze for end of week 2
             |
Day 3-14     MODE 2: Phases 5-9 (Transformation)
             - 2-3 people in parallel (5+6 || 7, then converge on 8-9)
             - No merges from dev
             - Forward-port critical fixes only
             - Track all dev changes in FORWARD_PORT_LOG.md
             |
Day 12       Enter MODE 3: Feature freeze on dev
             |
Day 12-15    MODE 3: Phases 10-12 (Cutover)
             - API restructure + DI wiring + cleanup
             - Re-implement deferred features (if any)
             - Cherry-pick critical fixes from dev
             |
Day 15       CUTOVER: refactor/hexagonal replaces dev
             Lift feature freeze
             |
Week 4+      POST-CUTOVER: Phases 13-15 on new dev (parallel)
             - Phase 13: Project layout restructure (infra/, scripts/, tests/, ui/)
             - Phase 14: Per-partition presets (indexation + retrieval config)
             - Phase 15: OIDC / Keycloak SSO (dual auth, auto-provisioning)
             Normal development resumes
```

---

## CI/CD for refactor/hexagonal

### Branch protection

```yaml
# .github/branch-protection for refactor/hexagonal
required_checks:
  - unit-tests
  - layer-import-guard
  - docker-build
```

### CI pipeline

```yaml
# .github/workflows/refactor-ci.yml
name: Refactor CI
on:
  push:
    branches: [refactor/hexagonal]
  pull_request:
    branches: [refactor/hexagonal]

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: uv sync
      - run: uv run pytest -m unit

  layer-guard:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python scripts/check_layer_imports.py

  docker-build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: docker build -f Dockerfile -t openrag:refactor .
```

### Integration tests (nightly or on-demand)

## Decision Log Template

Keep a running decision log during the :

```markdown
# Refactoring Decision Log

## 2026-04-21: Branch strategy
Decision: Merge during foundation, isolate during
          transformation, freeze during cutover
Reason: dev is active, full merges during Phases 5-9 would create
        unmanageable conflicts

## 2026-04-21: Branch base
Decision: Branch from v1.1.9 tag, not dev
Reason: Frozen, tested baseline. Dev is a moving target.

## [date]: [decision title]
Decision: [what was decided]
Reason: [why]
Alternative considered: [what else was on the table]
```

---

## Risk Mitigation Checklist

Before entering each mode transition:

### Before Mode 1 -> Mode 2

- [ ] All Phase 0-4 deliverables verified
- [ ] Import guard passes
- [ ] Last merge from dev completed and tested
- [ ] FORWARD_PORT_LOG.md created
- [ ] Team informed: "no more merges from dev"

### Before Mode 2 -> Mode 3

- [ ] All Phase 5-9 deliverables verified
- [ ] Integration tests pass on refactor branch
- [ ] FORWARD_PORT_LOG.md reviewed — deferred features catalogued
- [ ] Feature freeze announced to team (2-3 days notice)
- [ ] Freeze start date agreed

### Before cutover

- [ ] All Phase 10-12 deliverables verified
- [ ] All deferred features re-implemented
- [ ] Docker build and compose up works
- [ ] Full integration test suite passes
- [ ] Manual smoke test completed
- [ ] README.md and CLAUDE.md updated
- [ ] Cutover communication drafted
- [ ] Rollback plan: dev-legacy branch preserved
