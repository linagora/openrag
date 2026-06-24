# Forward Port Log

Tracks `dev` changes during MODE 2 isolation (Phases 5-9).
Each entry: what changed on `dev`, whether it was forward-ported or deferred
to the cutover re-implementation queue.

> Created retroactively at the start of Phase 5 (2026-04-29). The Phase 0-4
> Mode 1 work merged from `dev` cleanly so this log starts empty.

---

## `main` → `refactor/hexagonal` security forward-port (2026-06-24)

Working branch: `forward-port/main-to-hexagonal` (off `origin/refactor/hexagonal`,
local only). Porting the 68 non-merge commits that landed on `main` after the
merge-base (`c9d53cc0`, 2026-05-21) — mostly a one-shot security-hardening audit
plus loader/OpenAI fixes, deploy hardening, deps and release chores. Each port is
one commit carrying the original subject/body + a `Forward-ported from <hash>`
trailer. Order chosen by the user: package security code first, infra non-root +
docs last.

### Ported

| source (main) | subject | target location(s) |
|---|---|---|
| `c1079d7f` | Ray dashboard → localhost (compose) | `infra/compose/docker-compose.yaml` |
| `8914dfbb` | non-root Ray container (H4) | `infra/docker/ray.Dockerfile` |
| `8914dfbb`+`6860341c`+`26a70dbc`+`c0847d8f` | non-root OpenShift app image (consolidated — later commits rewrote earlier) | `infra/docker/api.Dockerfile` |
| `645128dc` | no prod bind-mount / gate --reload (N8) | `infra/compose/docker-compose.yaml`, `infra/scripts/entrypoint.sh` |
| `0e5687bc` | remove API_NUM_WORKERS footgun | `infra/scripts/entrypoint.sh`, `infra/compose/.env.example`, `infra/charts/.../values.yaml`, `docs/.../env_vars.md` |
| `319bc5cd` | drop API_NUM_WORKERS from doc env assets | `docs/assets/env_example.env`, `env_linux_gpu.env` |
| `c558dd1f` / `d69be1da` | version bump → 1.1.12 / 1.1.13 | `pyproject.toml` |
| `2af1d76f` | harden Ansible deploy (#488) | `infra/ansible/ansible.cfg`, `playbooks/openrag.yml` |
| `34dc3a2f` | Ray dashboard localhost default (C3) | `openrag/api/main.py`, `openrag/api/mcp/server.py`, `infra/cluster.yaml`, `infra/quick_start/docker-compose.yaml`, `docs/assets/compose_ollama_cpu.yaml` |
| `aa015bdd` | external Ray cluster via RAY_ADDRESS | `openrag/api/main.py`, `openrag/api/mcp/server.py`, `.env.example`, docs |
| `0515f705` | require MinIO creds (H3) | `infra/compose/milvus/milvus.yaml`, `infra/quick_start/vdb/milvus.yaml`, `.env.example` |
| `0164b829` | remove weak DB/AUTH defaults (M2/M3) | `conf/config.yaml`, compose stacks, `docs/assets/compose_ollama_cpu.yaml`, `.env.example` |
| `b8002ef0` | drop seccomp:unconfined (N11) | `infra/compose/milvus/milvus.yaml` + `.named-volumes.yaml`, `infra/quick_start/vdb/milvus.yaml` |
| `24efaa66` | Helm DB password → Secret (N7) | `infra/charts/openrag-stack/values.yaml` |
| `c8f2d47f` | default-deny NetworkPolicy (N12) | `infra/charts/.../templates/networkpolicy.yaml` (new), `values.yaml` |
| `5caec50b` | restrict metrics exposure (N9) | `infra/compose/monitoring.docker-compose.yaml` |
| `d7fc3130` | pin image tags (N10) | `infra/charts/.../values.yaml` (openrag-owned → 1.1.13), `infra/compose/monitoring.docker-compose.yaml` (third-party pins). Compose app-image pins skipped (reverted by 64c3e722). |
| `7bc46696` | require ALLOW_NO_AUTH for no-token admin bypass | `openrag/api/middleware/auth.py`, `.env.example`, `tests/unit/api/middleware/test_bypass_config.py` |
| `202433d7` | update_user mass-assignment whitelist | `openrag/core/models/user.py` (extra="ignore"), `openrag/services/orchestrators/user_service.py` (whitelist), tests |
| `edd2c7ce` | external_user_id empty→NULL (#121) | `openrag/core/models/user.py` (validator; covers update path the repo missed) |
| `229503b4` | no session token in UI file URLs (N13) | `openrag/app_front.py` |
| `97c624ef` | back-channel logout exp/jti + replay (M9) | `services/auth/oidc_client.py`, `services/orchestrators/auth_service.py` (+ replay test) |
| `2b34a0d1` | clock-skew leeway + nbf (crypto) | `services/auth/oidc_client.py` |
| `c2fde135` | logout CSRF Fetch-Metadata guard (N3) | `api/routers/auth/oidc.py` (+ tests) |
| `9a73200a` | revoke OIDC sessions on token regen (#361, #486) | `services/orchestrators/{auth_service,user_service}.py` (startup-rotation guard + revoke_by_user already present) |

### Skipped (already present / superseded on refactor)

| source (main) | reason |
|---|---|
| `f9e0c394` | /chainlit path-boundary anchor already in `api/middleware/auth.py` |
| `9b82996c` | azp on multi-aud tokens already in `services/auth/oidc_client.py` |
| `64c3e722` | compose `:latest` tags already the refactor state |
| `f9b8a776` | de-flake `test_cancel_*` superseded by refactor `cca5f415` |
| `47b8cd32` | refactor already fail-fasts on missing CHAINLIT_AUTH_SECRET (stricter, #380/`b63a9825`, with a regression test). 47b8cd32 only re-adds an ALLOW_NO_AUTH-gated default-secret fallback — a loosening intentionally NOT ported. |
| `73acb1c9` | both halves already present: `sanitize_next_url` rejects CR/LF/NUL + the `/\` protocol-relative vector (#360 regression test), and the callback already verifies userinfo.sub == id_token.sub (OIDC Core §5.3.2). |
| `cdb3edc9` | test-only follow-up to M9 on main's `test_oidc_client.py` (no equivalent file); subsumed by the new replay test which carries exp. |

### Remaining (TODO — not yet ported)

**Batch 4 — RAG / retrieval / loaders / OpenAI (~21 commits):**
`714f2a84` streaming finish_reason → `core/utils/source_filtering.py`; `bf4ae134` + `f079efa5` Milvus filter-injection guards → `services/storage/*`, `api/routers/*` + `api/dependencies`; `db92875d` llm_override credential strip → `services/inference/vllm_client.py` + `api/schemas/user/chat.py`; `67ec4199` source-download authz (partial — `/static` mount already gone) → `api/routers/user/source_links.py`; `86c9b51d` ensure_partition_role fail-open → `api/dependencies/auth.py`; `8ecbc781` web-search SSRF/MITM → `services/websearch/content_fetcher.py`; `e3c7eac2` + `81bccf08` control-token neutralizer → `core/utils/` + sources-tag parser; `818d5446` stack-trace leak → `api/routers/admin/indexing.py`; `54165900` token-limit + n/best_of bounds → `api/schemas/user/chat.py` + `api/routers/user/chat.py`; `6bc898e9` surrounding-chunk partition scope → `services/storage/vector_store_searcher.py`; `63a857af` image-URL SSRF → parsers; `8ea723ca` SVG external-fetch guard → image parser; `70a2db36` CustomDocLoader page accumulation (#376) → `services/workers/parsers/legacy_loaders/`; `199424bf` empty-stream 502 (#363) → `api/routers/user/chat.py`; `0bc6157e` stop logging raw query (#481) → `api/routers/user/search.py`; `761f47a0` copy-endpoint source_file_id validation (#477); `221f8ed8` parser DoS caps (M8) → `core/config/indexation.py` + parsers; `d66cf029` cap partitions per user (M13); `52be26f1` non-empty RAG answer → `openrag/prompts/templates/`.

**Batch 2 — deps:** `74de8232` Starlette/FastAPI bump, `4d8bca01` `limits` not slowapi, `0e6e7836` rate-limit module + tests (`pyproject.toml`, `uv.lock`, new `openrag/...rate_limit`, `.env.example`).

**Deferred (user-requested, do last):** `4bbefd41` compose non-root rework (init-perms + per-service `user:` — needs careful adaptation to `infra/compose/` paths), `701fcf9e` + `8849fe7d` docs moves, `563907ad` doc comment trim, plus a final `ruff format`/`check` pass.

> Note: the suite shows one pre-existing **false-alarm** failure,
> `test_seed_defaults_preserves_endpoint_api_keys` — `seed_defaults` reads
> `os.getenv("API_KEY", ...)` and this worktree is nested under the main checkout,
> so `load_dotenv()` walks up and picks up the real `.env`. Not caused by this
> port (passes from a checkout outside the main tree). Everything else: 1317 pass.

---

## Forward-ported (critical)

_(legacy template section — superseded by the dated section above.)_

(none recorded under the original Phase 5–9 process)
