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
| `714f2a84` | streaming finish_reason not on content chunk | `core/utils/source_filtering.py` (+ test) |
| `0bc6157e` | stop logging raw query text (#481) | `api/routers/user/search.py` (query_len) |
| `52be26f1` | non-empty RAG answer body | `openrag/prompts/templates/sys_prompt_tmpl.txt` |
| `6bc898e9` | surrounding-chunk partition scope (N6) | `services/storage/vector_store_searcher.py` (+ tests) |
| `86c9b51d` | ensure_partition_role fail-open → 404 | `api/dependencies/auth.py` (+ tests) |
| `bf4ae134` | Milvus filter scope-escape via precedence | `services/storage/milvus_store.py` `_build_filter_expr` (paren-wrap multi-part) (+ tests) |
| `f079efa5` | validate file_id / partition allowlist | `core/indexing/validators.py`, `api/dependencies/auth.py`, `partition_service.py`, `api/routers/{user/search,admin/partitions}.py` (+ tests). Defense-in-depth atop `_format_value` escaping. |
| `db92875d` | strip client llm_override endpoint/creds | `services/inference/vllm_client.py` `_resolve_overrides`, `api/schemas/user/chat.py` (+ tests) |
| `e3c7eac2` + `81bccf08` | control-token neutralizer (H8, #487) | `core/utils/text.py` `neutralize_prompt_control_tokens`, `core/prompts/chat_prompt_builder.py` (+ tests) |
| `818d5446` | stop leaking stack traces / FS paths (M7) | `api/routers/admin/indexing.py` (generic save error; admin-gated traceback) |
| `54165900` | token limit in RAG mode + bound n/best_of (M12) | `api/routers/user/chat.py`, `api/schemas/user/chat.py` (+ tests) |
| `d66cf029` | cap partitions per non-admin user (M13) | `services/orchestrators/partition_service.py`, `api/routers/admin/partitions.py`, `.env.example` (+ tests) |
| `8ecbc781` | web-search SSRF/MITM deltas (verify_ssl default True + DNS-resolution guard hook) | `services/websearch/content_fetcher.py` (+ tests). The refactor already had per-hop redirect revalidation (#383). |
| `8ea723ca` | explicit cairosvg `unsafe=False` (SSRF/XXE) | `core/indexing/parsers/image_parser.py` |
| `221f8ed8` | EML attachment fan-out cap (M8) | `core/indexing/parsers/eml_parser.py` (+ test). eml-depth already bounded by the dispatcher; docx/pptx/pdf caps N/A (docling/marker delegate). |
| `67ec4199` | authorize source-file downloads by partition | new `api/routers/user/download.py` (replaces the open `/static` mount), `api/main.py`, `chat.py`/`source_links.py` rekeyed to chunk id (+ tests) |
| `761f47a0` | validate copy-endpoint source_file_id (#477) | `api/routers/admin/indexing.py` + `docs/assets/compose_linux_gpu.yaml` |
| `74de8232` | Starlette>=0.47.2 / FastAPI>=0.116.1 (CVE-2025-54121) | `pyproject.toml` + `uv.lock` regen (starlette 0.46.2->0.47.3, fastapi->0.116.2, chainlit->2.11.1) |
| `0e6e7836` + `4d8bca01` | path-tiered rate limiting (M6) | new `api/middleware/rate_limit.py` (registered before AuthMiddleware), `limits>=3.6` dep, `.env.example`, API-test compose disable (+ tests). Refactor never had slowapi, so the 4d8bca01 swap is folded in. |

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
| `199424bf` | empty-stream 502 (#363): obviated by the refactor architecture — non-streaming uses `self._llm.chat()`/`.generate()` (materialized dicts, not a stream's first chunk), and the inference client already raises `InferenceError(status_code=502)` on invalid upstream responses. No `__anext__`/`StopAsyncIteration` path remains. |
| `70a2db36` | CustomDocLoader page accumulation (#376): obviated — the parser-shim removal deleted `CustomDocLoader`; `.doc` now goes through the docling/marker workers, which produce whole-document markdown and have no single-page-overwrite bug. |
| `63a857af` | image-URL SSRF on captioning (H2): obviated — the refactor captions extracted image *bytes* (`vlm.caption_image(image_bytes)`); a document's remote `![](http://…)` becomes an `ImageBlock(source_url=…)` with empty bytes that nothing fetches, and the URL is never forwarded to the VLM. No SSRF gadget. (`image_captioning_url` is a dormant unread knob.) The SVG sub-fix is ported separately as `8ea723ca`. |

### Remaining (TODO — not yet ported)

**Batches 1 (infra core), 2 (deps), 3 (auth/OIDC), 4 (RAG/retrieval): ✅ COMPLETE.**
All security-relevant package code and dependencies are ported or obviated.

**Deferred (user-requested, do last — the ONLY remaining work):**
- `4bbefd41` compose non-root rework (init-perms bootstrap container + per-service `user:` mappings — needs careful adaptation to the `infra/compose/` volume paths)
- `701fcf9e` docs: Chainlit on CHAINLIT_PORT under Ray Serve
- `8849fe7d` docs: move OIDC/SSO quick-start guides into the docs site
- `563907ad` doc comment trim, plus a final `ruff format`/`check` pass.

> Note: the suite shows one pre-existing **false-alarm** failure,
> `test_seed_defaults_preserves_endpoint_api_keys` — `seed_defaults` reads
> `os.getenv("API_KEY", ...)` and this worktree is nested under the main checkout,
> so `load_dotenv()` walks up and picks up the real `.env`. Not caused by this
> port (passes from a checkout outside the main tree). Everything else: 1317 pass.

---

## Forward-ported (critical)

_(legacy template section — superseded by the dated section above.)_

(none recorded under the original Phase 5–9 process)
