# SSO Quick Start (OIDC)

Configure OpenRag to delegate authentication to your corporate SSO (LemonLDAP::NG, Keycloak, Auth0, Azure AD, Okta…) in five steps.

> **New to OIDC?** You just need to coordinate with your SSO admin and set six `.env` variables. No code to change.

---

## Step 1 — Ask your SSO admin to register a client

Give them the following information.

> ⚠ **Point the IdP at the _front door_ that serves your UI AND reaches the backend's `/auth/callback`.**
> The OIDC callback and back-channel-logout endpoints are hosted **only** on the backend (the container running `openrag/api.py`). So the redirect target must either *be* the backend, or be a **reverse proxy that forwards `/auth/*` to it**:
>
> - **Bundled admin-ui (nginx)** or any reverse proxy → use **its** host/port. It serves the UI *and* proxies `/auth/callback` to the backend, so it is correct — and it's where you must land after login (the post-login redirect is relative). For the admin-ui that's `http://<host>:<ADMIN_UI_PORT>/auth/callback`.
> - **Backend directly** (e.g. it serves the Chainlit UI on `APP_PORT`) → use the backend host + `APP_PORT`.
> - **A _static_ UI front with no proxy** (a bare SPA) → ❌ **never.** It has no `/auth/callback`, so you get an **infinite redirect loop** (404 → "not authenticated" → new OIDC flow → back to the front → …).
>
> Common pitfall: pointing at the **bare backend port** when a separate proxy serves your UI. Login *completes* (the backend has the callback) but the relative post-login redirect drops you on the backend port, which doesn't serve the UI — a **silent** landing on a blank page. Match the redirect URI to the origin that serves the UI.

