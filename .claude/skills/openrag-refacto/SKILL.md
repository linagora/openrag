---
name: openrag-refacto
description: Use when working on the OpenRAG hexagonal refactoring — editing or adding files under openrag/core/, openrag/services/, openrag/api/, openrag/di/, OR touching legacy paths openrag/components/, openrag/routers/, openrag/models/, openrag/config/, openrag/utils/ during the refacto (including bug fixes forward-ported from dev), moving code out of components/ or routers/, creating ABCs/registries/adapters/loaders/parsers, writing Ray actors or pipeline stages, writing orchestrators or FastAPI routers during the refacto, working on the refactor/hexagonal branch or any refactor/phase-N-* branch, planning or executing a refactoring phase, resolving branch/merge/forward-port questions, updating dependencies during the refacto, or when a user question mentions refacto, hexagonal, Strangler Fig, ServiceContainer, Registry[T], CatalogStore, PostgresStore, MilvusVectorStore, make_component_factory, forward-port, or any of phases 0-15.
---

# OpenRAG Hexagonal Refactoring

## The three source docs (authoritative)

The three docs live alongside this `SKILL.md` in `.claude/skills/openrag-refacto/`. Before proposing or writing any refacto change, load the relevant section of:

- `./Refactoring OpenRAG for Enterprise.md` — high-level architecture, motivation, 3-layer diagram
- `./REFACTORING_STRATEGY_v1.md` — target layout, design patterns §2, guiding principles §4, phase-by-phase commits (phases 0-15), risk register, migration utilities
- `./REFACTORING_DEV_WORKFLOW.md` — branch strategy, MODE 1/2/3 rules, forward-port log, cutover, mode-transition checklists, CI

Read them with the Read tool (absolute path from repo root: `.claude/skills/openrag-refacto/<filename>`). These docs override general conventions in CLAUDE.md for refacto work. When you can't answer from memory, open the relevant section and cite it (e.g. "STRATEGY §2.3", "WORKFLOW MODE 2").

**Precedence for new code on `refactor/hexagonal`:** Refacto docs > this skill > CLAUDE.md. CLAUDE.md describes the **pre-refacto** architecture (components/, routers/, vectordb god object, Loguru, LangChain Document, etc.). For new code in the hexagonal layout, the refacto docs win. For legacy code being patched via forward-port, CLAUDE.md still describes how that code works today.

## Core principles (non-negotiable)

1. **Deployable after every commit.** Tests pass, app boots. No "I'll fix it in the next commit."
2. **Strangler Fig only.** New file → old file re-exports from new → consumers migrate → shim deleted in Phase 12. Never big-bang a move.
3. **Strict layer dependencies.** Enforced by `scripts/check_layer_imports.py` (created in Phase 0.2).

```
api/ ──► di/ ──► services/ ──► core/
api/ ──► core/               (models, config, ABCs — read-only)
services/ ──► core/
core/  ──► nothing in openrag (stdlib + pure libs only)
di/    ──► core/ + services/ (the ONLY place that crosses boundaries)
```

`core/` MUST NOT import `services`, `api`, or `di`. `services/` MUST NOT import `api`. `api/` MUST NOT import `services/` directly — go through `di/providers.py`.

## Before acting — answer these five

1. **Which layer?** — core / services / api / di (see "Where does this belong" below)
2. **Which phase?** (0-15) — check STRATEGY §5 Phase Overview
3. **Which MODE is active?** (1 / 2 / 3) — see "Discovering current state" below
4. **Strangler Fig stage?** — creating new file, writing shim, migrating consumer, deleting shim?
5. **`FORWARD_PORT_LOG.md` entry needed?** — any cross-branch work in MODE 2/3

If any answer is unclear, open the relevant doc section and ask the user if still unsure.

## Discovering current state

The skill cannot assume what mode/phase is active. To find out:

- Look for `FORWARD_PORT_LOG.md` and `REFACTORING_DECISION_LOG.md` at repo root — both SHOULD exist during the refacto
- Check current branch: `refactor/hexagonal` → we are in the refacto; `refactor/phase-N-*` → the N pins the phase
- Check recent commits on `refactor/hexagonal` for the last completed phase
- Ask the user if still ambiguous — never guess

