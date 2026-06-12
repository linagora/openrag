# Phase 15 — Keycloak Group → Partition Authorization (reconciled & uniformized)

## Context

`docs/refactoring/REFACTORING_STRATEGY_v1.md` §"Phase 15 — OIDC / Keycloak SSO" specs an OIDC
feature in two halves:

1. **SSO login** — interactive authentication via an external IdP, and
2. **Claim-driven authorization** — map Keycloak *groups → partition memberships* and a
   *role → `is_admin`*.

**Half #1 already shipped — but as a different design with different variable names.** Both the
strategy doc and `docs/refactoring/REFACTORING_DEV_WORKFLOW.md:434` sketch a *stateless
raw-JWT-bearer* flow (`jwt_validator.py` / `oidc_mapper.py` / `oidc_provisioner.py`,
`api/dependencies/auth.py` dual-auth dispatch) and invent an env surface for it
(`OIDC_ENABLED`, `OIDC_ISSUER_URL`, `OIDC_AUDIENCE`, `VITE_OIDC_*`…). What actually exists in the
tree (and in `docs/oidc.md` + `docs/sso-quickstart.md`) is a **session-cookie Authorization Code +
PKCE** flow with its own, already-stable variable surface (`AUTH_MODE=oidc`, `OIDC_ENDPOINT`,
`OIDC_REDIRECT_URI`, `OIDC_TOKEN_ENCRYPTION_KEY`, …). It covers SSO login, back-channel logout,
lazy refresh, programmatic `users.token` access, and `display_name`/`email` claim sync.

So Phase 15 is **not** a greenfield build. It is:

- **(A) Uniformization** — the strategy doc names several things that *already exist under
  different names*. We adopt the shipped names as canonical, drop the strategy doc's duplicates,
  and never introduce a second variable for an existing concept.
- **(B) The one genuinely-missing half** — claim-driven authorization (groups → partition
  memberships). Today the callback consumes *no* group/role claims; partition access is managed
  only via the `/partition/{p}/users` admin API.

This document is the plan for **both**. No code is written yet.

---

## Part A — Variable uniformization

The single source of truth is the **shipped** `OIDCConfig` in
`openrag/core/config/auth.py` (env reader: `OIDCConfig.from_env`). The strategy doc's Phase 15
variable list is reconciled against it below. **Rule: never add a new variable for a concept that
already has a shipped variable.**

### A.1 — Already shipped under a different name → use the shipped name (no new var)

| Strategy-doc var / field        | Shipped canonical (KEEP)        | Notes                                                            |
| ------------------------------- | ------------------------------- | --------------------------------------------------------------- |
| `OIDC_ENABLED` / `enabled`      | **`AUTH_MODE=oidc`**            | Boolean enable is derived from `AUTH_MODE`; no `OIDC_ENABLED`.   |
| `OIDC_ISSUER_URL` / `issuer_url`| **`OIDC_ENDPOINT`**             | Same meaning (issuer base for discovery). Field stays `issuer_url`, env stays `OIDC_ENDPOINT`. |
| `OIDC_CLIENT_ID` / `client_id`  | **`OIDC_CLIENT_ID`**            | Identical — no change.                                           |
| `OIDC_CLIENT_SECRET`            | **`OIDC_CLIENT_SECRET`**        | Identical — no change.                                           |
| `OIDC_AUTO_PROVISION` / `auto_provision` | **`OIDC_AUTO_PROVISION_LOGIN`** | Shipped name is more explicit; keep it.                  |
| `OIDC_DEFAULT_QUOTA` / `default_quota` | **`DEFAULT_FILE_QUOTA`** (`rdb.default_file_quota`) | Global quota default already exists (`loader.py:55`); auto-provisioned users already inherit it. No OIDC-specific quota var. |

### A.2 — Subsumed by an existing, more general mechanism → do NOT add

| Strategy-doc field | Subsumed by (KEEP)                       | Notes                                                                          |
| ------------------ | ---------------------------------------- | ------------------------------------------------------------------------------ |
| `claim_sub`        | hard-coded `sub` matching                | Matching is exclusively `external_user_id == sub` (documented); not configurable by design. |
| `claim_name`       | `OIDC_CLAIM_MAPPING=display_name:<claim>`| The generic claim-mapping already syncs display name.                          |
| `claim_email`      | `OIDC_CLAIM_MAPPING=email:<claim>`       | Same — generic mapping already syncs email.                                    |
| (claim read source)| `OIDC_CLAIM_SOURCE` (`id_token`/`userinfo`)| Already chooses where claims are read.                                        |

