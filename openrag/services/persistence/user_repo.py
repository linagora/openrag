"""Postgres implementation of :class:`UserRepository`.

Backs the ``users`` table (and, when the post-refactoring ``api_keys``
table lands, ``api_keys``). The legacy
:class:`components.indexer.vectordb.utils.PartitionFileManager` exposed
eleven user-shaped methods (``create_user``, ``get_user_by_id``,
``get_user_by_token``, ``delete_user``, ``update_user``, ``list_users``,
``regenerate_user_token``, ``user_exists``, ``get_user_by_external_id``,
``update_user_fields``, ``_ensure_admin_user``) which map onto this class.
The six partition-membership methods moved to
:class:`~services.persistence.partition_membership_repo.PgPartitionMembershipRepository`
(7A.2 one-repo-per-entity layout). This class still *reads*
``partition_memberships`` via :meth:`_fetch_memberships` to hydrate the
``User`` aggregate's ``partitions`` field — a read-only denormalisation
inside the user aggregate boundary, not membership management.

Notes on the schema vs. the port:

* The port :class:`~openrag.core.models.user.User` model carries
  ``password_hash``, ``is_active`` and ``updated_at`` fields that have
  no column today. They are treated as ``None`` / ``True`` / ``created_at``
  respectively at the boundary so the domain shape stays useful for
  callers.
* The ``UserRepository`` port also defines four ``api_key_*`` methods.
  OpenRAG currently stores one hashed token in ``users.token`` — a real
  ``api_keys`` table is on the post-refactoring roadmap. Until then the
  api-key methods raise :class:`NotImplementedError` to signal the gap
  loudly rather than silently returning empty lists.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import asyncpg
from core.config.auth import ADMIN_TOKEN_ENV_VAR, SeedUserConfig
from core.config.secrets_guard import ALLOW_INSECURE_SECRETS_ENV_VAR, MIN_SECRET_LENGTH, is_known_default
from core.models.user import ApiKey, PartitionRole, User, UserPartition
from core.ports.user_repo import UserRepository
from core.utils.logging import get_logger

logger = get_logger()


def _hash_token(token: str) -> str:
    """SHA-256 hex digest of a token string.

    Matches the legacy :meth:`PartitionFileManager.hash_token` so existing
    rows continue to validate against the same hash. Exposed at module
    level so callers (e.g. auth middleware) can hash before lookup
    without instantiating the repo.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


#: Serialises :meth:`PgUserRepository.ensure_seed_users` across API replicas
#: booting together. Transaction-scoped: released on commit or rollback.
_SEED_USERS_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('users.seed_users'))"