Phase ↔ Mode mapping: **Mode 1 = Phases 0-4, Mode 2 = Phases 5-9, Mode 3 = Phases 10-12**. Phases 13-15 are post-cutover normal dev.

## Where does this belong? — quick layer decision

| If the code… | Layer | Example |
|---|---|---|
| Defines a type, ABC, pure algorithm, config schema, exception | `core/` | `core/models/Chunk`, `core/retrieval/rrf.py`, `core/embeddings/embedder.py` |
| Speaks HTTP, SQL, Milvus, S3, Ray, filesystem | `services/` | `services/inference/vllm_client.py`, `services/storage/milvus_store.py` |
| Coordinates ports to satisfy a business flow | `services/orchestrators/` | `services/orchestrators/retrieval_service.py` |
| Serializes/deserializes for Ray | `services/workers/` | `services/workers/indexer_actor.py` |
| Is a FastAPI route, middleware, request/response schema | `api/` | `api/routers/admin/partitions.py`, `api/schemas/user/*` |
| Wires everything together | `di/` | `di/container.py`, `di/providers.py`, `di/factories.py` |

## Forbidden patterns (refuse or convert)

| Pattern | Replace with |
|---|---|
| `config = load_config()` at module level | Constructor injection from `ServiceContainer` |
| `ray.get_actor("X")` singleton at import time | Lazy init in `ServiceContainer.initialize()` |
| New `XyzFactory` class | `Registry[T]` + `make_component_factory()` (§2.1-2.2) |
| ABC inheriting LangChain's sync `Embeddings` class | Async-native ABC (`async def embed(...)`, `async def embed_single(...)`) |
| Sync methods in an **I/O-bound** ABC (`Embedder`, `LLM`, `VLM`, `Reranker`, `VectorStore`, `CatalogStore`, `DocumentParser`, any repository port) | Async-native. Wrap sync 3rd-party libs via `asyncio.to_thread()` at the adapter level |
| LangChain imports anywhere in `core/` except inside `from_langchain()` / `to_langchain()` method bodies | Domain models and algorithms in `core/` are LangChain-free; boundary conversion only |
| `from loguru import logger` in new `core/` or `services/` code | Use structlog via `core/utils/logging.py`; `request_id` flows through structlog contextvars (§2.10, §1.3) |
| Dependency bump during MODE 2 | Defer to MODE 3 dependency sync (WORKFLOW "Dependency sync" — `diff pyproject.toml` then batch-update at cutover) |
| Modifying the existing auth (`users.token`, `PartitionRole`, `SUPER_ADMIN_MODE`) during Phases 5-11 | Preserve OpenRAG's current auth as-is (§2.14). OIDC re-implementation is Phase 15; until then, the shape of users/tokens/roles does not change |
| `# noqa: E402` / `# noqa: TID252` on a layer-crossing import to bypass `check_layer_imports.py` | Don't bypass the guard. Refactor the dependency direction instead |
| New inference/HTTP client logs raw payloads without `scrub_secrets()` | Scrub credentials with `core/utils/scrub.py` before any log/trace writes |
| Business logic inside a Ray actor | Actor = thin serialization/error boundary; logic in `services/orchestrators/` or pipeline stages |
| Passing a pydantic model (or ORM object) **into** a Ray actor method | Caller calls `model_dump()` → actor receives `dict` → actor calls `Model.model_validate(dict)`. Never send pydantic objects across the Ray boundary (§2.6) |
| Holding per-request state on a Ray actor instance (`self._current_job = ...`) | Actors are stateless between calls. Any state lives in the caller or in persistence (§2.6) |
| I/O inside `ServiceContainer.__init__` (opening DB pool, HTTP connect, `await` anything) | `__init__` is sync and does zero I/O: load config → register impls → create stores → create factories → create services. All actual connecting happens in `async initialize()`. This is what lets container construction never fail on network (§2.3) |
| Router importing from `components/` or `services/` directly | `Depends(get_service)` from `di/providers.py` |
| LangChain `Document` crossing layer boundaries | `core/models/Document` + `from_langchain()`/`to_langchain()` (Phase 2, removed Phase 12) |
| Component ABC placed in `core/ports/` | Component ABCs are co-located in subject folder. `core/ports/` is CRUD repos only |
| Module-level `Settings` access from `core/` | `core/` receives typed config section via constructor |
| Big-bang move (add new + delete old + rewrite consumers in one commit) | Strangler Fig sequence: add → re-export → migrate → delete-in-Phase-12 |
| Orchestrator receiving concrete `Embedder`/`LLM` instance | Orchestrator receives `Callable[[str], Embedder]` factory (§4.4) |
| Embedding done inside `VectorStore.upsert()` | Embedding is a pipeline stage; `vector_store.upsert()` receives pre-embedded chunks (§7B) |
| New inference client without `_circuit_breaker` / `_retry` / `_timeout` decorators | Wrap every external call; see `services/inference/_*.py` (Phase 6.5) |
| Duplicating chunk text to Postgres `tsvector` for BM25 | Keep Milvus BM25 (sparse vectors) — OpenRAG's chosen approach (§2.12) |
| Error response as bare string / plain exception bubble | Structured JSON with `error.{message,type,code,request_id}`; domain exceptions mapped by MRO walk in `api/error_handlers.py` (§2.10, Phase 10A) |

