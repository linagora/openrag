---
title: 🔐 Authentication & Authorization Overview
---

This document explains how **user authentication** and **access control** work within the application.  
It covers admin behavior, user tokens, and partition-level permissions.

---

## **1. Authentication Activation**

### `AUTH_TOKEN`
- **`AUTH_TOKEN`** is the token used to bootstrap the admin user and authenticate protected API calls.
- In **`AUTH_MODE=token`**, if **`AUTH_TOKEN`** is absent, OpenRAG fails closed by default.
- In **`AUTH_MODE=oidc`**, human login uses the OIDC session flow; **`AUTH_TOKEN`** is not the OIDC login mechanism.
- Local open mode requires **`ALLOW_NO_AUTH=true`** together with `AUTH_MODE=token`. This should never be used in production.

:::danger[Attention !!!]
**`SUPER_ADMIN_MODE=true`** must be activated if you want admin users to access all existing partitions, not just the admin's own partitions.
:::

---

## **2. Admin Bootstrapping**

When `AUTH_TOKEN` is set:
1. On startup, the application checks whether an **admin user** already exists in the database.
2. If not, it **creates one automatically**:
   - `display_name`: `"Admin"`
   - `is_admin`: `True`
   - `token`: SHA-256 hash of the `AUTH_TOKEN` value

This admin user serves as the global entry point for bootstrapping the system.

### Operator-managed accounts (`auth.seed_users`)

Service accounts whose token is decided outside OpenRAG (typically in OpenBao, shared with
the client that uses it) can be declared in the configuration instead of being created
through `POST /users/`. The API provisions them at every startup, right after the admin
account and the default partition:

```yaml
# conf/config.yaml
auth:
  seed_users:
    - external_user_id: svc-cozy-stack   # stable key, matched on every startup
      display_name: cozy-stack
      token_env: COZY_STACK_TOKEN        # name of the env var holding the token, never the value
      is_admin: false
      partitions:
        - { name: twake, role: editor }
```

The account's shape lives in the configuration, the token stays in the environment. With
cozy-stack, both sides read the same OpenBao key: OpenRAG gets it as `COZY_STACK_TOKEN`,
cozy-stack puts the same value in the `rag:` section of its configuration, so the two
cannot drift apart. Rotating the token is: change it in OpenBao, restart the API.

