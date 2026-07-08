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