## Co-located ABC + registry layout (the standard shape)

```
core/embeddings/
  embedder.py           # Embedder ABC (async-native)
  registry.py           # embedder_registry: Registry[Embedder]
  __init__.py           # re-exports Embedder + embedder_registry

# Implementation lives in services/, registers via decorator:
# services/inference/vllm_client.py
@embedder_registry.register("vllm")
class VLLMEmbedder(Embedder):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...

# Wiring via side-effect import:
# di/embedders.py
def register_embedders() -> None:
    import openrag.services.inference.vllm_client  # noqa: F401
```

The `# noqa: F401` is **required** — ruff would otherwise flag the unused import. The import itself is the side effect: importing the module runs the `@embedder_registry.register("vllm")` decorator. The same pattern applies to Ray actors in `services/workers/indexer_actor.py` — the actor's `__init__` re-imports the inference clients it needs so that a fresh Ray worker process has every registration populated (see §2.6 and §9 example).

Same shape for: `rerankers/`, `llm/`, `vlm/`, `chunking/`, `vector_stores/`, `catalog/`, `indexing/parsers/`.

**Async vs sync on ABC methods:**

| ABC | Async? | Why |
|---|---|---|
| `Embedder`, `LLM`, `VLM`, `Reranker` | async | Remote HTTP calls |
| `VectorStore`, `CatalogStore`, all repository ports | async | Network/DB I/O |
| `DocumentParser` | async | File I/O, OCR, VLM calls during parsing |
| `ChunkingStrategy` | **sync** per STRATEGY §4 table (CPU-bound text splitting); the pipeline stage calling it wraps in `asyncio.to_thread()` |
| Prompt builders in `core/prompts/` | sync | Pure string assembly |

STRATEGY §4.6 states "all interface ABCs are async" as the general principle, but the §4 table shows `ChunkingStrategy.chunk(...)` as sync. The table is the authority for ChunkingStrategy; everything else follows the async-native rule. If in doubt, ask the user.

`core/ports/` is for CRUD repository contracts only — `DocumentRepository`, `ChunkRepository`, `UserRepository`, `JobRepository`, `PartitionRepository`, `ConversationRepository`, `PromptRepository`, `EntityRepository`, `TopicTagRepository`, `AuditLogRepository`, `IdempotencyRepository`, `ModelEndpointRepository`, `PresetRepository` (13 total) — composed inside `CatalogStore`.

## CatalogStore composite pattern (§2.5, Phase 7A)

```python
class PostgresStore(CatalogStore):
    def __init__(self, config: PostgresConfig):
        self._conn = ConnectionManager(config)          # owns the pool
        pool_getter = lambda: self._conn.pool            # deferred access
        self._documents = PgDocumentRepository(pool_getter)
        self._users = PgUserRepository(pool_getter)
        # ... 13 repos total, composition not inheritance
```

Repos receive a `pool_getter` lambda so they can defer pool access until `initialize()`. Single `ConnectionManager` owns the lifecycle. This replaces the `PartitionFileManager` god object.

## Pipeline stages (§2.8, Phase 9) — standard shape

