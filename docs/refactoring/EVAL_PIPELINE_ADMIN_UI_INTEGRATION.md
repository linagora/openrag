# Evaluation Pipeline → Admin UI Integration

> **Status:** design doc, no code merged yet. A static, non-functional prototype of
> the target UI exists at
> `tests/load/automatic-evaluation-pipeline/admin_ui_prototype.html` (open it
> directly in a browser — plain HTML/CSS/JS, no build step, no backend calls).
> It demonstrates layout and interaction flow only; **do not** copy its markup
> or JS into the real app — the real app uses React + shadcn components per the
> patterns below.

## 1. Why

The evaluation pipeline (`tests/load/automatic-evaluation-pipeline/`) ships a
Streamlit dashboard (`dashboard.py`) as its only UI. **Streamlit is being
retired.** Its functionality — browsing benchmark runs, comparing them,
triggering new runs, comparing OpenRAG versions — needs a home in the React
admin-ui (`ui/`) before Streamlit goes away, or that functionality is simply
lost.

This doc is the handoff: what exists today, what's decided, what's still open,
and a concrete phase-by-phase plan a new contributor can pick up without
re-deriving any of this from scratch.

---

## 2. Current state

### 2.1 The eval pipeline today

Everything lives in `tests/load/automatic-evaluation-pipeline/`:

| File | Role |
|---|---|
| `upload_files.py` | Index `pdf_files/` into a partition |
| `generate_questions.py` | Cluster chunks → typed, critic-filtered Q/A dataset |
| `benchmark.py` | Run a dataset through OpenRAG, score every metric |
| `context_ablation.py` | With-context vs. closed-book comparison |
| `orchestrator.py` | Deploy one OpenRAG git version, ingest, benchmark, tear down |
| `dashboard.py` | **Streamlit UI being retired** — browse/compare runs, trigger everything above |
| `config.py`, `evaluation_prompts.py`, `judge_schemas.py`, `metrics.py` | Shared config/prompts/schemas/metrics |
| `reports/` | Flat `eval_*.json/csv` (live-instance runs) or `reports/<uuid>/` (orchestrated runs) |

**Architecture note — this is the crux of the whole integration problem:**
`dashboard.py` is a local process that reads/writes files on disk directly and
launches `subprocess.Popen` on the other scripts. `orchestrator.py` goes
further and shells out to `docker compose up/down` against a **separate git
clone** ("the versions repo") to stand up a different OpenRAG version
entirely. None of this goes through OpenRAG's HTTP API — it *is* the process
that would need to be turned into API calls.

### 2.2 The admin-ui today

`ui/` — React 19 + Vite + TS + Tailwind 4 + shadcn/Radix, `@tanstack/react-query`
for data + polling, `@tanstack/react-table` for tables, `react-hook-form` +
`zod` for forms, `recharts` for charts, `sonner` for toasts, MSW for mocks.
Existing pages: Overview, Partitions, Documents, Jobs, Models, Presets, Users,
System, Settings. **There is no Evaluation page and no eval-related backend
route today** — this is 100% new surface, not a wiring exercise.

Key conventions to copy (file:line references are to the current tree):

| Concern | Copy this pattern from |
|---|---|
| List page with polling + status filter tabs | `ui/src/pages/admin/jobs/list.tsx` |
| Detail page with terminal-state-aware polling + cancel | `ui/src/pages/admin/jobs/detail.tsx` |
| Create-dialog form + mutation + toast + invalidate | `ui/src/pages/admin/presets.tsx` |
| API client module shape (doc comment mapping to backend routes, typed helpers) | `ui/src/lib/api/jobs.ts` |
| Auth/fetch wrapper | `ui/src/lib/api/client.ts` |
| Capability gating | `ui/src/lib/permissions.ts` |
| Nav registration | `ui/src/components/layout/sidebar.tsx` (`navItems` array) |
| Route registration (admin-only, lazy-loaded) | `ui/src/router.tsx` (`ModelsPage`/`PresetsPage` entries) |
| Real logo assets | `ui/public/openrag-title-white.svg` (sidebar wordmark, all-white, used on the crimson sidebar bg), `ui/public/logo-openrag.svg` (icon mark) |
| Theme tokens | `ui/src/index.css` (`:root` / `.dark` — light primary `#c71f45` crimson, dark primary shifts to `oklch(0.62 0.22 277)` indigo) |

