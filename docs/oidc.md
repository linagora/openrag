# OpenID Connect (OIDC) Authentication Guide

This guide walks you through configuring and using OpenRag's OIDC authentication mode.

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Configuration](#configuration)
4. [User Pre-provisioning](#user-pre-provisioning)
5. [Keycloak Setup](#keycloak-setup)
6. [LemonLDAP::NG Setup](#lemonldapng-setup)
7. [Programmatic Access](#programmatic-access)
8. [Back-Channel Logout](#back-channel-logout)
9. [Troubleshooting](#troubleshooting)
10. [Security Considerations](#security-considerations)

---

## Overview

OpenRag supports two authentication modes:

- **Token Mode** (`AUTH_MODE=token`, default): Traditional Bearer token authentication. Suitable for development and programmatic access.
- **OIDC Mode** (`AUTH_MODE=oidc`): OpenID Connect Authorization Code + PKCE flow. Users authenticate via an external identity provider (IdP), and the UI receives a browser-managed session cookie.

### When to Use OIDC

Use OIDC when:
- Your organization uses a centralized identity provider (Keycloak, Azure AD, Okta, LemonLDAP::NG, etc.)
- You want users to authenticate through a familiar corporate login
- You need federated identity and single sign-on (SSO) across multiple systems
- You want to leverage existing user directories (LDAP, Active Directory, SAML)

Use Token Mode when:
- Running in development or testing
- Your application is purely backend/programmatic
- You prefer simplicity without external dependencies
- You're integrating with headless tools (CI/CD, SDKs, etc.)

---

## Architecture

### Authentication Flow Diagram

```
Browser              OpenRag           IdP (Keycloak)
   |                   |                    |
   |-- GET /chainlit -->|                   |
   |<- 302 /auth/login  |                   |
   |-- GET /auth/login->|                   |
   |                    | [gen state/nonce/PKCE,
   |                    |  pose cookie (5 min)]
   |<-302 authorize-----|                   |
   |----------- authorize?client_id=&state=&code_challenge=-------->|
   |<------ login form --------------------------------|
   |------- username/password ----------------------->|
   |<------ 302 /auth/callback?code=&state=----------|
   |-- GET /auth/callback->|                   |
   |                    |---token exchange --->|
   |                    |<-- id_token, access_token, refresh_token --|
   |                    | [verify signature, nonce, claims;
   |                    |  extract email; match user;
   |                    |  create oidc_sessions row;
   |                    |  set openrag_session cookie]
   |<--302 next_url-----|                   |
   |-- GET /chainlit --->|                   |
   |                    | [session OK]
   |<-- 200 Chainlit UI--|                   |
```

### Key Components

**`openrag/components/auth/` package:**
- `oidc_client.py` — Authlib-based OIDC client (discovery, JWKS, token exchange, verification)
- `session_tokens.py` — Session token generation and Fernet encryption/decryption
- `state_cookie.py` — Temporary cookie (5 min TTL) for transporting `state`, `nonce`, `code_verifier`
- `middleware.py` — Modified `AuthMiddleware` supporting cookie + bearer + lazy refresh
- `refresh.py` — Lazy access token refresh logic

**`openrag/routers/auth.py`:**
- `/auth/login` — Start Authorization Code + PKCE flow
- `/auth/callback` — Handle IdP redirect, create session
- `/auth/backchannel-logout` — IdP-driven revocation (OIDC spec)
- `/auth/logout` — RP-initiated logout (local + IdP)
- `/auth/me` — Debug endpoint (returns current user + session expiry)

**Database:**
- New `oidc_sessions` table: stores encrypted IdP tokens, session metadata, revocation status
- New `email` column on `users` table: unique index for matching users by email claim

---

## Configuration

### Environment Variables

All variables must be set when `AUTH_MODE=oidc`. If any required variable is missing, the application refuses to start.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AUTH_MODE` | No | `token` | Set to `oidc` to enable OIDC authentication |
| `OIDC_ENDPOINT` | Yes* | — | Issuer URL (auto-discovery via `/.well-known/openid-configuration`) |
| `OIDC_CLIENT_ID` | Yes* | — | Client ID registered at the IdP |
| `OIDC_CLIENT_SECRET` | Yes* | — | Client secret (confidential clients only) |
| `OIDC_REDIRECT_URI` | Yes* | — | Callback URL, must match IdP configuration (e.g., `https://openrag.example.com/auth/callback`) |
| `OIDC_TOKEN_ENCRYPTION_KEY` | Yes* | — | Fernet key for encrypting tokens at rest (see [Generating the Fernet Key](#generating-the-fernet-key)) |
| `OIDC_EMAIL_SOURCE` | No | `id_token` | Where to extract the `email` claim: `id_token` (from JWT) or `userinfo` (from `/userinfo` endpoint) |
| `OIDC_SCOPES` | No | `openid email profile offline_access` | Space-separated OIDC scopes; include `offline_access` for refresh tokens |
| `OIDC_POST_LOGOUT_REDIRECT_URI` | No | `/` | URL to redirect to after RP-initiated logout |
| `OIDC_ALLOWED_EMAIL_DOMAINS` | No | — | Optional CSV list of email domain whitelist (e.g., `example.com,partner.org`) |

\* Required when `AUTH_MODE=oidc`

### Generating the Fernet Key

Generate a cryptographically secure key for token encryption:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Output example:
```
XFlT-ZfXkdqf0v-5Z8kVt9xhU6c7Z4z0ZY8Z4Z4Z4=
```

Store this key securely (e.g., in a secrets manager). Never commit it to version control.

### Example Configuration

Create a `.env` file (or set these environment variables):

```bash
# Token mode (default)
AUTH_MODE=token
AUTH_TOKEN=sk-or-change-me

# OR, OIDC mode
AUTH_MODE=oidc

# OIDC configuration (Keycloak example)
OIDC_ENDPOINT=https://idp.example.com/realms/openrag
OIDC_CLIENT_ID=openrag
OIDC_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxx
OIDC_REDIRECT_URI=https://openrag.example.com/auth/callback
OIDC_TOKEN_ENCRYPTION_KEY=XFlT-ZfXkdqf0v-5Z8kVt9xhU6c7Z4z0ZY8Z4Z4Z4=
OIDC_EMAIL_SOURCE=id_token
OIDC_SCOPES=openid email profile offline_access
OIDC_POST_LOGOUT_REDIRECT_URI=/
# OIDC_ALLOWED_EMAIL_DOMAINS=example.com,partner.org
```

---

## User Pre-provisioning

OIDC requires users to be pre-provisioned in OpenRag's database. There is **no automatic user creation** on login.

### Admin Pre-provisioning

Admins must create users with email matching the OIDC `email` claim **before** users attempt to log in.

**Create a user via API:**

```bash
curl -X POST http://localhost:8080/users/ \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "display_name": "Alice Cooper",
    "email": "alice@example.com",
    "is_admin": false
  }'
```

**Response** (returns the token once):
```json
{
  "id": 42,
  "display_name": "Alice Cooper",
  "email": "alice@example.com",
  "external_user_id": null,
  "is_admin": false,
  "token": "or-xxxxxxxxxxxxxxxxxxxxxxxx"
}
```

**Pre-fill `external_user_id` (optional):**

If you know the user's OIDC `sub` claim in advance (e.g., from a Keycloak export or LDAP directory), you can set `external_user_id` directly:

```bash
curl -X POST http://localhost:8080/users/ \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "display_name": "Bob Smith",
    "email": "bob@example.com",
    "external_user_id": "550e8400-e29b-41d4-a716-446655440000",
    "is_admin": false
  }'
```

This skips the email-matching step on first login; the OIDC `sub` claim is checked directly.

### Bulk User Provisioning

For bulk imports, write a script that calls `/users/` in a loop:

```python
import requests
import json

auth_token = "sk-your-token"
headers = {
    "Authorization": f"Bearer {auth_token}",
    "Content-Type": "application/json"
}

users = [
    {"display_name": "Alice", "email": "alice@example.com"},
    {"display_name": "Bob", "email": "bob@example.com"},
    {"display_name": "Charlie", "email": "charlie@example.com"},
]

base_url = "http://localhost:8080"

for user in users:
    response = requests.post(f"{base_url}/users/", headers=headers, json=user)
    print(f"Created {user['display_name']}: {response.json()}")
```

---

## Keycloak Setup

[Keycloak](https://www.keycloak.org/) is a popular open-source identity provider. This section walks through a typical Keycloak configuration.

### Prerequisites

- Keycloak 20+ (or latest)
- Network connectivity between OpenRag and Keycloak
- Admin access to Keycloak

### Step 1: Create a Realm

1. Log into Keycloak Admin Console (e.g., `http://localhost:8081/admin`)
2. Click **Create Realm** (top left)
3. Enter realm name: `openrag`
4. Click **Create**

### Step 2: Create a Client (Confidential)

1. In the realm `openrag`, navigate to **Clients** (left sidebar)
2. Click **Create client**
3. Set **Client ID**: `openrag`
4. Leave **Client Type**: `OpenID Connect`
5. Click **Next**
6. Enable:
   - **Client authentication**: ON (confidential client)
   - **Authorization**: ON
7. Click **Next**
8. **Valid redirect URIs**: Add `https://openrag.example.com/auth/callback` (replace with your actual URL)
9. **Valid post logout redirect URIs**: Add `https://openrag.example.com/` (or your home page)
10. **Backchannel logout URL**: Add `https://openrag.example.com/auth/backchannel-logout`
11. **Backchannel logout session required**: ON
12. Click **Save**

### Step 3: Configure Scopes

1. Navigate to **Clients** → `openrag` → **Client scopes**
2. Ensure these default scopes are assigned:
   - `email` — includes email claim
   - `profile` — includes name claims
   - `offline_access` — allows refresh tokens
3. Click **Save**

### Step 4: Get Client Credentials

1. Navigate to **Clients** → `openrag` → **Credentials** tab
2. Copy **Client secret**
3. Use this in `OIDC_CLIENT_SECRET` env var

### Step 5: Configure OpenRag

In `.env`:

```bash
AUTH_MODE=oidc
OIDC_ENDPOINT=http://keycloak.example.com/realms/openrag
OIDC_CLIENT_ID=openrag
OIDC_CLIENT_SECRET=<paste-secret-from-step-4>
OIDC_REDIRECT_URI=https://openrag.example.com/auth/callback
OIDC_TOKEN_ENCRYPTION_KEY=<generate-via-python-script>
OIDC_EMAIL_SOURCE=id_token
OIDC_SCOPES=openid email profile offline_access
```

### Step 6: Create Test User in Keycloak

1. Navigate to **Users** (left sidebar)
2. Click **Create new user**
3. **Username**: `testuser`
4. **Email**: `testuser@example.com`
5. **First name**: `Test`
6. **Last name**: `User`
7. Click **Create**
8. Go to **Credentials** tab, set a password for testing
9. Ensure **Temporary** is OFF (so user can log in immediately)

### Step 7: Pre-provision User in OpenRag

```bash
curl -X POST http://localhost:8080/users/ \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "display_name": "Test User",
    "email": "testuser@example.com",
    "is_admin": false
  }'
```

### Step 8: Test the Flow

1. Navigate to `http://localhost:8080/chainlit` (or your app URL)
2. Expect: 302 redirect to `/auth/login`
3. Expect: 302 redirect to Keycloak (`http://keycloak.example.com/realms/openrag/protocol/openid-connect/auth?...`)
4. Log in with `testuser` / `<password>`
5. Expect: 302 redirect back to `/auth/callback?code=...`
6. Expect: 302 redirect to `/chainlit` or `next_url`
7. Should be authenticated

**Troubleshooting Keycloak**:

- **"Invalid redirect URI"**: Ensure `OIDC_REDIRECT_URI` exactly matches what's configured in Keycloak **Clients** → **Valid redirect URIs**.
- **"Client secret mismatch"**: Copy the secret again from **Credentials** tab.
- **"Invalid scope"**: Ensure `email` and `offline_access` are in the client's scope mappings.

---

## LemonLDAP::NG Setup

[LemonLDAP::NG](https://www.lemonldap-ng.org/) is another OIDC provider, often used in enterprise environments with LDAP/AD integration.

### Configuration Mapping

LemonLDAP::NG uses different terminology. Map these OpenRag variables to LLNG config:

| OpenRag Variable | LLNG Parameter | Example |
|---|---|---|
| `OIDC_ENDPOINT` | `OIDCServiceMetaDataIssuer` | `https://llng.example.com` |
| `OIDC_CLIENT_ID` | `OIDCServiceMetaDataClientID` | `openrag` |
| `OIDC_CLIENT_SECRET` | `OIDCServiceMetaDataClientSecret` | (from LLNG admin) |
| `OIDC_REDIRECT_URI` | `OIDCServiceMetaDataRedirectUris` | `https://openrag.example.com/auth/callback` |

### Steps

1. **Create an OIDC relying party in LLNG admin**:
   - Go to LLNG admin console
   - Navigate to **Applications** → **OpenID Connect Relying Parties**
   - Create a new relying party named `openrag`
   - Set **Client ID**: `openrag`
   - Set **Redirect URI**: `https://openrag.example.com/auth/callback`
   - Set **Post-logout URI**: `https://openrag.example.com/`
   - Generate/retrieve the client secret

2. **Configure OpenRag .env**:

```bash
AUTH_MODE=oidc
OIDC_ENDPOINT=https://llng.example.com
OIDC_CLIENT_ID=openrag
OIDC_CLIENT_SECRET=<secret-from-llng>
OIDC_REDIRECT_URI=https://openrag.example.com/auth/callback
OIDC_TOKEN_ENCRYPTION_KEY=<generate-via-python>
OIDC_EMAIL_SOURCE=id_token
OIDC_SCOPES=openid email profile offline_access
```

3. **Pre-provision users** (via `/users/` API, same as Keycloak)

4. **Test**: Navigate to OpenRag, should redirect to LLNG login

For LLNG-specific questions, consult the [LLNG documentation](https://www.lemonldap-ng.org/documentation).

---

## Programmatic Access

Even in OIDC mode, **Bearer token authentication is still supported** for programmatic access (CI/CD, SDKs, scripts, tests).

### Using `users.token` in OIDC Mode

Each user has a `users.token` column (same as token mode). Clients can use this for programmatic access:

```bash
curl -H "Authorization: Bearer or-xxxxxxxxxxxxxxxxxxxxxxxx" \
  http://openrag.example.com/v1/models
```

This bypasses the OIDC/session cookie flow entirely, suitable for:
- CI/CD pipelines uploading documents
- Python/JavaScript SDK clients
- Test automation
- Headless microservices

### Getting a User's API Token

Admin retrieves a user's token:

```bash
curl -X GET http://localhost:8080/users/42 \
  -H "Authorization: Bearer ${AUTH_TOKEN}"
```

**Note**: The token is hidden in normal responses (security). To get it, use:

```bash
curl -X POST http://localhost:8080/users/42/regenerate_token \
  -H "Authorization: Bearer ${AUTH_TOKEN}"
```

This returns a fresh token (old one invalidated).

### Example: Upload Documents via CI/CD

```bash
#!/bin/bash
OPENRAG_URL="https://openrag.example.com"
API_TOKEN="or-xxxxxxxxxxxxxxxxxxxxxxxx"
PARTITION_NAME="documents"

curl -X POST "${OPENRAG_URL}/indexer/add_file" \
  -H "Authorization: Bearer ${API_TOKEN}" \
  -F "file=@document.pdf" \
  -F "partition=${PARTITION_NAME}"
```

---

## Back-Channel Logout

Back-channel logout is part of the OIDC standard. When a user logs out from the IdP or an admin terminates their session, the IdP can notify OpenRag to revoke the session.

### How It Works

1. User logs out from Keycloak (or is logged out by admin)
2. Keycloak sends a `POST` request to `https://openrag.example.com/auth/backchannel-logout` with a signed JWT `logout_token`
3. OpenRag verifies the JWT signature and extracts the `sid` claim
4. All `oidc_sessions` rows with matching `sid` are marked `revoked_at = now()`
5. OpenRag responds with `200 OK`
6. Next time the user tries to use an old session cookie, the middleware sees `revoked_at` is set and redirects to login

### IdP Configuration

**Keycloak:**

1. **Clients** → `openrag` → **Settings**
2. Enable **Backchannel logout session required**: ON
3. Set **Backchannel logout URL**: `https://openrag.example.com/auth/backchannel-logout`
4. Click **Save**

**LemonLDAP::NG:**

1. Set the backchannel logout URL in the relying party configuration
2. Consult LLNG docs for exact steps

### Request Format

The IdP sends:

```
POST /auth/backchannel-logout HTTP/1.1
Host: openrag.example.com
Content-Type: application/x-www-form-urlencoded

logout_token=eyJhbGc...
```

**`logout_token`** is a signed JWT with claims:
```json
{
  "iss": "https://idp.example.com/realms/openrag",
  "sub": "user-sub",
  "sid": "session-id",
  "aud": "openrag",
  "iat": 1234567890,
  "exp": 1234571490,
  "events": {
    "http://schemas.openid.net/event/backchannel-logout": {}
  }
}
```

OpenRag:
1. Fetches the IdP's JWKS (via auto-discovery)
2. Verifies the JWT signature, `iss`, `aud`, `exp`, `iat`
3. Checks the `events` claim contains the logout event
4. Extracts `sid` and revokes matching sessions
5. Returns `200 OK` or `400 Bad Request` if validation fails

### Testing Back-Channel Logout

Create a signed logout token manually (advanced):

```python
import jwt
import json
from datetime import datetime, timedelta

# Keycloak realm public key (from /.well-known/openid-configuration -> jwks_uri)
# For testing, sign with a private key

payload = {
    "iss": "https://idp.example.com/realms/openrag",
    "sub": "user-sub",
    "sid": "session-id-from-openrag",
    "aud": "openrag",
    "iat": int(datetime.utcnow().timestamp()),
    "exp": int((datetime.utcnow() + timedelta(minutes=5)).timestamp()),
    "events": {
        "http://schemas.openid.net/event/backchannel-logout": {}
    }
}

token = jwt.encode(payload, "your-private-key", algorithm="RS256")
print(token)
```

Then send:

```bash
curl -X POST http://localhost:8080/auth/backchannel-logout \
  -d "logout_token=${token}" \
  -H "Content-Type: application/x-www-form-urlencoded"
```

Expect `200 OK`.

---

## Troubleshooting

### 1. "OIDC_TOKEN_ENCRYPTION_KEY is not set"

**Error**: Application refuses to start when `AUTH_MODE=oidc`.

**Solution**: Generate and set the key:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
export OIDC_TOKEN_ENCRYPTION_KEY=<paste-key>
```

### 2. "Issuer mismatch" at discovery

**Error**: Backend logs show `Issuer mismatch: configured '…', got '…'` and `/auth/login` returns 500 "OIDC discovery failed".

**Cause**: OpenRag enforces **byte-for-byte equality** between `OIDC_ENDPOINT`, the `issuer` field returned by the IdP's discovery document, and the `iss` claim in tokens — per OIDC Core §2. The most common culprit is a **trailing slash mismatch**:

| IdP | Typical issuer form |
|-----|---------------------|
| Keycloak | `https://kc.example.com/realms/myrealm` (no trailing slash) |
| LemonLDAP::NG | `https://llng.example.com/` (WITH trailing slash) |
| Auth0 | `https://tenant.auth0.com/` (WITH trailing slash) |
| Google | `https://accounts.google.com` (no trailing slash) |

**Solution**: Configure `OIDC_ENDPOINT` to match the IdP's advertised issuer **exactly**. To check what the IdP actually returns:

```bash
curl -s http://your-idp/.well-known/openid-configuration | jq -r .issuer
```

Copy that string verbatim (including or excluding the trailing `/`) into `.env`:

```
OIDC_ENDPOINT=<exact string from jq output>
```

OpenRag builds the discovery URL by stripping any trailing slash internally, so both forms work for discovery — but the subsequent token-claim validation is strict.

### 3. "Invalid redirect URI"

**Error**: IdP rejects the callback with "Invalid redirect URI" or similar.

**Solution**: 
- Ensure `OIDC_REDIRECT_URI` matches exactly what's configured in the IdP (case-sensitive, trailing slashes matter)
- Example: `https://openrag.example.com/auth/callback` (NOT `https://openrag.example.com/auth/callback/`)
- Keycloak: **Clients** → **Valid redirect URIs**

### 4. "email claim not found"

**Error**: User logs in successfully, but OpenRag responds with 403 "email not found in claims".

**Solution**:
- Set `OIDC_EMAIL_SOURCE=userinfo` if `email` is not in the ID token
- Or, ask IdP admin to include `email` scope in the token
- Verify the IdP is returning `email` claim via OIDC `/userinfo` endpoint:
```bash
curl -H "Authorization: Bearer <access_token>" \
  https://idp.example.com/realms/openrag/protocol/openid-connect/userinfo
```

### 5. "external_user_id mismatch"

**Error**: User with `external_user_id=A` tries to log in, but OIDC `sub=B`.

**Cause**: The user's `external_user_id` was set to one value, but the IdP's `sub` claim changed (e.g., user was re-imported or IdP was reconfigured).

**Solution**:
- If intentional (user switched IdPs), clear the old `external_user_id`:
```bash
curl -X PATCH http://localhost:8080/users/42 \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"external_user_id": null}'
```
- Then log in again; the new `sub` will be backfilled

### 6. "email domain not whitelisted"

**Error**: User logs in, but OpenRag responds with 403 "email domain not whitelisted".

**Solution**:
- Set `OIDC_ALLOWED_EMAIL_DOMAINS` to allow the user's domain:
```bash
export OIDC_ALLOWED_EMAIL_DOMAINS=example.com,partner.org
```
- Or, empty it to allow any domain

### 6. "session not found" or "session expired"

**Error**: User can log in once, but subsequent requests show "unauthenticated".

**Cause**: Session cookie was lost or expired.

**Solution**:
- Ensure cookies are enabled in browser
- Check `openrag_session` cookie exists and is not marked `revoked_at` in DB
- Increase `access_token_expires_at` via OIDC scopes (`offline_access` + longer TTL in IdP)

### 7. "clock skew" or "token not yet valid"

**Error**: "iat claim is in the future" or "exp claim is in the past".

**Solution**:
- Sync system clocks between OpenRag and IdP servers
- Check NTP is running: `ntpq -p`

### 8. "invalid scope: offline_access"

**Error**: OIDC client redirect fails with "Invalid scope requested: offline_access".

**Solution**:
- Keycloak: Ensure `offline_access` scope is mapped to the client
  - **Clients** → `openrag` → **Client scopes** → Verify `offline_access` is in the assigned scopes
- LemonLDAP::NG: Check the OIDC relying party configuration includes `offline_access`

---

## Security Considerations

### Cookie Security

- **httpOnly**: Cookies are marked `httpOnly`, preventing JavaScript access (XSS mitigation)
- **Secure**: In production (HTTPS), cookies are marked `Secure` (only sent over HTTPS)
- **SameSite=Lax**: CSRF protection; allows top-level navigation but not cross-site subresource requests
- **Path=/**: Cookies sent for all paths
- **No Domain**: Host-only (not shared with subdomains)

### Token Encryption at Rest

- Access tokens and refresh tokens are encrypted using **Fernet** (symmetric encryption) before storage in the database
- The key (`OIDC_TOKEN_ENCRYPTION_KEY`) must be kept secret
- If the key is compromised, regenerate it and re-encrypt all sessions (manual process in v1; planned for v2)

### Token Rotation

- Access tokens have a short expiry (typically 5-15 minutes)
- Refresh tokens have a longer expiry (typically hours or days, configured in IdP)
- Middleware automatically refreshes access tokens when <60 seconds remain before expiry (lazy refresh)
- No extra API calls for users; happens transparently

### CSRF Mitigation

- Authorization requests use a server-generated `state` parameter
- The `state` is stored in a temporary, signed cookie (`idp_state`, 5-minute TTL)
- The callback validates that the returned `state` matches the cookie
- Prevents CSRF attacks on the callback endpoint

### Replay Protection

- **PKCE** (Proof Key for Code Exchange):
  - Client generates a `code_verifier` (43-128 character random string)
  - Client computes `code_challenge = BASE64URL(SHA256(code_verifier))`
  - Client sends `code_challenge` in the authorization request
  - Callback sends `code_verifier` in the token exchange request
  - IdP verifies they match, preventing authorization code interception
- **Nonce**:
  - Client generates a random `nonce`
  - Included in the authorization request and returned in the ID token
  - Callback verifies the nonce matches, preventing token replay

### Breach Scenarios

| Breach | Impact | Mitigation |
|--------|--------|-----------|
| OIDC_TOKEN_ENCRYPTION_KEY leaked | All encrypted tokens (access, refresh) decryptable | Rotate key + re-encrypt sessions (v2 feature) |
| Authorization code intercepted | Code is single-use + PKCE prevents exchange without code_verifier | PKCE enforced |
| Session cookie stolen (XSS) | Attacker can impersonate user in browser | httpOnly prevents JavaScript access; SameSite limits scope; session revocation via back-channel logout |
| IdP tokens (access/refresh) leaked | Attacker can call IdP on behalf of user | Refresh tokens are short-lived; access tokens are encrypted at rest |
| `state` cookie stolen | Attacker can forge authorization requests | `state` is signed (itsdangerous); 5-minute TTL |

### Best Practices

1. **Use HTTPS in production** — Secure and httpOnly cookies require HTTPS
2. **Rotate encryption keys periodically** (v2 feature; manual rotation needed in v1)
3. **Monitor back-channel logout requests** — Ensure IdP is sending them
4. **Set reasonable token lifetimes** in the IdP (e.g., 15-minute access tokens, 7-day refresh tokens)
5. **Use strong OIDC scopes** — Request only what you need (e.g., `openid email` vs. `openid email profile`)
6. **Audit user access** — Log all authentication and authorization events
7. **Implement a password policy** in the IdP
8. **Enforce MFA** in the IdP for sensitive users

---

## Additional Resources

- [OpenID Connect Core 1.0 Specification](https://openid.net/specs/openid-connect-core-1_0.html)
- [OpenID Connect Back-Channel Logout 1.0](https://openid.net/specs/openid-connect-backchannel-1_0.html)
- [Keycloak Documentation](https://www.keycloak.org/documentation)
- [LemonLDAP::NG Documentation](https://www.lemonldap-ng.org/documentation)
- [Authlib Documentation](https://docs.authlib.org/)

---

**Last Updated**: 2026-04-17