Each stage in `services/workers/stages/*.py` is a pure async function following this contract:
- Rows are mutated in-place (no functional overhead)
- Failed rows get `_error` field but still flow to next stage
- Credentials are scrubbed after the stage that consumes them
- Per-stage timeout: base + per-chunk scaling — **except `contextualize`**, which has no stage timeout (to prevent cascade timeouts)

Standard stage sequence: `parse → caption → chunk → contextualize → embed → store`.

## Resilience for inference clients (Phase 6.5)

Every `services/inference/*_client.py` method that calls an external endpoint MUST be wrapped with:
- `@with_circuit_breaker` (aiobreaker)
- `@with_retry` (tenacity + jitter)
- `@with_timeout` (asyncio.timeout)

Decorators live in `services/inference/_circuit_breaker.py`, `_retry.py`, `_timeout.py`. Also use `DistributedLLMSemaphore` for cluster-wide throttling on LLM endpoints.

## What counts as a "critical bug fix" (the only thing allowed to cross MODE 2/3 boundaries)

Only these qualify for forward-porting during MODE 2 or cherry-picking during MODE 3 freeze:

- **Security vulnerabilities** (auth bypass, injection, token leakage, privilege escalation, dependency CVE with active exploit)
- **Data loss / corruption** (migrations, persistence bugs, storage bugs that destroy user data)
- **Production outages** (service crash, hang, OOM, repeated 5xx on primary endpoints, Ray cluster deadlock)

Everything else — new endpoints, UX polish, new loaders, dependency upgrades without a CVE, non-critical bug fixes — is a **feature** and is deferred to the cutover re-implementation queue in `FORWARD_PORT_LOG.md`.

## Naming shifts to watch for

- `loader` → `parser` (Phase 5D; `components/indexer/loaders/*.py` becomes `core/indexing/parsers/*.py`; `BaseLoader` ABC becomes `DocumentParser`)
- `Document` (LangChain) → `Document` (pydantic in `core/models/document.py`) with `ProcessedDocument` for the post-parse stage
- `Vectordb` / `MilvusDB` Ray actor → split into `MilvusVectorStore` (Phase 7B) + `PostgresStore`/CatalogStore (Phase 7A)
- `PartitionFileManager` → composite `PostgresStore` with 13 repos (Phase 7A)
- `RetrieverFactory`, `RerankerFactory`, `ChunkerFactory`, `EmbeddingFactory`, `WebSearchFactory` → `Registry[T]` instances
- Loguru (Ray-coupled) → structlog (refacto default)
- `openrag/scripts/` inside the package → `scripts/` at repo root (Phase 13B)
- `Dockerfile`, `docker-compose.yaml` at repo root → `infra/docker/*.Dockerfile`, `infra/compose/docker-compose.yaml` (Phase 13A)

## Branch workflow quick-reference

| Mode | Phases | Merge from `dev` | Features on `dev` | Key artifact |
|---|---|---|---|---|
| 1 — MERGE | 0-4 | Weekly / at end of mode | Allowed | — |
| 2 — ISOLATE | 5-9 | **Never.** Forward-port critical fixes only | Small + logged | `FORWARD_PORT_LOG.md` |
| 3 — FREEZE | 10-12 | Cherry-pick critical only | **Frozen** (time-boxed 3-4 days) | Re-implement deferred items |

- `refactor/hexagonal` is branched from the `v1.1.9` tag; never merges back into `dev` until the final cutover.
- Per-task branches: `refactor/phase-N-topic`, branched from and PR'd into `refactor/hexagonal`.
- Phase dependencies: **5 + 6 + 7 can run in parallel → converge on 8 → then 9**.
- `FORWARD_PORT_LOG.md` has two sections: "Forward-ported (critical)" with dev-commit → refactor-commit pairs, and "Deferred to cutover (features)" with re-implementation targets.
- `REFACTORING_DECISION_LOG.md` captures structural decisions (branch strategy, patterns adopted) — keep it updated when a non-obvious choice is made.
- See WORKFLOW §"CUTOVER" for branch rename + `--force-with-lease` push sequence.

## Mode-transition gates (all items must pass)

