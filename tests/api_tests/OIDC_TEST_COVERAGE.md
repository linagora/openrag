# OIDC Test Coverage Matrix

Maps every acceptance criterion from `.omc/plans/oidc-auth/plan.md` §7 to the
test(s) that cover it.

| AC | Description | Test file : function |
|----|-------------|----------------------|
| AC1 | `AUTH_MODE=token` behaviour strictly unchanged | `openrag/components/auth/test_middleware.py::TestTokenModeLegacy::test_bearer_valid_returns_200`, `::test_bearer_invalid_returns_403`, `::test_missing_token_returns_403` |
| AC2 | `AUTH_MODE=oidc` — UI path without cookie → 302 to `/auth/login` | `openrag/components/auth/test_middleware.py::TestOIDCMode::test_no_creds_root_path_returns_302`, `::test_no_creds_static_path_returns_302` |
| AC3 | `AUTH_MODE=oidc` — API path without cookie/Bearer → 401 JSON | `openrag/components/auth/test_middleware.py::TestOIDCMode::test_no_creds_api_path_returns_401`, `::test_no_creds_v1_chat_returns_401` |
| AC4 | `GET /auth/login` → 302 with `state`, `nonce`, `code_challenge` (PKCE S256), `scope openid email` | `openrag/routers/test_auth_router.py::test_login_redirects_to_idp_with_pkce`, `tests/api_tests/test_oidc_lifecycle.py::test_full_oidc_lifecycle` (step 1) |
| AC5 | `GET /auth/callback` with valid code+state → sets `openrag_session` cookie + 302 to `next_url` | `openrag/routers/test_auth_router.py::test_callback_success_by_external_id`, `tests/api_tests/test_oidc_lifecycle.py::test_full_oidc_lifecycle` (step 3) |
| AC6 | Callback with unknown email AND unknown sub → 403, no session created | `openrag/routers/test_auth_router.py::test_callback_user_not_registered` |
| AC6b | Callback: match by sub directly (user already linked), no email fallback | `openrag/routers/test_auth_router.py::test_callback_success_by_external_id` |
| AC6c | Callback: match by email, backfill `external_user_id=sub`, session created | `openrag/routers/test_auth_router.py::test_callback_backfills_external_user_id`, `tests/api_tests/test_oidc_lifecycle.py::test_full_oidc_lifecycle` (steps 3–4) |
| AC6d | Callback: `external_user_id` already set to different sub → 403 conflict | `openrag/routers/test_auth_router.py::test_callback_external_id_mismatch` |
| AC7 | Callback with invalid/mismatched `state` → 400 | `openrag/routers/test_auth_router.py::test_callback_state_mismatch`, `::test_callback_missing_state_cookie` |
| AC8 | Callback with nonce mismatch in ID token → 400 | `openrag/components/auth/test_oidc_client.py` (exchange_code nonce validation tests) |
| AC9 | Request with valid session cookie → `request.state.user` populated, normal flow | `openrag/components/auth/test_middleware.py::TestOIDCMode::test_cookie_valid_and_access_token_fresh_no_refresh`, `tests/api_tests/test_oidc_lifecycle.py::test_full_oidc_lifecycle` (step 5) |
| AC10 | Cookie with near-expiry access_token + refresh_token → transparent refresh, new tokens in DB | `openrag/components/auth/test_middleware.py::TestOIDCMode::test_cookie_near_expiry_triggers_refresh`, `::TestRefreshHelper::test_no_refresh_when_token_fresh` |
| AC11 | Cookie with expired access_token, no refresh_token → session revoked, 302 to `/auth/login` | `openrag/components/auth/test_middleware.py::TestOIDCMode::test_cookie_refresh_fails_session_revoked_and_302`, `::TestRefreshHelper::test_expired_no_refresh_token_returns_none` |
| AC12 | `POST /auth/backchannel-logout` with valid signed `logout_token` (sid match) → 200, sessions revoked | `openrag/routers/test_auth_router.py::test_backchannel_logout_revokes_by_sid`, `tests/api_tests/test_oidc_lifecycle.py::test_full_oidc_lifecycle` (step 6) |
| AC13 | `POST /auth/backchannel-logout` with invalid signature → 400, no revocation | `openrag/routers/test_auth_router.py::test_backchannel_logout_rejects_invalid_token` |
| AC14 | After backchannel-logout, old cookie rejected (401/302) | `tests/api_tests/test_oidc_lifecycle.py::test_full_oidc_lifecycle` (step 7) |
| AC15 | Bearer `users.token` accepted in `AUTH_MODE=oidc` for programmatic access | `openrag/components/auth/test_middleware.py::TestOIDCMode::test_bearer_fallback_accepted_in_oidc_mode` |
| AC16 | `access_token` and `refresh_token` stored encrypted (Fernet), unreadable without key | `openrag/components/auth/test_session_tokens.py::TestEncryptDecrypt::test_round_trip`, `::test_wrong_key_raises_value_error` |
| AC17 | Alembic migration upgrade/downgrade idempotent | Manual: `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` |
| AC18 | `OIDC_TOKEN_ENCRYPTION_KEY` missing in oidc mode → clear startup error | `openrag/components/auth/test_oidc_client.py` (startup/config validation tests) |