### 2.3 The precedent that answers "how do we store this"

OpenRAG already solved "queryable metadata + bulky files, safely" for uploaded
documents — reuse it verbatim rather than inventing anything:

- **Postgres `files` table** (`openrag/services/persistence/schema.py:117`) —
  small, indexed, queryable metadata (`file_id`, `partition_name`,
  `file_metadata` JSON, `created_by`, timestamps).
- **Raw bytes on a filesystem volume** — `paths.data_dir` (env `DATA_DIR`),
  mounted via `${DATA_VOLUME:-../../data}:/app/data` in
  `infra/compose/docker-compose.yaml`.
- **Never a static mount** — `openrag/api/routers/user/download.py` explicitly
  rejects that (would expose every tenant's source) and instead: looks up the
  DB-stored path, resolves it, checks `is_relative_to(DATA_DIR)` to defeat
  traversal, streams it back per-request with auth applied.

No object storage (S3/MinIO) exists anywhere in the stack today. Don't
introduce one for this feature — eval artifacts are smaller and far more
disposable than the document corpus they'd be joining.

---

## 3. Scope: what moves, what doesn't

| Capability | Where it ends up |
|---|---|
| Browse past runs, trend charts, compare two runs, logprobs viewer | **Admin-ui**, backed by a new read-only API |
| Trigger a benchmark / ablation / dataset-generation run **against the currently-deployed instance** | **Admin-ui**, backed by a new trigger API + background job |
| Compare a different OpenRAG **version** (what `orchestrator.py` does) | **Admin-ui trigger, but execution stays decoupled** — see §5. The API process never runs `docker compose` itself. |
| The raw `docker compose up/down` deploy/teardown mechanics | **Stays exactly as `orchestrator.py` today**, just invoked by a new out-of-process worker instead of Streamlit's `subprocess.Popen` |

The reason version-compare isn't simply "not ported": the API container has
no Docker socket, no versions-repo checkout, and giving a multi-tenant web API
either of those is a real privilege-escalation surface. See §5 for the
decoupled design that gets the feature into the UI anyway.

---

## 4. Storage design

**Decided:** mirror the `files` table / `data_dir` precedent exactly.

```
eval_runs (Postgres table, analogous to files)
  run_id           uuid, PK
  run_type         text   -- 'benchmark' | 'ablation' | 'generation' | 'version_compare'
  partition        text
  version          text, nullable   -- git ref for version_compare rows; null = live instance
  status           text   -- QUEUED | RUNNING | COMPLETED | FAILED | CANCELLED
  created_by       int, FK -> users.id
  created_at       timestamptz
  completed_at     timestamptz, nullable
  config_snapshot  jsonb   -- reproducibility: full CONFIG snapshot at trigger time
  summary_metrics  jsonb   -- small aggregates (hit_rate, mrr, rougeL, judge means,
                           -- latency mean...) so list/sort/trend views never open a file
  artifact_dir     text    -- relative path under the confined eval artifact root
  fail_reason      text, nullable   -- e.g. "All queries failed — AUTH_TOKEN rejected"
```

Filesystem holds only the bulky, sequential artifacts per run:
`eval_<ts>.json/csv`, `response_labels_<ts>.csv`, `cot_audit_<ts>.csv`,
`logprobs_<ts>.jsonl` (unlocks the token-confidence viewer), and for
`version_compare` rows: `run_config.json`, `container_logs.txt`.

Served through a new confined route (mirrors `download.py`'s traversal
defense exactly):

```
GET /admin/eval/runs/{run_id}/artifacts/{name}
```

### Open question — needs a decision before the migration is written

Where does the filesystem root live?

- **Option A** — a subtree of the existing `paths.data_dir` (e.g.
  `data/eval_runs/<run_id>/`). One less config knob.
- **Option B (recommended)** — a new dedicated `paths.eval_dir` config knob
  (env `EVAL_DIR`, added to `openrag/core/config/infrastructure.py` and the
  `("EVAL_DIR", "paths.eval_dir", str)` tuple in
  `openrag/core/config/loader.py`, next to `DATA_DIR`/`LOG_DIR`/`DB_DIR`), with
  its own compose volume.

Recommendation is B: documents are precious tenant content kept indefinitely;
eval runs are disposable/regenerable and need their own retention/cleanup
policy (a `DELETE /admin/eval/runs/{run_id}` that drops the Postgres row and
`rm -rf`s `artifact_dir`, same shape as `delete_partition`'s cascade). Mixing
the two directories makes that cleanup harder to reason about and risks an
eval-cleanup bug touching document storage. **This was raised with the project
owner and not yet confirmed — resolve it before writing the alembic
migration**, since the column comment / default path depend on the answer.

If Ray workers ever run on separate physical nodes (per `infra/cluster.yaml`),
this artifact root needs to be a shared mount across nodes — same requirement
`/ray_mount/data` already has today, not a new constraint.

---

## 5. Version-compare: decoupled trigger/execution design

This is the part of the integration most likely to go wrong if built naively
as "just another button that calls the API," so it gets its own section.

### 5.1 Why it can't be a normal endpoint

1. **Docker socket exposure.** Running `docker compose up/down` requires the
   host's Docker socket. Mounting that into the container serving multi-tenant
   HTTP traffic turns any bug there into host root.
2. **Resource contention with the instance serving the UI.** Each deployed
   version is a full Ray + vLLM stack (`shm_size: 10.24gb` each). This host
   runs with 6–23 GiB free typically — two admins clicking "compare a version"
   at once is enough to OOM either the new stack or the production instance
   the admin-ui itself is running on. Nothing else in the admin-ui can hurt the
   host this way.

### 5.2 The design

- **Trigger = a Postgres row, nothing else.** `POST /admin/eval/runs` with
  `run_type: "version_compare"` just writes an `eval_runs` row with
  `status: "QUEUED"`. The API does no subprocess work, no Docker.
- **Execution = a separate, out-of-process worker.** A systemd unit or
  cron-driven poller (lives under `infra/scripts/` or `infra/ansible/`, **not**
  inside the `openrag`/Ray container) polls
  `eval_runs WHERE run_type='version_compare' AND status='QUEUED'`, claims one
  row (`SELECT ... FOR UPDATE SKIP LOCKED` or an advisory lock), and only then
  calls the existing `orchestrator.py` logic unchanged. This worker is the
  only thing that ever touches the Docker socket or the versions-repo clone.
- **Cancel** flips a `cancel_requested` flag the worker checks between phases
  — you can't `ray.cancel` a `docker compose up`.
- **Trust tier**: gate the trigger action behind `superAdmin` (the same tier
  as the System page), not plain `isAdmin` — one click here can consume the
  whole host's spare RAM budget. *(Also not yet confirmed with the project
  owner — flagged as open in §7.)*

### 5.3 Guardrails the worker must encode

These are hard-won operational lessons from running `orchestrator.py`
manually — do not skip them when building the worker, or the UI will just be
a nicer way to hit the same failures.

| Known failure mode | Guardrail |
|---|---|
| Host OOM from running multiple full stacks at once (`generate_questions` hitting `/chunks` loads ALL chunks + embeddings into memory; observed free RAM 6–23 GiB) | Preflight check of free host RAM before starting; refuse and mark `FAILED` with a clear message instead of letting it OOM mid-run |
| Multiple concurrent version-compare runs competing for RAM | Hard concurrency cap of 1 in the worker (tune later once headroom is actually measured) |
| Different OpenRAG versions publish different/hardcoded host ports | Reuse the existing per-run compose override: `ports: !override []` on every service, re-publish only `${APP_PORT}:8080` on a free port. Don't regress to per-env-var port remapping. |
| `/health_check` returns 403 without auth on some versions | Health poll and `upload_files.check_api` must always send `AUTH_TOKEN` |
| Compose file moved between versions (`docker-compose.yaml` at repo root for old versions vs. `infra/compose/docker-compose.yaml` for the hexagonal refactor) | Auto-detect which path exists in the checked-out versions-repo; already implemented in `orchestrator.py`, just don't reintroduce a hardcoded path |
| Refactored compose requires `POSTGRES_PASSWORD` as an interpolation var | Merge the deploy `.env` into the **compose** env, not just the eval-scripts env |
| `.env` has inline `# comment`s and quoted values | `parse_env_file` must strip inline comments and honor quotes |
| Deploy `.env`'s `EMBEDDER_MODEL_NAME` is sometimes an invalid/remote-only HF repo id, crashing the local `vllm-gpu` on load | Override to a known-good local embedder for eval-triggered runs (`EMBEDDER_BASE_URL=http://vllm:8000/v1`, `EMBEDDER_MODEL_NAME=jinaai/jina-embeddings-v3`) rather than trusting whatever the deploy env says |
| "Empty metrics" (all None, latency only) silently reads as a valid low score | This means OpenRAG returned 500 on generation, not an orchestrator bug. Worker must grep `container_logs.txt` for the 500/`BadRequestError` signature and write a real `fail_reason` ("target model rejected requests — see logs") instead of a clean-looking all-None row |
| Session-bound background waiters die when the triggering terminal session ends | This is exactly why the worker must be systemd/cron-supervised, not a subprocess of anything session-scoped |

### 5.4 Self-contained vs. fixed-dataset comparison

`orchestrator.py --generate-questions` (self-contained mode) generates the
dataset from the version's *own* freshly-indexed chunks, so chunk ids match
and retrieval metrics are non-zero. A fixed golden dataset scored against a
freshly re-indexed instance gives retrieval = 0, because Milvus auto-IDs
differ per index. **Not yet implemented:** using `file_id` (a content hash) as
the stable join key for fixed-dataset cross-version comparison — flagged here
so whoever builds the comparison view doesn't rediscover this the hard way.

---

## 6. Backend changes (new code, in order)

1. **Migration** — new alembic revision adding `eval_runs`
   (`openrag/services/persistence/migrations/alembic/versions/`). Follow the
   idempotency rule already in `CLAUDE.md`: guard with
   `table_exists`/`column_exists` in both `upgrade()` and `downgrade()`,
   because `Base.metadata.create_all()` already creates this table on a fresh
   bootstrap before alembic runs.
2. **Schema** — add the `eval_runs` `Table(...)` to
   `openrag/services/persistence/schema.py`, next to `files`.
3. **Config** (only if Option B from §4 is chosen) — add `eval_dir: Path` to
   `openrag/core/config/infrastructure.py` and register `EVAL_DIR` in
   `openrag/core/config/loader.py`.
4. **`EvalService`** — new file `openrag/services/orchestrators/eval_service.py`,
   same shape as `services/orchestrators/job_service.py`: list/get/create rows,
   compute `summary_metrics`, resolve/confine artifact paths. For
   `run_type != "version_compare"`, this service actually runs the
   benchmark/ablation/generation logic (as a background task or a new Ray
   actor — reuse `call_ray_actor_with_timeout` if it goes through Ray). For
   `run_type == "version_compare"`, it only ever writes the `QUEUED` row —
   the worker in §5 does the rest.
5. **DI wiring** — `get_eval_service` provider in `openrag/di/providers.py`.
6. **Router** — new `openrag/api/routers/admin/eval.py`, thin HTTP layer over
   `EvalService` (same shape as `routers/admin/jobs.py`):
   - `GET /admin/eval/runs` (list, filterable by `run_type`/`status`/`partition`)
   - `GET /admin/eval/runs/{run_id}`
   - `POST /admin/eval/runs` (trigger)
   - `DELETE /admin/eval/runs/{run_id}` (cancel if active, else hard-delete + artifact cleanup)
   - `GET /admin/eval/runs/{run_id}/artifacts/{name}` (confined file serving, mirrors `download.py`)
7. **RBAC** — `require_admin` on everything; the `version_compare` branch of
   `POST /admin/eval/runs` additionally needs the stricter `superAdmin` check
   (see open question in §7).
8. **The worker** — new standalone script, e.g.
   `tests/load/automatic-evaluation-pipeline/version_runner_worker.py`, polling
   the same Postgres DB the API uses. Deployed as a systemd unit (natural home:
   `infra/ansible/` or `infra/scripts/`, following existing infra conventions),
   never as a docker-compose service in the main stack. Encodes every
   guardrail in §5.3.

---

## 7. Frontend changes (new code, in order)

1. **`ui/src/lib/api/eval.ts`** — new client module, same shape as
   `lib/api/jobs.ts`: a doc-comment mapping to the backend routes above, typed
   request/response interfaces, `isTerminalState`-style helpers reused or
   duplicated from `jobs.ts`.
2. **`ui/src/lib/permissions.ts`** — add `canManageEval: isAdmin` (list/trigger
   live-instance runs) and, per §5.2, a stricter gate for triggering
   `version_compare` specifically — either reuse `superAdmin` directly or add
   `canRunVersionCompare: superAdmin`.
3. **`ui/src/components/layout/sidebar.tsx`** — one `navItems` entry:
   `{ title: "Evaluation", href: "/eval", icon: FlaskConical, requires: (p) => p.canManageEval }`.
4. **`ui/src/router.tsx`** — lazy-loaded, `AdminRoute`-wrapped routes `/eval`
   and `/eval/:runId`, same pattern as `ModelsPage`/`PresetsPage`.
5. **`ui/src/pages/admin/eval/list.tsx`** — `useQuery` + `DataTable`
   (`jobs/list.tsx` pattern), `refetchInterval` active only while any row is
   `QUEUED`/`RUNNING`. Stat tiles (total runs, running now, best hit rate, avg
   judge score) above the table. Trend chart section below the stat tiles
   using **recharts** — port the metric groupings directly from
   `TREND_METRIC_GROUPS` in `dashboard.py` (Retrieval / Generation / Judge &
   Faithfulness / Relevancy / Refusal / Latency), they translate almost
   line-for-line into a chart config object.
6. **New-run dialog** (inline in `eval/list.tsx`, `presets.tsx`'s dialog
   pattern) — `react-hook-form` + `zod`, fields: target segmented control
   (Live instance / Compare a version — the latter reveals a version/git-ref
   picker, gated per §5.2/§7 open question), partition, dataset source, limit.
   `useMutation` → `toast` → `queryClient.invalidateQueries(["eval-runs"])`.
7. **`ui/src/pages/admin/eval/detail.tsx`** — `useQuery` with
   terminal-state-aware `refetchInterval` (`jobs/detail.tsx` pattern exactly),
   `ConfirmDialog` + `useMutation` for cancel, `Card`/`dl` metric layout. For
   `version_compare` rows: a comparison table against the current live-instance
   baseline (metric / this version / baseline / delta, colored by
   better/worse). For completed rows: the per-token logprobs confidence
   viewer, reading `logprobs_<ts>.jsonl` — currently raw HTML/CSS in
   `dashboard.py`, genuinely portable to a small React component.
8. **Artifact downloads — new territory, no existing pattern.** Grepped the
   whole SPA: there is no blob-download helper anywhere today (documents are
   viewed as JSON, never downloaded as files). Add a small `downloadBlob`
   helper that does an authenticated `fetch` (reusing `client.ts`'s bearer
   header handling) then `URL.createObjectURL` — don't use a bare `<a href>`
   to the API, since that can't attach the `Authorization` header and would
   otherwise tempt widening the `?token=` query-param bypass (today scoped
   only to `/static`) just for this feature.
9. **`ui/src/mocks/handlers.ts`** — MSW entries for every new endpoint, same
   convention as every other feature, needed for dev/test without a backend.
10. **Tests** — a `*.test.tsx` per new page, matching existing coverage
    (`presets.test.tsx`, `admin-route.test.tsx` as reference shape).

### Reference prototype

`tests/load/automatic-evaluation-pipeline/admin_ui_prototype.html` is a static,
dependency-free mockup built from the real theme tokens (`ui/src/index.css`)
and the real logo (`ui/public/openrag-title-white.svg`), with fake in-page
state simulating `QUEUED → RUNNING → COMPLETED`. Use it to see the intended
layout, the live/version-compare toggle, and the comparison-table/logprobs
concepts — **not** as source to copy; it's vanilla HTML/CSS/JS with hand-rolled
fake data and none of the real component library.

---

## 8. Suggested build order

Frontend work is blocked on backend existing — there's nothing to call yet.
Recommended phases, each independently shippable:

| Phase | Scope | Depends on |
|---|---|---|
| 1 | Migration + `eval_runs` schema + `EvalService` (live-instance runs only) + router + RBAC | §4 decision resolved |
| 2 | `eval.ts` client + `list.tsx` + `detail.tsx` (list/poll/cancel only, no trigger yet) | Phase 1 |
| 3 | Trigger dialog (live-instance only) | Phase 2 |
| 4 | Trend charts + two-run compare view | Phase 2 |
| 5 | Logprobs viewer | Phase 1 artifact-serving route |
| 6 | `version_compare` support: worker script + guardrails (§5.3) + UI toggle + trust-tier gating | Phases 1–3, §7 open question resolved |

This mirrors how Jobs itself shipped — list + detail + cancel first, richer
views layered in after.

---

## 9. Open questions — resolve before/while building, don't assume

1. **§4** — dedicated `paths.eval_dir` vs. reusing `paths.data_dir`. Recommendation: dedicated. Not yet confirmed.
2. **§5.2/§7** — trust tier for triggering `version_compare`: `superAdmin` vs plain `isAdmin`. Recommendation: `superAdmin`. Not yet confirmed.
3. **Concurrency cap** for the version-runner worker — set to 1 initially; needs real headroom measurement on the target host to see if it can ever safely go higher.
4. **Retention policy** — how long do completed `eval_runs` rows + artifacts live before cleanup? No TTL/cleanup job designed yet; needed before this ships to avoid unbounded disk growth (every version-compare run leaves a `container_logs.txt` + full artifact set).
5. **§5.4** — `file_id`-based join key for comparing a fixed golden dataset across re-indexed instances. Not implemented; needed for meaningful fixed-dataset cross-version comparison.

---

## 10. Appendix — quick file index

**Eval pipeline:** `tests/load/automatic-evaluation-pipeline/{benchmark,config,orchestrator,dashboard,generate_questions,context_ablation,evaluation_prompts,judge_schemas,metrics}.py`, `README.md` (kept current with every pipeline change — read it for the latest CLI flags/config knobs).

**Admin-ui patterns:** `ui/src/pages/admin/jobs/{list,detail}.tsx`, `ui/src/pages/admin/presets.tsx`, `ui/src/lib/api/{jobs,client}.ts`, `ui/src/lib/permissions.ts`, `ui/src/components/layout/sidebar.tsx`, `ui/src/router.tsx`.

**Storage precedent:** `openrag/services/persistence/schema.py` (`files` table), `openrag/api/routers/user/download.py`, `openrag/core/config/infrastructure.py`, `openrag/core/config/loader.py`.

**Existing job/queue pattern to mirror:** `openrag/services/orchestrators/job_service.py`, `openrag/api/routers/admin/jobs.py`, `openrag/api/dependencies/auth.py` (`require_admin`).

**Prototype:** `tests/load/automatic-evaluation-pipeline/admin_ui_prototype.html`.