### A.3 — Strategy-doc vars that do NOT apply to the shipped design → DROP

| Strategy-doc var | Reason dropped                                                                                     |
| ---------------- | -------------------------------------------------------------------------------------------------- |
| `OIDC_AUDIENCE` / `audience` | The session flow validates the ID token via Authlib against `client_id`/nonce; no separate audience knob is exposed. (Revisit only if a raw-JWT-bearer path is ever added.) |
| `OIDC_JWKS_CACHE_TTL` / `jwks_cache_ttl` | JWKS fetching/caching is handled inside the OIDC client (Authlib); not an operator knob. |
| `VITE_OIDC_ENABLED` / `VITE_OIDC_ISSUER_URL` / `VITE_OIDC_CLIENT_ID` | Frontend needs none — login is a backend `302 → /auth/login` redirect; the indexer-ui does not run its own OIDC client. |
| stateless raw-JWT bearer dual-auth (`_is_jwt`/`eyJ`) | Redundant: session cookie (UI) + `users.token` (machine) already cover both audiences. Out of scope. |

### A.4 — Genuinely NEW variables this phase adds (Part B)

Named to match the **shipped** `OIDC_*` convention, *not* re-inventing. These are the only new
env vars introduced:

| New env var (field)                         | Default                        | Purpose                                                    |
| ------------------------------------------- | ------------------------------ | ---------------------------------------------------------- |
| `OIDC_CLAIM_GROUPS` (`claim_groups`)        | `""` (feature OFF)             | Name of the claim holding the user's group list. Empty disables group sync entirely. |
| `OIDC_GROUP_PREFIX` (`group_prefix`)        | `/openrag/`                    | Prefix stripped from each group before pattern matching.   |
| `OIDC_GROUP_PATTERN` (`group_pattern`)      | `(.+)/(owner\|editor\|viewer)$`| Regex → capture group 1 = partition, group 2 = role.       |
| `OIDC_GROUP_SYNC_PRUNE` (`group_sync_prune`)| `false`                        | When true, remove memberships absent from the token (Keycloak = sole source of truth). |

**Deferred behind an Open Decision (NOT added unless approved):** `OIDC_CLAIM_ROLES` +
`OIDC_ADMIN_ROLE` (role → `is_admin`). The shipped design documents a *hard* boundary —
`is_admin` is never writable via claims — so these are out by default. See Open Decisions.

### A.5 — Fidelity review vs. the strategy docs (intentional deviations)

The strategy doc (`REFACTORING_STRATEGY_v1.md` §Phase 15) and the workflow doc
(`REFACTORING_DEV_WORKFLOW.md:434`) both describe the *unbuilt, greenfield* design
(stateless JWT bearer: `jwt_validator.py` / `oidc_mapper.py` / `oidc_provisioner.py`,
`api/dependencies/auth.py` dual-auth dispatch, indexer-ui `oidc-client-ts`). The shipped
reality is the session-cookie flow, so this plan **deliberately diverges** on the points below.
Each deviation is intentional and justified — listed here so a future reader doesn't "correct"
the plan back toward the obsolete spec:

| # | Strategy doc says | This plan does | Why |
| - | ----------------- | -------------- | --- |
| 1 | `claim_groups` defaults to `"groups"` → **group sync ON by default** | `OIDC_CLAIM_GROUPS` defaults to `""` → **OFF by default** | Greenfield could default-on; we add to a *shipped* system and must not silently change any existing deployment's authorization. |
| 2 | `group_pattern = (.+)/(owner\|editor\|viewer)` (no end anchor) | adds a trailing `$` anchor | Avoids a trailing-garbage group accidentally matching; stricter, operator-overridable. |
| 3 | Surfaces only `OIDC_ADMIN_ROLE` + `OIDC_GROUP_PREFIX` as env; `claim_groups`/`group_pattern` are hard-coded defaults | also surfaces `OIDC_CLAIM_GROUPS` + `OIDC_GROUP_PATTERN` as env | The group claim name varies per IdP (Keycloak `groups` vs LLNG/Azure); it must be operator-settable for the feature to work at all. |
| 4 | role → `is_admin` is in-scope (`claim_roles`, `admin_role`, `_is_admin()`) | **deferred** behind Open Decision #2 | Honoring it breaches the shipped, documented "`is_admin` never claim-writable" boundary (`docs/oidc.md` §Claim Mapping). Needs an explicit, loud opt-in. |
| 5 | New files `jwt_validator.py`, `oidc_mapper.py`, `oidc_provisioner.py`; dispatch in `api/dependencies/auth.py` | one pure helper `services/auth/oidc_groups.py` + a method on the existing `AuthService`; no `api/dependencies/auth.py` change | Those files target the stateless-bearer path that the session-cookie flow already obviates. Authn lives in `AuthMiddleware` + `AuthService.handle_oidc_callback`, which is where the group sync belongs. |
| 6 | Adds `get_user_by_external_id()` to `UserRepository` | already exists (`user_repo.py:130`) | Shipped during the session-OIDC work; nothing to add. |
| 7 | `default_quota = 10`, `auto_provision = True` defaults | uses shipped `DEFAULT_FILE_QUOTA` (= `-1`) and `OIDC_AUTO_PROVISION_LOGIN` (= `false`) | These are pre-existing shipped defaults; this plan does not re-open them. |