def _resolve_seed_tokens(
    seed_users: Sequence[SeedUserConfig],
    env: Mapping[str, str],
) -> tuple[dict[str, str | None], set[str]]:
    """Read and vet each seed user's token before any row is touched.

    Returns ``(tokens, refused)``: ``tokens`` maps the ``external_user_id`` of
    every entry to its token, ``None`` when the env var is unset or blank or the
    token is refused; ``refused`` holds the entries whose token was refused. A
    token is refused when it fails the boot-time secret policy (a published
    default, or shorter than :data:`MIN_SECRET_LENGTH`;
    ``ALLOW_INSECURE_SECRETS=true`` downgrades that to a warning, as for every
    other credential), when it is ``AUTH_TOKEN``'s value, or when two entries
    share it, in which case both are refused since neither can be told apart
    from the other at lookup time. A refused token is never written, but the
    entry still applies its admin flag and roles to an existing account. Log
    lines name the entry by ``external_user_id`` only: neither the token nor
    the ``token_env`` name is logged, since a token pasted where the name
    belongs can pass the name pattern.
    """
    allow_insecure = env.get(ALLOW_INSECURE_SECRETS_ENV_VAR, "").strip().lower() == "true"
    admin_token = env.get(ADMIN_TOKEN_ENV_VAR) or None
    tokens: dict[str, str | None] = {}
    refused: set[str] = set()

    def refuse(seed: SeedUserConfig, reason: str) -> None:
        logger.bind(external_user_id=seed.external_user_id).error(
            f"Seed user token refused: {reason}; the token is not written"
        )
        tokens[seed.external_user_id] = None
        refused.add(seed.external_user_id)

    for seed in seed_users:
        value = env.get(seed.token_env, "")
        if not value.strip():
            tokens[seed.external_user_id] = None
            continue
        weak = is_known_default(value) or len(value.strip()) < MIN_SECRET_LENGTH
        if weak and not allow_insecure:
            refuse(
                seed,
                f"its token is a published default or shorter than {MIN_SECRET_LENGTH} characters "
                f"(set {ALLOW_INSECURE_SECRETS_ENV_VAR}=true to override on a disposable stack)",
            )
            continue
        if weak:
            logger.bind(external_user_id=seed.external_user_id).warning(
                f"{ALLOW_INSECURE_SECRETS_ENV_VAR}=true: seeding a token that fails the secret policy"
            )
        if admin_token is not None and value == admin_token:
            refuse(seed, f"its token is the value of {ADMIN_TOKEN_ENV_VAR}")
            continue
        tokens[seed.external_user_id] = value

    by_value: dict[str, list[SeedUserConfig]] = {}
    for seed in seed_users:
        value = tokens.get(seed.external_user_id)
        if value is not None:
            by_value.setdefault(value, []).append(seed)
    for sharing in by_value.values():
        if len(sharing) > 1:
            names = ", ".join(s.external_user_id for s in sharing)
            for seed in sharing:
                refuse(seed, f"its token is shared by several seed users ({names})")
    return tokens, refused


