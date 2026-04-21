# Refactoring Decision Log

Running log of **judgment calls that aren't prescribed by the refactor docs**.
If a decision is grounded in STRATEGY / WORKFLOW / SKILL / GUIDE, it does NOT
belong here — just follow the doc. This file exists so future readers can see
what had to be decided outside the written plan, and why.

Source abbreviations:
- STRATEGY = `.claude/skills/openrag-refacto/REFACTORING_STRATEGY_v1.md`
- WORKFLOW = `.claude/skills/openrag-refacto/REFACTORING_DEV_WORKFLOW.md`
- SKILL    = `.claude/skills/openrag-refacto/SKILL.md`
- GUIDE    = `.claude/skills/openrag-refacto/Refactoring OpenRAG for Enterprise.md`

---

## Phase 0 — Scaffold + import guard + CI wiring (2026-04-21)

**1. The guard ignores files outside the four new layer roots.**
Files under `openrag/components/`, `openrag/routers/`, `openrag/models/`,
`openrag/config/`, `openrag/utils/` are skipped.
- Why: Phase 0's verification requires existing tests to keep passing. If the
  guard ran against legacy code, every old import that doesn't fit the new
  rules would trip the check and block the phase. Legacy code gets migrated in
  Phases 5–12 and the guard picks those files up as they move into the new
  layer roots.
- Alternative considered: whitelist-only enforcement on new code (same idea,
  different framing). What we chose is "enforce wherever the file lives in one
  of the four roots", which is simpler.

**2. Split CI into `layer_guard.yml` + extending existing `lint.yml` and
`unit_tests.yml`, instead of one new `refactor-ci.yml`.**
WORKFLOW's CI example is a single file with three jobs (`unit-tests`,
`layer-guard`, `docker-build`). We took a different shape.
- Why: We already have a well-set-up `unit_tests.yml` and `lint.yml`. Creating
  a parallel `refactor-ci.yml` with its own unit-tests job would duplicate the
  uv setup and caching. Extending the existing files adds a few lines of
  config and reuses everything.
- Alternative considered: follow the WORKFLOW example literally. Rejected for
  the duplication reason above. Trade-off is that refactor-specific CI isn't
  all in one file.

**3. `docker-build` CI check NOT wired in Phase 0.**
The skill lists it as a required check.
- Why: Existing `build.yml` and `build_dev.yml` workflows push images to ghcr,
  which isn't what we want on every refactor push. A lightweight "docker build
  only, don't push" check needs a new job. Deferred to keep Phase 0 scope
  tight. Docker build was verified locally on the phase-0 tree.
- Alternative considered: add the job in this phase. Rejected for scope.
  Follow-up: add a `docker-build` job in a separate PR, modelled on the
  WORKFLOW CI example.

**4. Introduced this decision log as a required artifact, with a rule that
only non-doc-grounded calls get logged.**
The log file lives here. The governing "Decision log — you MUST write to it"
section in SKILL.md that defines the rule and the staleness policy lands via
the skill PR (#325), not this one.
- Why: The three source docs mention `REFACTORING_DECISION_LOG.md` in passing
  (SKILL "Discovering current state" says it "SHOULD exist") but never define
  what goes in it or who maintains it. Without an explicit rule, judgment
  calls leak into PR descriptions and commit messages that are hard to find
  later. Restricting the log to non-doc-grounded calls keeps it short and
  signal-heavy.
- Alternative considered: log everything (grounded + judgment). Rejected —
  grounded decisions are already captured by the docs, so re-stating them in
  the log is noise.

---

## Template for future entries

```
## Phase N — [short title] ([YYYY-MM-DD])

**K. [decision in one line].**
- Why: [what forced the call, what the docs didn't cover].
- Alternative considered: [what else was on the table, why it was rejected].
```