**Consistent with the docs (no deviation):** the four-step Keycloak setup, group-prefix stripping
to `/openrag/`, `viewer/editor/owner` role vocabulary (== `AUTH_SERVICE.ROLE_HIERARCHY`), and
"Keycloak is source of truth" framing (offered via `OIDC_GROUP_SYNC_PRUNE`).

---

## Part B — Implementation: group → partition membership sync

### What's already in place (reuse, do not rebuild)

- **Seam** — `AuthService.handle_oidc_callback()`
  (`openrag/services/orchestrators/auth_service.py:165`). After resolving the user it already runs
  `_sync_auto_provisioned` (`:207`) then `_apply_claim_mapping` (`:208`). The new membership sync
  slots in **immediately after `:208`**.
- **Membership repo already injected** — `self._membership_repo` (`auth_service.py:124`, comment:
  *"Retained for the role/membership helpers that later phases will [use]"*). Clean port methods
  (`openrag/services/persistence/partition_membership_repo.py` /
  `openrag/core/ports/partition_membership_repo.py`):
  `list_user_partitions(user_id)`, `assign_partition(UserPartition)`,
  `update_partition_role(user_id, partition, PartitionRole)`, `remove_partition(user_id, partition)`.
  Use these (not the `*_partition_member` legacy methods marked `TODO(phase-9)`).
- **Config + startup validation** — `OIDCConfig` / `OIDCConfig.from_env()` in
  `core/config/auth.py`; validated once at startup by `ServiceContainer` (`di/container.py:98`).
- **`User` shape & helpers** — callback `user` is a `core.models.user.User` (`.id`, `.email`,
  `.display_name`, `.external_user_id`); `PartitionRole` enum values are `viewer`/`editor`/`owner`,
  matching `AuthService.ROLE_HIERARCHY`.
- **FK reality** — `partition_memberships.partition_name` → `partitions.partition` (CASCADE,
  `schema.py:202`). Assigning a membership for a non-existent partition raises a FK violation,
  which the per-membership try/except (below) catches → logged & skipped ("skip + warn" default).

### Step 1 — Extend `OIDCConfig` (`core/config/auth.py`)

Add the four `A.4` fields. In `from_env()`, read the four env vars; validate that
`OIDC_GROUP_PATTERN` compiles and exposes ≥2 capture groups (parse-and-discard, like the existing
`_parse_oidc_claim_mapping`, so a bad regex fails at startup regardless of whether the feature is
on). Group sync is **active only when `OIDC_CLAIM_GROUPS` is non-empty** → zero behavior change for
every current deployment.

### Step 2 — New pure mapper `openrag/services/auth/oidc_groups.py`

```python
def extract_partition_roles(claims, cfg) -> list[tuple[str, str]]:
    # "/openrag/project-alpha/editor" --strip prefix--> "project-alpha/editor"
    #   --pattern--> ("project-alpha", "editor")
    # Drop non-matching groups; de-dupe keeping the highest role per partition.
```
No I/O, fully unit-testable. Accepts a list/str groups claim; uses a local
`_ROLE_RANK = {"viewer":1,"editor":2,"owner":3}` (kept local so the pure mapper has no dependency
on the orchestrator layer). Imported directly from the submodule (not re-exported via
`services/auth/__init__.py`) to avoid any import cycle.

### Step 3 — `_sync_oidc_memberships` in `AuthService` + call after `:208`