**Before Mode 1 → Mode 2:**
- Phases 0-4 deliverables exist (see WORKFLOW "Exit criteria for Mode 1")
- `scripts/check_layer_imports.py` passes
- `python -c "from openrag.core.models import Chunk, Document, User"` works
- All existing tests still pass
- Last merge from `dev` completed
- `FORWARD_PORT_LOG.md` created
- Team informed: "no more merges from dev"

**Before Mode 2 → Mode 3:**
- All Phase 5-9 deliverables verified
- Integration tests pass (full upload → search → chat cycle) — **required after Phases 7 and 9 specifically**
- `FORWARD_PORT_LOG.md` reviewed; deferred features catalogued
- Feature freeze announced to team (2-3 days notice)
- Freeze start date agreed

**Before cutover (Mode 3 → replacing `dev`):**
- `openrag/components/`, `openrag/routers/`, `openrag/models/` no longer exist
- `di/container.py` ServiceContainer is the composition root
- No module-level `config = load_config()` anywhere
- No module-level Ray actor singletons
- Import guard passes with zero violations
- All deferred features from `FORWARD_PORT_LOG.md` re-implemented
- Full integration test suite passes
- `docker compose -f infra/compose/docker-compose.yaml build` succeeds
- `docker compose up` + manual smoke test (upload, search, chat, manage users) passes
- README.md and CLAUDE.md updated
- Rollback plan: `dev-legacy` branch preserved

## Before each commit on the refacto branch

- [ ] `uv run ruff check openrag/ tests/` passes
- [ ] `uv run ruff format --check openrag/ tests/` passes
- [ ] `python scripts/check_layer_imports.py` passes (from Phase 0 onward)
- [ ] `uv run pytest` passes
- [ ] Old imports still work via re-export shim (if you moved a module)
- [ ] No forbidden pattern introduced (see table above)
- [ ] New `core/` module comes with at least one unit test in `tests/unit/core/**` (risk register: "test gap during migration")
- [ ] New inference client in `services/inference/` has the three resilience decorators applied
- [ ] Commit message title ≤ 72 chars, single line

## Before opening a PR into `refactor/hexagonal`

- [ ] PR targets `refactor/hexagonal`, not `main` or `dev`
- [ ] Scoped to a single phase (or flagged as cross-phase)
- [ ] New ABCs are async-native and co-located with their registry
- [ ] New inference client has the three resilience decorators
- [ ] Any deferred-from-`dev` work logged in `FORWARD_PORT_LOG.md`
- [ ] If in MODE 2: no `dev` merge was performed — forward-port only
- [ ] CI required checks pass: `unit-tests`, `layer-import-guard`, `docker-build`

## Common situations → what to do