class PgUserRepository(UserRepository):
    """asyncpg-backed implementation of :class:`UserRepository`."""

    def __init__(self, pool_getter: Callable[[], asyncpg.Pool]) -> None:
        self._pool_getter = pool_getter

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool_getter()

    # ── User CRUD ────────────────────────────────────────────────────

    async def create_user(self, user: User) -> User:
        """Insert a user row and return it with its assigned PK.

        ``User.password_hash`` is dropped because the column does not
        exist yet — when password auth lands we'll add the column and
        wire it here. ``token`` / ``token_hash`` should be set out-of-band
        via :meth:`set_user_token`; this method does NOT generate one.

        ``external_user_id`` is normalized: an empty string is coerced to
        ``NULL`` so the UNIQUE constraint allows multiple users with no
        external id, instead of raising a UniqueViolation on the second
        insert.
        """
        row = await self.pool.fetchrow(
            """
            INSERT INTO users (display_name, external_user_id, email,
                               is_admin, file_quota, file_count, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, COALESCE($7, NOW()))
            RETURNING *
            """,
            user.display_name,
            (user.external_user_id.strip() or None) if user.external_user_id else None,
            (user.email.strip().lower() if user.email else None),
            user.is_admin,
            user.file_quota,
            user.file_count,
            user.created_at,
        )
        return self._row_to_user(row)

    async def get_user(self, user_id: int) -> User | None:
        row = await self.pool.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        if row is None:
            return None
        memberships = await self._fetch_memberships(user_id)
        return self._row_to_user(row, memberships)

    async def get_users_by_ids(self, user_ids: list[int]) -> list[User]:
        if not user_ids:
            return []
        rows = await self.pool.fetch(
            "SELECT * FROM users WHERE id = ANY($1::int[])",
            user_ids,
        )
        return [self._row_to_user(row) for row in rows]

    async def get_user_by_email(self, email: str) -> User | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM users WHERE email = $1",
            email.strip().lower(),
        )
        if row is None:
            return None
        memberships = await self._fetch_memberships(row["id"])
        return self._row_to_user(row, memberships)

    async def get_user_by_token(self, token_hash: str) -> User | None:
        """Lookup by the SHA-256 hash of the bearer token.

        The auth middleware hashes the raw token before calling this, so
        the repo never sees plaintext.
        """
        row = await self.pool.fetchrow(
            "SELECT * FROM users WHERE token = $1",
            token_hash,
        )
        if row is None:
            return None
        memberships = await self._fetch_memberships(row["id"])
        return self._row_to_user(row, memberships)

    async def get_user_by_external_id(self, external_id: str) -> User | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM users WHERE external_user_id = $1",
            external_id,
        )
        if row is None:
            return None
        memberships = await self._fetch_memberships(row["id"])
        return self._row_to_user(row, memberships)

    async def list_users(self, offset: int = 0, limit: int = 50) -> list[User]:
        rows = await self.pool.fetch(
            "SELECT * FROM users ORDER BY id LIMIT $1 OFFSET $2",
            limit,
            offset,
        )
        return [self._row_to_user(r) for r in rows]

    async def update_user(self, user_id: int, **fields: Any) -> User | None:
        """Patch fields on a user row.

        Silently ignores unknown columns — keeps the call site forgiving
        when the domain model carries fields the schema does not have
        yet (``password_hash``, ``is_active``, ``updated_at``).
        """
        allowed = {
            "display_name",
            "external_user_id",
            "email",
            "is_admin",
            "file_quota",
            "file_count",
            "token",
        }
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "email" and isinstance(value, str):
                value = value.strip().lower()
            params.append(value)
            sets.append(f"{key} = ${len(params)}")
        if not sets:
            return await self.get_user(user_id)
        params.append(user_id)
        row = await self.pool.fetchrow(
            f"UPDATE users SET {', '.join(sets)} WHERE id = ${len(params)} RETURNING *",
            *params,
        )
        if row is None:
            return None
        memberships = await self._fetch_memberships(user_id)
        return self._row_to_user(row, memberships)

    async def delete_user(self, user_id: int) -> bool:
        result = await self.pool.execute("DELETE FROM users WHERE id = $1", user_id)
        return result.endswith(" 1")

    async def count_users(self) -> int:
        return await self.pool.fetchval("SELECT COUNT(*)::int FROM users")

    # ── API keys (stub — table not yet shipped) ──────────────────────

    async def create_api_key(self, key: ApiKey) -> ApiKey:
        raise NotImplementedError(
            "api_keys table is on the post-refactoring roadmap; use users.token until then.",
        )

    async def get_api_keys_by_prefix(self, prefix: str) -> list[ApiKey]:
        raise NotImplementedError(
            "api_keys table is on the post-refactoring roadmap; use users.token until then.",
        )

    async def list_api_keys_for_user(self, user_id: int) -> list[ApiKey]:
        raise NotImplementedError(
            "api_keys table is on the post-refactoring roadmap; use users.token until then.",
        )

    async def delete_api_key(self, key_id: str) -> bool:
        raise NotImplementedError(
            "api_keys table is on the post-refactoring roadmap; use users.token until then.",
        )

    # ── Legacy method names used by the Phase 7C shim ────────────────

    async def create_legacy_user(
        self,
        display_name: str | None,
        external_user_id: str | None,
        email: str | None,
        is_admin: bool,
        file_quota: int | None,
    ) -> dict:
        """TODO(phase-9): remove. Mirror of legacy ``create_user``.

        Generates a plain ``or-`` token, stores its hash, returns the
        plaintext exactly once (it is never persisted unhashed).
        """
        plaintext = f"or-{secrets.token_hex(16)}"
        token_hash = _hash_token(plaintext)
        row = await self.pool.fetchrow(
            """
            INSERT INTO users (display_name, external_user_id, email,
                               token, is_admin, file_quota, file_count, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, 0, NOW())
            RETURNING *
            """,
            display_name,
            (external_user_id.strip() or None) if external_user_id else None,
            (email.strip().lower() if email else None),
            token_hash,
            is_admin,
            file_quota,
        )
        return {
            "id": row["id"],
            "display_name": row["display_name"],
            "external_user_id": row["external_user_id"],
            "email": row["email"],
            "token": plaintext,
            "is_admin": row["is_admin"],
            "file_quota": row["file_quota"],
            "file_count": row["file_count"],
        }

    async def regenerate_user_token(self, user_id: int) -> dict | None:
        """TODO(phase-9): remove. Rotate ``users.token`` and surface the plaintext."""
        plaintext = f"or-{secrets.token_hex(16)}"
        token_hash = _hash_token(plaintext)
        row = await self.pool.fetchrow(
            """
            UPDATE users SET token = $2
            WHERE id = $1
            RETURNING *
            """,
            user_id,
            token_hash,
        )
        if row is None:
            return None
        return {
            "id": row["id"],
            "display_name": row["display_name"],
            "external_user_id": row["external_user_id"],
            "token": plaintext,
            "is_admin": row["is_admin"],
            "file_quota": row["file_quota"],
            "file_count": row["file_count"],
        }

    async def get_user_by_token_plain(self, token: str) -> dict | None:
        """TODO(phase-9): remove. Hash + lookup + serialise to legacy dict shape."""
        return await self.get_user_dict_by_id(
            await self.pool.fetchval(
                "SELECT id FROM users WHERE token = $1",
                _hash_token(token),
            ),
        )

    async def get_user_dict_by_id(self, user_id: int | None) -> dict | None:
        """TODO(phase-9): remove. Legacy dict shape with ``memberships`` list."""
        if user_id is None:
            return None
        row = await self.pool.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        if row is None:
            return None
        memberships = await self.pool.fetch(
            "SELECT * FROM partition_memberships WHERE user_id = $1 ORDER BY added_at",
            user_id,
        )
        return {
            "id": row["id"],
            "display_name": row["display_name"],
            "external_user_id": row["external_user_id"],
            "email": row["email"],
            "is_admin": row["is_admin"],
            "file_quota": row["file_quota"],
            "file_count": row["file_count"],
            "memberships": [
                {
                    "partition": m["partition_name"],
                    "role": m["role"],
                    "added_at": m["added_at"].isoformat() if m["added_at"] else None,
                }
                for m in memberships
            ],
        }

    async def get_user_by_external_id_dict(self, external_user_id: str) -> dict | None:
        """TODO(phase-9): remove. Legacy dict shape, lookup by OIDC sub claim."""
        row = await self.pool.fetchrow(
            "SELECT id FROM users WHERE external_user_id = $1",
            external_user_id,
        )
        return await self.get_user_dict_by_id(row["id"]) if row else None

    async def list_users_dict(self) -> list[dict]:
        """TODO(phase-9): remove. Legacy list shape used by /users/ endpoint."""
        rows = await self.pool.fetch("SELECT * FROM users ORDER BY id")
        return [
            {
                "id": r["id"],
                "display_name": r["display_name"],
                "external_user_id": r["external_user_id"],
                "email": r["email"],
                "is_admin": r["is_admin"],
                "file_quota": r["file_quota"],
                "file_count": r["file_count"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]

    async def user_exists(self, user_id: int) -> bool:
        """TODO(phase-9): remove."""
        return await self.pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM users WHERE id = $1)",
            user_id,
        )

    # Whitelist mirrored from the legacy PartitionFileManager. Three
    # layers (startup validator, claim parser, repo) all enforce the
    # same set as defence-in-depth against an OIDC claim mapping that
    # would otherwise let a remote IdP rewrite arbitrary user columns.
    _OIDC_WRITABLE_USER_FIELDS = frozenset({"display_name", "email"})

    async def update_user_fields(self, user_id: int, fields: dict[str, Any]) -> None:
        """TODO(phase-9): remove. Strict-whitelist update for the OIDC claim mapper."""
        if not fields:
            return
        bad = set(fields) - self._OIDC_WRITABLE_USER_FIELDS
        if bad:
            raise ValueError(f"Cannot update non-whitelisted user fields: {sorted(bad)}")
        cleaned = {k: v for k, v in fields.items() if v is not None}
        if not cleaned:
            return
        if "email" in cleaned and isinstance(cleaned["email"], str):
            cleaned["email"] = cleaned["email"].strip().lower()
        sets: list[str] = []
        params: list[Any] = []
        for key, value in cleaned.items():
            params.append(value)
            sets.append(f"{key} = ${len(params)}")
        params.append(user_id)
        result = await self.pool.execute(
            f"UPDATE users SET {', '.join(sets)} WHERE id = ${len(params)}",
            *params,
        )
        if not result.endswith(" 1"):
            raise ValueError(f"User {user_id} not found")

    async def ensure_admin_user(self, admin_token: str | None) -> str:
        """TODO(phase-9): remove. Bootstrap mirror of the legacy admin-bootstrap.

        Ensures ``users.id = 1`` exists with ``is_admin = TRUE`` and the
        token hash matching ``admin_token``. Generates a token if none
        is supplied. Returns whichever plaintext token is now valid.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow("SELECT id, token FROM users WHERE id = 1")
                if existing is None:
                    plaintext = admin_token or f"or-{secrets.token_hex(16)}"
                    await conn.execute(
                        """
                        INSERT INTO users (id, display_name, token, is_admin, file_count, created_at)
                        VALUES (1, 'Admin', $1, TRUE, 0, NOW())
                        """,
                        _hash_token(plaintext),
                    )
                    # Keep the sequence ahead of the explicit id=1 insert so
                    # subsequent `INSERT INTO users` calls don't collide.
                    await conn.execute(
                        "SELECT setval(pg_get_serial_sequence('users','id'), GREATEST(1, (SELECT MAX(id) FROM users)))"
                    )
                    return plaintext
                if admin_token:
                    # Operator-provided AUTH_TOKEN is authoritative: keep DB in sync.
                    # A managed account holding the same hash (both secrets read
                    # from one OpenBao key by mistake) would fail the unique index
                    # and keep the API at 503. AUTH_TOKEN wins over a seed entry
                    # (_resolve_seed_tokens), so the managed row loses it here.
                    cleared = await conn.fetch(
                        "UPDATE users SET token = NULL WHERE token = $1 AND managed_by_config AND id <> 1 RETURNING id",
                        _hash_token(admin_token),
                    )
                    for row in cleared:
                        logger.bind(user_id=row["id"]).warning(
                            f"{ADMIN_TOKEN_ENV_VAR} is also the token of a managed account: "
                            "its token is cleared, the admin keeps it"
                        )
                    await conn.execute(
                        "UPDATE users SET is_admin = TRUE, token = $1 WHERE id = 1",
                        _hash_token(admin_token),
                    )
                    return admin_token
                # AUTH_TOKEN unset: preserve the previously stored token (and
                # any /users/{id}/regenerate_token rotation) instead of
                # silently invalidating every admin client on each restart.
                await conn.execute("UPDATE users SET is_admin = TRUE WHERE id = 1")
                return ""

    async def ensure_seed_users(
        self,
        seed_users: Sequence[SeedUserConfig],
        env: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Provision the operator-managed accounts declared in ``auth.seed_users``.

        The generalisation of :meth:`ensure_admin_user` to every account whose
        token is decided outside OpenRag (OpenBao, a Kubernetes Secret): the
        shape comes from configuration, the token from the environment variable
        each entry names. Rows created here carry ``managed_by_config`` and the
        configuration is their source of truth:

        * token env var set: the account is upserted on ``external_user_id`` and
          its stored hash rewritten, so a rotation is "change the secret,
          restart". Listed memberships are upserted to the listed role, except
          that a partition's only owner is never demoted; unlisted ones (such as
          the owner row of a partition the account created through the API) are
          never touched.
        * token env var unset, or its token refused: the stored token is kept
          and a new account is not created (it would have no credential), but
          an existing account still gets its admin flag and listed roles, so a
          right taken away in the configuration is not held back by a missing
          secret. Logged, not fatal.
        * a managed account no longer listed: its token is cleared (no hash
          matches ``NULL``) and admin rights dropped, at every startup. The row
          and its memberships stay, so partitions it owns are not orphaned, and
          listing the entry again restores it.
        * any OIDC session on a managed account is deleted: such accounts
          cannot log in through the IdP (see ``AuthService._resolve_user``).

        A row that exists without the marker (created through the API, or by
        an OIDC login matching on ``sub``) is never taken over, and
        ``users.id = 1`` is never touched. A token that fails the secret
        policy, repeats another entry's token or ``AUTH_TOKEN``, or is held by
        an account outside the list is not written. A partition that does not
        exist is skipped too, never created. No log line carries a token or a
        hash.

        Runs under an advisory lock, so API replicas booting together apply it
        once each, serially, in two transactions. The first only takes access
        away (OIDC sessions, removed entries, the previous token of a listed
        account being given a new one) and commits before the second creates
        and updates the listed accounts: a failure there, a lock timeout for
        instance, cannot bring a revoked token back. Clearing the previous
        tokens first also lets two entries swap tokens in one run. Returns the
        outcome per ``external_user_id`` (``created``, ``updated``,
        ``untouched``, ``skipped``, ``revoked``); a revoked row without one is
        keyed ``users.id=<id>``.
        """
        if not seed_users and not await self.pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM users WHERE managed_by_config AND id <> 1)"
        ):
            return {}  # nothing configured, nothing to revoke
        environ = os.environ if env is None else env
        tokens, refused = _resolve_seed_tokens(seed_users, environ)
        outcomes: dict[str, str] = {}

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(_SEED_USERS_LOCK_SQL)
                if await conn.fetchval("SELECT 1 FROM users WHERE id = 1") is None:
                    # Without the admin row the next insert could take id 1,
                    # which ensure_admin_user would then promote to admin.
                    logger.error("Seed users not provisioned: the admin account (users.id = 1) does not exist yet")
                    return {seed.external_user_id: "skipped" for seed in seed_users}
                await self._end_managed_oidc_sessions(conn)
                outcomes.update(await self._revoke_removed_seed_users(conn, seed_users))
                await self._clear_replaced_seed_tokens(conn, tokens)

            async with conn.transaction():
                await conn.execute(_SEED_USERS_LOCK_SQL)
                # Lock the listed partitions before any users row: a partition
                # delete holds its row FOR UPDATE, then updates the file
                # counters of the files' creators. Taken here, a delete either
                # waits for this transaction or finishes first (its row is then
                # skipped below), and neither side waits while holding a row the
                # other needs. Locked later, it could deadlock, or the membership
                # insert would fail its foreign key and abort the whole run.
                present = {
                    r["partition"]
                    for r in await conn.fetch(
                        """
                        SELECT partition FROM partitions WHERE partition = ANY($1::text[])
                        ORDER BY partition FOR KEY SHARE
                        """,
                        sorted({m.name for seed in seed_users for m in seed.partitions}),
                    )
                }
                for seed in seed_users:
                    outcomes[seed.external_user_id] = await self._apply_seed_user(
                        conn,
                        seed,
                        tokens[seed.external_user_id],
                        seed.external_user_id in refused,
                        present,
                    )
        return outcomes

    @staticmethod
    async def _end_managed_oidc_sessions(conn: asyncpg.Connection) -> None:
        """A managed account never logs in through the IdP. Sessions an older
        release let a matching ``sub`` open on one end here."""
        ended = await conn.execute(
            "DELETE FROM oidc_sessions WHERE user_id IN (SELECT id FROM users WHERE managed_by_config AND id <> 1)"
        )
        if ended != "DELETE 0":
            logger.bind(count=int(ended.split()[-1])).warning(
                "Seed users: ended OIDC sessions opened on managed accounts"
            )

    @staticmethod
    async def _revoke_removed_seed_users(
        conn: asyncpg.Connection, seed_users: Sequence[SeedUserConfig]
    ) -> dict[str, str]:
        """Clear the token and admin flag of every managed row no longer listed.

        ``IS NOT TRUE`` also catches a managed row whose external_user_id is
        NULL. Memberships are kept: without a token the account cannot
        authenticate, and dropping them would orphan the partitions it owns.
        Access is removed through the API.
        """
        outcomes: dict[str, str] = {}
        stale = await conn.fetch(
            """
            SELECT id, external_user_id FROM users
            WHERE managed_by_config AND id <> 1 AND (external_user_id = ANY($1::text[])) IS NOT TRUE
            """,
            sorted({seed.external_user_id for seed in seed_users}),
        )
        for row in stale:
            key = row["external_user_id"] or f"users.id={row['id']}"
            revoked = await conn.fetchval(
                """
                UPDATE users SET token = NULL, is_admin = FALSE
                WHERE id = $1 AND (token IS NOT NULL OR is_admin)
                RETURNING id
                """,
                row["id"],
            )
            if revoked is not None:
                logger.bind(external_user_id=row["external_user_id"], user_id=row["id"]).warning(
                    "Seed user removed from auth.seed_users: token revoked, memberships kept"
                )
            outcomes[key] = "revoked"
        return outcomes

    @staticmethod
    async def _clear_replaced_seed_tokens(conn: asyncpg.Connection, tokens: Mapping[str, str | None]) -> None:
        """Clear the stored token of each listed account about to get another one.

        Committed with the revocations, so a rotated-away token stops working
        even if the write of the new one fails. It also frees the hashes before
        any is written: when two entries swap tokens, each new token is still
        the other row's when the entries are applied one by one, and neither
        could be written. A token held by an account this step does not clear
        will be refused, so that entry keeps its previous token.
        """
        replacing = {external_id: _hash_token(token) for external_id, token in tokens.items() if token is not None}
        if not replacing:
            return
        await conn.execute(
            """
            UPDATE users AS u SET token = NULL
            FROM unnest($1::text[], $2::text[]) AS s(external_user_id, token)
            WHERE u.external_user_id = s.external_user_id
              AND u.managed_by_config AND u.id <> 1
              AND u.token IS DISTINCT FROM s.token AND u.token IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM users AS holder
                WHERE holder.token = s.token
                  AND holder.external_user_id IS DISTINCT FROM s.external_user_id
                  AND NOT (holder.managed_by_config AND holder.id <> 1 AND holder.external_user_id = ANY($1::text[]))
              )
            """,
            list(replacing),
            list(replacing.values()),
        )

    async def _apply_seed_user(
        self,
        conn: asyncpg.Connection,
        seed: SeedUserConfig,
        token: str | None,
        refused: bool,
        present: set[str],
    ) -> str:
        log = logger.bind(external_user_id=seed.external_user_id)
        existing = await conn.fetchrow(
            "SELECT id, managed_by_config FROM users WHERE external_user_id = $1",
            seed.external_user_id,
        )
        if existing is not None and (existing["id"] == 1 or not existing["managed_by_config"]):
            log.error(
                "Seed user skipped: an account with this external_user_id already exists and was not "
                "created from auth.seed_users (API or OIDC); it is left untouched"
            )
            return "skipped"

        token_hash = _hash_token(token) if token is not None else None
        if token_hash is not None and await conn.fetchval(
            "SELECT id FROM users WHERE token = $1 AND external_user_id IS DISTINCT FROM $2",
            token_hash,
            seed.external_user_id,
        ):
            log.error("Seed user token refused: it is already the token of another account; the token is not written")
            token_hash, refused = None, True
        if token_hash is None:
            if existing is None:
                if not refused:
                    log.warning("Seed user token env var is unset: account not created")
                return "skipped"
            if not refused:
                log.warning("Seed user token env var is unset: token kept, admin flag and roles applied")

        if seed.is_admin:
            log.warning("Seed user is provisioned with admin rights (is_admin: true)")
        try:
            # A savepoint, so a constraint violation the checks above could not
            # foresee (a concurrent API insert) skips this entry only. The
            # driver's message is not logged: it can quote the token hash.
            async with conn.transaction():
                if token_hash is None:
                    user_id = await conn.fetchval(
                        "UPDATE users SET is_admin = $2 WHERE id = $1 AND managed_by_config RETURNING id",
                        existing["id"],
                        seed.is_admin,
                    )
                else:
                    user_id = await conn.fetchval(
                        """
                        INSERT INTO users (external_user_id, display_name, token, is_admin,
                                           file_count, created_at, managed_by_config)
                        VALUES ($1, $2, $3, $4, 0, NOW(), TRUE)
                        ON CONFLICT (external_user_id) DO UPDATE
                          SET display_name = EXCLUDED.display_name,
                              token = EXCLUDED.token,
                              is_admin = EXCLUDED.is_admin
                          WHERE users.managed_by_config AND users.id <> 1
                        RETURNING id
                        """,
                        seed.external_user_id,
                        seed.display_name,
                        token_hash,
                        seed.is_admin,
                    )
                if user_id is None:
                    log.error("Seed user skipped: the account was taken by an unmanaged row meanwhile")
                    return "skipped"
                await self._sync_seed_memberships(conn, user_id, seed, present, log)
        except asyncpg.UniqueViolationError as exc:
            log.bind(constraint=exc.constraint_name).error("Seed user skipped: unique constraint violated")
            return "skipped"
        except asyncpg.ForeignKeyViolationError as exc:
            log.bind(constraint=exc.constraint_name).error("Seed user skipped: foreign key constraint violated")
            return "skipped"
        if token_hash is None:
            return "skipped" if refused else "untouched"
        return "updated" if existing is not None else "created"

    @staticmethod
    async def _sync_seed_memberships(
        conn: asyncpg.Connection, user_id: int, seed: SeedUserConfig, present: set[str], log: Any
    ) -> None:
        for membership in seed.partitions:
            if membership.name not in present:
                log.bind(partition=membership.name).warning(
                    "Seed user membership skipped: partition does not exist (it is not created)"
                )
                continue
            # The account may own a listed partition it created through the
            # API after the first boot. Demoting a partition's only owner would
            # leave nobody but a super-admin able to manage its members.
            applied = await conn.fetchval(
                """
                INSERT INTO partition_memberships (partition_name, user_id, role, added_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (partition_name, user_id) DO UPDATE SET role = EXCLUDED.role
                  WHERE partition_memberships.role <> 'owner'
                     OR EXISTS (
                        SELECT 1 FROM partition_memberships AS other
                        WHERE other.partition_name = EXCLUDED.partition_name
                          AND other.role = 'owner' AND other.user_id <> EXCLUDED.user_id
                     )
                RETURNING role
                """,
                membership.name,
                user_id,
                membership.role.value,
            )
            if applied is None and membership.role is not PartitionRole.OWNER:
                log.bind(partition=membership.name, role=membership.role.value).warning(
                    "Seed user membership kept as owner: the account is the partition's only owner"
                )

    # ── Helpers ──────────────────────────────────────────────────────

    async def _fetch_memberships(self, user_id: int) -> list[UserPartition]:
        rows = await self.pool.fetch(
            "SELECT * FROM partition_memberships WHERE user_id = $1 ORDER BY added_at",
            user_id,
        )
        return [self._row_to_user_partition(r) for r in rows]

    @staticmethod
    def _row_to_user(
        row: asyncpg.Record,
        memberships: list[UserPartition] | None = None,
    ) -> User:
        # `password_hash`, `is_active`, `updated_at` do not exist on the
        # current schema; fall back to safe defaults so the domain model
        # stays consistent even though the underlying row is narrower.
        created = row["created_at"]
        return User(
            id=row["id"],
            display_name=row["display_name"],
            external_user_id=row["external_user_id"],
            email=row["email"],
            password_hash=None,
            is_admin=row["is_admin"],
            is_active=True,
            file_quota=row["file_quota"],
            file_count=row["file_count"],
            created_at=created,
            updated_at=created,
            partitions=memberships or [],
            managed_by_config=bool(row.get("managed_by_config", False)),
        )

    @staticmethod
    def _row_to_user_partition(row: asyncpg.Record) -> UserPartition:
        return UserPartition(
            user_id=row["user_id"],
            partition=row["partition_name"],
            role=PartitionRole(row["role"]),
            added_at=row["added_at"],
        )


__all__ = ["PgUserRepository", "_hash_token"]