```python
async def _sync_oidc_memberships(self, user, claims) -> None:
    if not (self._config.claim_groups or "").strip():
        return
    desired = dict(extract_partition_roles(claims, self._config))      # {partition: role}
    current = {m.partition: m.role.value
               for m in await self._membership_repo.list_user_partitions(user.id)}
    # add missing (assign_partition) / update changed (update_partition_role)
    # if group_sync_prune: remove_partition for partitions in current - desired
    # each membership op wrapped in try/except → logger.bind(...).warning + continue
    #   (one bad/unknown-partition group must never block the login)
```
Reads groups from `bundle.claims` — the same verified ID-token claims already in hand, **no extra
IdP round-trip**. Logs every add/update/prune at info with `user_id`/`partition`/`role`.

### Step 4 — Config docs & example

- `infra/compose/.env.example`: add the four new vars under the existing OIDC block, commented/
  optional, with a one-line "off unless `OIDC_CLAIM_GROUPS` is set" note.
- `docs/oidc.md`: new **"Group → Partition Mapping (optional)"** section — Keycloak group + group-
  membership-mapper setup, the prefix/pattern, the prune flag, and an explicit callout that
  `is_admin` is still **not** claim-writable. Include a short note reconciling the strategy doc's
  old names (Part A) so future readers don't reintroduce `OIDC_ISSUER_URL`/`OIDC_ENABLED`.
- `docs/sso-quickstart.md`: one-line pointer to the new section.

### Step 5 — Tests

- **Unit** — `extract_partition_roles`: prefix strip, pattern match, non-match drop, highest-role
  de-dupe, list-vs-scalar groups claim, empty-claim short-circuit. `OIDCConfig.from_env`: the four
  new vars parse; bad `OIDC_GROUP_PATTERN` (uncompilable / <2 groups) raises at startup; feature
  stays off when `OIDC_CLAIM_GROUPS` unset.
- **Service** — `_sync_oidc_memberships` against a fake membership repo: add, role-change, prune-on
  vs prune-off, unknown-partition (assign raises) is isolated & logged, per-membership failure
  isolation.
- **Regression** — existing `tests/integration/api/test_oidc_lifecycle.py` still passes with groups
  unset (proves zero behavior change).

---

## Open decisions

1. **Reconciliation strategy** — default **additive + role-update; prune behind
   `OIDC_GROUP_SYNC_PRUNE=false`**. Alternative: prune-by-default (matches the strategy doc's
   "Keycloak is source of truth" wording, but wipes manually-granted memberships on next login).
   *Implemented with a mass-revocation guard:* even with prune on, removal runs only when the token
   actually asserts the groups claim — a missing/misconfigured claim skips prune (an explicit
   `groups: []` still prunes). See decision log Phase 15 §2.
2. **`role → is_admin`** — default **excluded** to preserve the documented hard boundary
   (`is_admin` never claim-writable). Approving the alternative adds `OIDC_CLAIM_ROLES` +
   `OIDC_ADMIN_ROLE` (Part A.4 deferred row) behind a loud, explicit opt-in.
3. **Unknown partition referenced by a group** — default **skip + warn** (FK violation caught per
   membership). Alternative: auto-create the partition via `PartitionService` (needs that service
   injected into `AuthService` — a small DI change).

---

## Verification

```bash
uv run pytest tests/unit/ -k "oidc or group or membership"      # new + existing units
uv run pytest tests/integration/api/test_oidc_lifecycle.py      # no regression, groups unset
uv run ruff check openrag/ tests/
python scripts/check_layer_imports.py                           # core/services layering intact
```
End-to-end (manual, against a Keycloak realm): create groups `/openrag/<partition>/<role>`, assign
a test user, log in via `/auth/login`, confirm `GET /users/info` (or `/auth/me`) reflects the
synced memberships; toggle `OIDC_GROUP_SYNC_PRUNE` and re-login to confirm prune behavior.

## Non-goals (explicit)

- No rename of any shipped `OIDC_*` / `AUTH_MODE` variable (Part A only *adopts* shipped names as
  canonical and drops strategy-doc duplicates).
- No `VITE_OIDC_*` / frontend OIDC client, no raw-JWT-bearer dual-auth, no `OIDC_AUDIENCE` /
  `OIDC_JWKS_CACHE_TTL`, no DB migration (uses existing `partition_memberships`).

---

## Key considerations

Two questions a reader coming from the strategy doc will ask — answered here so the divergence from
§15C/§15D/§15E reads as intentional, not as an oversight.

### Why no `OIDCUserMapper` / `OIDCUserProvisioner` classes