On a deployment where `conf/config.yaml` is baked into the image (the Helm chart), pass
the same list as JSON in the `SEED_USERS` environment variable, which replaces
`auth.seed_users`. The chart renders it from `openrag.seedUsers` (see
[Kubernetes](/openrag/documentation/kubernetes/#operator-managed-accounts)).

Behaviour on each startup:

- **Token env var set**: the account is created, or updated, matched on
  `external_user_id`. Its stored hash is rewritten, its display name and admin flag follow
  the configuration and the listed memberships are created or set to the listed role. The
  one exception: an account that is the only owner of a listed partition (it created it
  through the API after a first startup that did not find it) stays its owner, with a
  warning, since demoting it would leave nobody to manage the partition's members.
  Memberships the entry does not list are never touched. The token it replaces stops
  working before the new one is written, so two entries can also swap tokens.
- **Token env var unset or empty**: the stored token is kept and a warning is logged, but
  the admin flag and the listed roles are still applied, so a right taken away in the
  configuration does not wait for the secret. A new account is not created, since it would
  have no credential. Startup carries on, so deployments that do not provide the variable
  keep working.
- **Entry removed from the configuration**: the account's token is revoked (cleared, so no
  bearer matches it), its admin flag is dropped and a warning is logged, at every startup.
  The row is kept, together with its memberships and what it uploaded, so partitions it
  owns are not left without an owner. It stays managed: listing it again with a token
  restores it, and the admin API refuses to give it a token or admin rights (409), since
  the next startup would take them away again. Delete it once it is no longer needed.
- **Partition that does not exist**: that membership is skipped with a warning. Partitions
  are never created by this step.
- **`is_admin: true`**: honoured, and a warning is logged on every startup.

Seeding never deletes an account or a membership: removing a partition from an entry, or
the entry itself, leaves the existing memberships in place. To take access away, remove
the membership through the API (`DELETE /partition/{partition}/users/{user_id}`).

What seeding refuses, each time with an error naming the entry and its env var, never the
token or its hash:

- An account created another way (through the API, or by an OIDC login, which stores the
  IdP `sub` in `external_user_id`) is never taken over, even when its `external_user_id`
  matches an entry. Only accounts created by this step are managed by it, and the admin
  account (`users.id = 1`) never is. Pick identifiers that cannot be an IdP `sub`, such as
  a `svc-` prefix.
- A token that is a published example value or shorter than 12 characters, the same rules
  as `AUTH_TOKEN` (`ALLOW_INSECURE_SECRETS=true` downgrades this to a warning on a
  disposable stack).
- A token equal to `AUTH_TOKEN`, to another entry's token (both entries are refused), or to
  the token of another existing account.

A refused token is not written. On an existing account the previous token hash stays in
place, while the admin flag and listed roles are still applied: a rotation to a refused
value leaves the old token valid until a startup with an accepted one. This is deliberate, as for an unset token env var: a mistake in the
new secret does not lock out the client still using the old one. When the old token must
stop working at once (it leaked, for instance), do not rely on the rotation: call
`POST /users/{user_id}/regenerate_token`, which replaces it immediately, or remove the
entry from `auth.seed_users` and restart, which revokes it. Then fix the secret and
restart.

The configuration itself is validated when it is loaded, and an error stops the boot:
`role` must be `owner`, `editor` or `viewer`, `external_user_id` and `token_env` must be
unique across entries, a partition may be listed once per entry, `token_env` cannot be
`AUTH_TOKEN`, and unknown keys (a plaintext `token:` for instance) are rejected. Unknown
keys directly under `auth:` are rejected too: a misspelt `seed_users` (such as the chart's
`seedUsers`) would otherwise load as an empty list, and the next startup would revoke
every managed account.

If `AUTH_TOKEN` is set to the token a managed account already holds (both read from the
same secret by mistake), the admin keeps it: the managed account's token is cleared at
startup with a warning, and its entry's token is then refused as above.

These accounts are for bearer tokens only. An OIDC login whose `sub` equals the
`external_user_id` of a managed account is refused with a 403 before anything is written
to the account, and any OIDC session found on a managed account is deleted at the next
startup.

The configuration stays the source of truth for these accounts. Changing the
`external_user_id` or the admin flag of a managed account through the admin API is refused
with a 409: seeding matches rows on `external_user_id`, so a renamed row would drop out of
its entry, and it sets the admin flag at every startup. Other admin edits (display name,
the role on a listed partition, a regenerated token) are accepted but only last until the
next startup that has the entry's token, which rewrites them from the configuration. Make
those changes in `auth.seed_users` instead.
Memberships on partitions the entry does not list are left alone.

Deleting a managed account through `DELETE /users/{user_id}` is refused with a 409 while
its entry is still in `auth.seed_users`: the delete would remove every partition the
account owns, and the next startup would recreate the account under a new id with only
its listed memberships. To delete it, remove the entry from `auth.seed_users` and restart
the API (the account's token is revoked), then delete it.

If the seeding step itself fails (a database error, for instance), the error is logged
without the token or the driver's message and the API starts anyway. What takes access
away (revoking removed entries, ending OIDC sessions, clearing a token about to be
replaced) is committed first, so a failure later in the step cannot bring it back; the
accounts being created or updated keep their previous state until the next startup.

Only the API process provisions these accounts. Several replicas starting together apply
the list one after the other, under a database lock. The Chainlit and MCP processes never
write them.

Each process applies the configuration it booted with, so the last one to start wins. A
replica still on the old configuration or token during a rollout, or a Ray pod that was
not restarted, re-applies the old list or token hash when it boots. After changing the
seed users or a token, complete the rollout and restart the Ray pods.

The token is hashed exactly as the environment variable holds it, as for `AUTH_TOKEN`: a
trailing newline in the Secret becomes part of the token and the client's bearer no longer
matches.

---

## **3. Token Management**

### Generation
- Each new user is assigned a token at creation time (format: `or-<random hex>`).  
- The app **returns the raw token** to the API caller once (e.g., `POST /users` response).

### Storage
- Only a **SHA-256 hash** of the token is stored in PostgreSQL.
- The raw token **is never persisted**, ensuring that leaked database contents cannot reveal user credentials.

### Validation
- When an API request includes an **`Authorization: Bearer <token>`** header:
  1. The middleware extracts the token.
  2. The hash of this token is computed.
  3. The hash is compared against the stored value in the `users` table.

---

## **4. User Roles**

### 👑 Admin
- Full access to all API routes, including:
  - User management
  - Actor management
  - Queue and system information
- Can also create other users and assign privileges.
- Admins can use the app **as regular users** (own partitions, files, etc.).
- By default, an admin **cannot view other users’ data**.

### 🧠 Super Admin Mode
- Controlled by the environment variable **`SUPER_ADMIN_MODE`**.
- When `SUPER_ADMIN_MODE=true`:
  - The admin can access **all partitions and data** across users.
  - Partition-level access restrictions are ignored.
- When `SUPER_ADMIN_MODE=false`:
  - Admin privileges are **limited to admin-only operations** (user creation, actor management, etc.).
  - Data-level access (partitions/files) requires using a normal user account.

---

## **5. Regular Users**

- Created by an admin via the `/users` endpoint.
- Receive a personal API token (returned once upon creation).
- Can authenticate using `Authorization: Bearer <token>`.

Users can:
- Create and manage **their own partitions** and **files**.
- Access shared partitions based on assigned roles.

---

## **6. Partition Access Roles**

Access control is handled through the **`partition_memberships`** table.  
Each user–partition relationship defines a **role**:

| Role | Description | Capabilities |
|------|--------------|---------------|
| **owner** | Partition creator or owner | Full access — can delete the partition, manage members, edit files, etc. |
| **editor** | Collaborator | Can read and write files within the partition |
| **viewer** | Read-only member | Can view content and perform semantic search or chat but not modify data |

Role-based restrictions are enforced via dependency guards:
- `require_partition_owner`
- `require_partition_editor`
- `require_partition_viewer`

---

## **7. Authorization Flow Summary**

1. Request arrives with optional `Authorization: Bearer <token>`.
2. In `AUTH_MODE=token`, if `AUTH_TOKEN` is **unset**, authentication is rejected unless `ALLOW_NO_AUTH=true` is explicitly enabled for local development.
3. If a token is configured:
   - Middleware hashes the token.
   - Looks up the user by hash.
   - Loads their partition memberships.
4. User info and memberships are attached to `request.state`.
5. Role-based dependencies ensure the user has proper privileges before executing the endpoint logic.

---

## **8. Summary Diagram**

```
┌───────────────────────┐
│ Incoming Request      │
│ Authorization: Bearer │
└────────────┬──────────┘
             │
             ▼
┌───────────────────────────┐
│ AuthMiddleware            │
│ - Hash token (SHA-256)    │
│ - Lookup user in DB       │
│ - Load memberships        │
│ - Attach to request.state │
└────────────┬──────────────┘
             │
             ▼
┌──────────────────────────────┐
│ Endpoint Dependency Checks   │
│ (e.g., require_partition_*)  │
└────────────┬─────────────────┘
             │
             ▼
┌──────────────────────────────┐
│ Route Logic Executes         │
│ with validated user context  │
└──────────────────────────────┘
```

---

## **9. Security Highlights**

- No plaintext tokens stored in database.
- SHA-256 hashing for authentication.
- Partition-based role hierarchy for fine-grained access control.
- Admin privileges separated from regular user data access.
- Configurable **`SUPER_ADMIN_MODE`** for system-wide debugging or admin override.

---