| Situation | Action |
|---|---|
| "Add a new embedder backend" | Create `services/inference/xxx_client.py` with `@embedder_registry.register("xxx")`, implement async `Embedder` ABC methods, wrap external calls with resilience decorators, import it in `di/embedders.py` `register_embedders()`. No factory class. |
| "Move module X to new architecture" | Strangler Fig: (1) create new file at target; (2) make old file re-export from new; (3) migrate consumers one PR at a time; (4) delete shim only in Phase 12. Never delete old path in the same PR that adds the new one. |
| "Where should this new thing live?" | Walk the "Where does this belong" table. If it's infra-touching (HTTP/DB/Ray/FS) → `services/`. Pure logic/types/config/ABCs → `core/`. FastAPI glue → `api/`. Wiring → `di/`. |
| "Create a Factory class for component X" | Stop. Use `Registry[T]` (`@registry.register("name")` decorator on class) + `make_component_factory()` in `di/` (§2.1-2.2). |
| "Critical bug fix landed on `dev` in MODE 2" | Don't merge. Read the diff, re-implement against new structure on `fix/forward-port-*`, PR into `refactor/hexagonal`, log in `FORWARD_PORT_LOG.md` under "Forward-ported (critical)". |
| "New feature request during MODE 3 freeze" | Defer. Add to `FORWARD_PORT_LOG.md` deferred list. Freeze is time-boxed 3-4 days. |
| "Per-partition config change" | Phase 14. Do NOT build this during Phases 0-12. |
| "OIDC / SSO work" | Phase 15. OpenRAG already has OIDC in the legacy code — Phase 15 is the clean re-implementation on the hexagonal architecture, preserving API-token auth alongside. |
| "Decomposing the `Vectordb` god object" | Phase 7 (highest risk). Create `MilvusVectorStore` + `PostgresStore(CatalogStore)` with 13 repos via `ConnectionManager` + `pool_getter` lambda. Old actor becomes delegation shim. Integration test upload→search→delete after every commit. |
| "Writing a new Ray pipeline stage" | Pure async function in `services/workers/stages/*.py`. In-place row mutation. Mark failed rows with `_error` but pass through. Scrub credentials after use. Timeout = base + per-chunk, **except for `contextualize` which has no stage timeout**. |
| "Writing a new orchestrator" | New file in `services/orchestrators/*_service.py`. Constructor takes port ABCs (never concretes), factory callables (`Callable[[str], T]`, not instances), and a typed config section. All methods async. No Ray import, no HTTP client import — those belong in adapters. Wire into `di/container.py` `__init__`. |
| "Need to upgrade pymilvus / any dependency during the refacto" | If in MODE 2: defer. Log to `FORWARD_PORT_LOG.md` "Deferred". If in MODE 3: do it as part of the dependency sync step (diff `pyproject.toml` between `dev` and `refactor/hexagonal`, batch update, `uv sync`, re-run test suite). |
| "Fix a bug on a legacy file in `openrag/components/` during MODE 2" | OK — that's a forward-port. Patch the legacy file, keep the fix minimal and well-scoped, log it in `FORWARD_PORT_LOG.md` with both the dev-commit and refactor-commit SHAs. If the legacy file has already been moved/shimmed, apply the fix to the new location in `core/` or `services/` instead. |
| "Need to rewrite imports in bulk" | Use `scripts/rewrite_imports.py --dry-run` (then `--apply`), backed by `scripts/import_mapping.json`. Don't hand-edit imports across the repo. |
| "Writing tests in the refacto" | Use `tests/conftest.py` fixtures: `container` (full ServiceContainer), `mock_vector_store` (InMemoryVectorStore for unit tests). Mark tests with `@pytest.mark.unit` / `@pytest.mark.integration`. |

## Red flags — stop and reopen the docs

- "Let me just move everything at once, it's cleaner" → Strangler Fig violation
- "I'll add a FactoryN class for this new component" → use `Registry[T]`
- "I'll import `openrag.services.*` from a router" → go through `di/providers.py`
- "I'll put this ABC in `core/ports/`" → component ABCs are co-located; `ports/` is CRUD repos only
- "I'll fast-forward merge `dev` into `refactor/hexagonal` real quick" → check the current mode first
- "This is a simple config access, let me just `load_config()` here" → inject via constructor
- "I'll keep the old file importing the new one" → that IS the Strangler Fig pattern; fine in Phases 1-11, MUST be deleted in Phase 12
- "I'll embed inside `vector_store.upsert()` for convenience" → embedding is a pipeline stage; the vector store receives pre-embedded chunks
- "I'll skip the circuit breaker on this one endpoint, it's simple" → every external call in `services/inference/` gets all three resilience decorators
- "I'll have the orchestrator take an `Embedder` instance directly" → orchestrators receive `Callable[[str], Embedder]` factories (§4.4)
- "I'll inherit from LangChain's `Embeddings` class, it's easier" → Embedder is async-native; sync ops wrapped in `asyncio.to_thread()` at the adapter layer
- "Let me add BM25 by duplicating chunk text into Postgres tsvector" → OpenRAG uses Milvus BM25; keep it
- "I'll open the Postgres pool in `ServiceContainer.__init__` so everything is ready" → `__init__` is sync, network-free. Pool opens in `async initialize()`
- "I'll cache the current job on the Ray actor instance to avoid re-fetching" → actors are stateless between calls; cache in caller or persistence
- "I'll pass the `Document` object directly to the Ray actor method" → caller serializes via `model_dump()`; actor validates via `model_validate()`
- "The three docs don't cover this edge case" → ask the user; do not invent

## Anchor back to source docs

When answering a refacto question, cite which doc + section supports your answer (e.g. "STRATEGY §2.3 ServiceContainer", "STRATEGY §7B MilvusVectorStore", "WORKFLOW MODE 2 rules", "WORKFLOW Exit criteria for Mode 2"). If you can't cite, you haven't read enough — open the doc.