The strategy doc's §15C (`OIDCUserMapper`) and §15D (`OIDCUserProvisioner`) are **not absent — they
are decomposed differently**, because the shipped design provisions *once at the callback*, not
*per request*. The doc's classes were sketched for the stateless-JWT-bearer world (§15E
`get_current_user` runs validator → mapper → provisioner on every request), so they are stateless
components hung on the auth hot path. We have no such per-request path — the opaque session token
short-circuits every request after login — so the natural seam is `handle_oidc_callback`, where
steps 1–3 of `ensure_user` already existed before this phase.

`OIDCUserProvisioner.ensure_user`'s 5-step docstring maps **line-for-line** onto the existing
callback pipeline (`auth_service.py:207-210`):

| `ensure_user` step (§15D)             | Where it lives now                         |
| ------------------------------------- | ------------------------------------------ |
| 1. Lookup by `external_user_id`       | `_resolve_user`                            |
| 2. Auto-provision if not found        | `_sync_auto_provisioned`                   |
| 3. Sync `is_admin` from roles         | **deliberately omitted** — Open Decision #2 (hard boundary) |
| 4. Sync partition memberships         | `_sync_oidc_memberships` ← this phase (Part B) |
| 5. Return full user                   | callback continues                         |

Wrapping this in a new class would either duplicate working code (steps 1, 2, 4 already have homes)
or force a second provisioning path. We add only the genuinely-missing step (4).

`OIDCUserMapper.extract_partitions` becomes the **pure function** `extract_partition_roles`
(`services/auth/oidc_groups.py`) rather than a class: it has no state and no I/O, so `self._config`
ceremony buys nothing. A module-level function matches the existing `_parse_oidc_claim_mapping`
style in `core/config/auth.py` and is unit-testable without instantiation.

### No raw-JWT bearer format on the wire — and why we don't need one yet

The strategy doc's §15E dispatches on token *shape* — a bearer starting `eyJ` (a raw Keycloak JWT)
takes the JWKS-validation path; `or-` takes the DB path. **The shipped design never puts a raw JWT
on the wire as a client credential**, so there is no third format to detect:

- The IdP's `id_token` / `access_token` / `refresh_token` are obtained server-side at
  `/auth/callback` and stored **Fernet-encrypted in `oidc_sessions`** — they never reach the
  browser.
- The client holds an **opaque** session token (`secrets.token_urlsafe(32)`, ~43 chars, *not* a
  JWT). A bearer is resolved by DB lookup in `AuthMiddleware` (`auth.py:197-222`): first against
  `oidc_sessions` (opaque session token), then against `users.token` (programmatic `or-` token).

A raw JWT (`eyJ…`) matches neither table → 401/403.

**Right now we don't need it.** The two audiences a JWT-bearer path would serve are already covered:
browsers authenticate via the session cookie, and machine/CI clients via `users.token`. No current
client holds a Keycloak access token it needs to present directly, so adding a third format would be
surface area with no consumer. It stays **out of scope** (Part A.3) until a concrete caller — e.g. a
service-to-service client that *already* obtains an access token from the IdP — requires it.

**Implications if it is implemented later** (this is the §15B `jwt_validator` path). The code is
small — the validation machinery already exists (`oidc_client.py` `_load_jwks` + `_verify_id_token`)
and the user-resolution helpers (`_resolve_user`, `_sync_auto_provisioned`, `_sync_oidc_memberships`)
already take raw `claims`. The cost is **semantic, not lines of code**:

| Concern | Today (opaque session / `users.token`) | With raw-JWT bearer |
| ------- | -------------------------------------- | ------------------- |
| **Validation** | DB lookup on every request | **No DB lookup** — pure offline crypto: JWKS signature + `iss`/`aud`/`exp` claim checks against cached public keys. |
| **Revocation** | Instant — delete the session/token row | **Cannot revoke** a still-valid JWT; it is trusted until its `exp`. Mitigations (short TTL, a denylist) reintroduce the state JWTs were meant to avoid. |
| **Provisioning** | Runs **once** at `/auth/callback` | `_sync_oidc_memberships` would run **per request** unless gated/cached — a write + IdP-shaped round-trip on the hot path. |
| **Audience** | n/a | Keycloak *access* tokens carry `aud = account`/resource, not `client_id`, so `_verify_id_token`'s rule can't be reused — needs a sibling `verify_access_token` plus the `OIDC_AUDIENCE` knob dropped in A.3, and a decision on *which* token (id vs access) clients present. |

Net: bolting it on is cheap mechanically but is a real **policy change** — trading instant
revocation for stateless validation, and accepting per-request provisioning cost. Defer until a
client actually needs it.