| Field                         | Value to give                                                  |
| ----------------------------- | -------------------------------------------------------------- |
| **Client type**               | `confidential` (server-to-server token exchange)               |
| **Grant type**                | `authorization_code`                                           |
| **Response type**             | `code`                                                         |
| **Valid redirect URIs**       | `<front-door-host>/auth/callback` _(the UI's proxy or the backend — see warning)_ |
| **Back-channel logout URI**   | `<front-door-host>/auth/backchannel-logout` _(same origin as above)_ |
| **Post-logout redirect URIs** | an optional URL **outside** OpenRag (see §Step 4 below)        |
| **Allowed scopes**            | `openid`, `email`, `profile`, `offline_access`                 |
| **Include `sid` in tokens**   | ✅ enabled (required for back-channel logout)                  |
| **Send refresh token**        | ✅ enabled (so the session doesn't drop every few minutes)     |

Then ask the admin for **three pieces of information**:

1. **`client_id`** — a public identifier, typically `openrag` or similar.
2. **`client_secret`** — a long random string, shown **only once** by most IdPs. Store it in a password manager.
3. **The IdP issuer URL** — e.g. `https://sso.mycorp.com/` or `https://keycloak.mycorp.com/realms/mycorp`.

---

## Step 2 — Verify the **exact** issuer string

This is the most common setup mistake. The issuer value you put in `.env` **MUST** match byte-for-byte what the IdP's discovery document advertises (including trailing slash, per OIDC Core §2). Keycloak usually has no trailing slash; LemonLDAP::NG and Auth0 usually have one.

Run this command against your IdP:

```bash
curl -s https://sso.mycorp.com/.well-known/openid-configuration | jq -r .issuer
```

Copy the output verbatim. If you get:

- `https://sso.mycorp.com/` → use that with the slash.
- `https://keycloak.mycorp.com/realms/mycorp` → use that without a slash.

Any mismatch and OpenRag refuses the login with `Issuer mismatch` in the logs.

---

## Step 3 — Generate a Fernet encryption key

Access tokens and refresh tokens returned by the IdP are stored encrypted at rest. Generate a dedicated key for your deployment:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Output example: `XFlT-ZfXkdqf0v-5Z8kVt9xhU6c7Z4z0ZY8Z4Z4Z4=` (44 chars, url-safe base64).

Store this in your secrets manager — **losing it invalidates every stored session**.

---

## Step 4 — Populate `.env`

Copy this block at the end of your `.env` and fill in your values:

```env
# Switch OpenRag from the legacy Bearer-token login to OIDC. REQUIRED.
AUTH_MODE=oidc

# Issuer URL — EXACT match with the curl|jq output from Step 2.
OIDC_ENDPOINT=https://sso.mycorp.com/

OIDC_CLIENT_ID=openrag
OIDC_CLIENT_SECRET=change-me-the-secret-from-step-1

# Must match EXACTLY the "Valid redirect URI" registered in Step 1.
OIDC_REDIRECT_URI=https://rag.mycorp.com/auth/callback

# From Step 3.
OIDC_TOKEN_ENCRYPTION_KEY=XFlT-ZfXkdqf0v-5Z8kVt9xhU6c7Z4z0ZY8Z4Z4Z4=

# --- Optional ---
# OIDC_SCOPES="openid email profile offline_access"   # default
# OIDC_CLAIM_SOURCE=id_token                          # default ; alternative: userinfo
# OIDC_CLAIM_MAPPING=                                 # default: empty (no sync of display_name/email from IdP)
# OIDC_AUTO_PROVISION_LOGIN=false                     # default ; true = create users on first login from claims (see Step 5)

# ⚠ Where the IdP sends the user AFTER logging out.
#   A ("/") lands on the OpenRag root, which immediately re-triggers
#   OIDC login — if the IdP session is still alive you appear to be
#   re-logged-in instantly (no apparent "logout" effect); if it was killed
#   you land back on the IdP form in a loop. Prefer a URL OUTSIDE OpenRag
#   or nothing to let SSO doing its job:
#     - your corporate intranet / landing page
#     - a static "you are logged out" page you control
#     - the IdP's own post-logout URL (e.g. https://sso.mycorp.com/)
# OIDC_POST_LOGOUT_REDIRECT_URI=https://intranet.mycorp.com/
```

> **Tip — values with spaces (like `OIDC_SCOPES`)**: quote them to stay safe across dotenv parsers:
> `OIDC_SCOPES="openid email profile offline_access"`.
> Quotes are stripped on read.

### Optional: sync `display_name` / `email` from the IdP

By default, OpenRag never modifies a user's `display_name` or `email` after login. If you want the IdP to be the source of truth (useful when HR changes a user's name), set:

```env
OIDC_CLAIM_MAPPING=display_name:name,email:email
```

Each pair is `db_field:oidc_claim`. Only `display_name` and `email` are writable — `is_admin`, `external_user_id`, `file_quota`, and `token` can **never** be changed via the IdP.

By default OpenRag reads the claims from the verified ID token (`OIDC_CLAIM_SOURCE=id_token`, no extra HTTP call). Switch to `userinfo` if your IdP only exposes certain claims via the `/userinfo` endpoint.

### Optional: grant partition access from IdP groups

If your IdP models access as groups (e.g. Keycloak groups like `/openrag/project-alpha/editor`),
OpenRag can map them to partition memberships automatically on every login — set
`OIDC_CLAIM_GROUPS` and friends. See `docs/oidc.md` →
[Group → Partition Mapping](./oidc.md#group--partition-mapping-optional). (`is_admin` is never
derived from groups.)

---

## Step 5 — Pre-provision users

By default, OpenRag **does not auto-create users** on first login. Each user must exist in the database with their OIDC `sub` stored in `external_user_id`.

> **Skip this step entirely** by setting `OIDC_AUTO_PROVISION_LOGIN=true` in your `.env`. The callback then creates a non-admin user from the ID-token claims on first login and keeps `display_name` + `email` in sync with the IdP on every subsequent login. The trade-off: your IdP's user list becomes the source of truth for OpenRag accounts. See `docs/oidc.md` → [Auto-provisioning](./oidc.md#auto-provisioning-optional) for the full trust-model.

Ask the IdP admin for each user's `sub` claim value (stable identifier, NOT the username). Then create the user via the OpenRag admin API — you'll need an admin `AUTH_TOKEN` for this:

```bash
# Boot once with AUTH_MODE=token and AUTH_TOKEN=sk-... to create users,
# OR keep AUTH_TOKEN in .env alongside AUTH_MODE=oidc — in that mode the
# bearer is still accepted for programmatic admin calls.

curl -X POST https://rag.mycorp.com/users/ \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "display_name": "Alice Cooper",
    "external_user_id": "alice@mycorp.com",
    "email": "alice@mycorp.com",
    "is_admin": false
  }'
```

- `external_user_id` **must equal** the user's OIDC `sub`. If you don't know it, ask the admin to check with a test login (the `sub` is the `.sub` claim in the ID token).
- `email` is optional metadata; not used for matching.
- `is_admin: true` grants full admin rights inside OpenRag.

If a user tries to log in and their `sub` isn't pre-provisioned, OpenRag returns `403 User not registered` and logs the `sub` so you can complete provisioning.

---

## Step 6 — Start and test

```bash
docker compose up --build -d
# Watch the startup logs for "OIDC authentication mode enabled"
docker compose logs openrag --tail 50 | grep -i OIDC
```

Open your browser at `https://rag.mycorp.com/` → it redirects to your SSO → you log in → you come back authenticated.

If something goes wrong, see the full **[troubleshooting section in `docs/oidc.md`](./oidc.md#troubleshooting)**. Most issues fall into one of three categories:

1. **Issuer mismatch** (Step 2 — trailing slash).
2. **Invalid redirect URI** (Step 1 — must match byte-for-byte).
3. **User not registered** (Step 5 — `external_user_id` ≠ `sub`).

---

## Appendix — Programmatic access in SSO mode

Once `AUTH_MODE=oidc`, human users go through SSO. **CI pipelines, scripts, and external agents** keep working by using the per-user bearer token (`users.token`) — the same one returned at `POST /users/` creation. Example:

```bash
# The token printed when you created alice in Step 5:
curl -H "Authorization: Bearer or-xxxxxxxxxxxxxxxx" https://rag.mycorp.com/v1/models
```

This gives you the best of both worlds: human-friendly SSO for the UI, token-based auth for automation.
