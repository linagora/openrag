"""``PgUserRepository.ensure_seed_users`` against a real Postgres (#1153)."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from core.config.auth import SeedUserConfig
from core.models.user import User
from loguru import logger
from services.auth.session_tokens import hash_session_token
from services.orchestrators.auth_service import AuthService
from services.persistence.user_repo import _hash_token
from services.storage.postgres_store import PostgresStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

ADMIN_TOKEN = "admin-token-0123456789"
TOKEN_A = "svc-a-token-0123456789abcdef"
TOKEN_A2 = "svc-a-token-rotated-fedcba9876"
TOKEN_B = "svc-b-token-0123456789abcdef"


def _seed(external_user_id: str = "svc-a", token_env: str = "SVC_A_TOKEN", **overrides) -> SeedUserConfig:
    fields = {
        "external_user_id": external_user_id,
        "display_name": external_user_id,
        "token_env": token_env,
        "partitions": [{"name": "twake", "role": "editor"}],
    }
    fields.update(overrides)
    return SeedUserConfig(**fields)


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _admin(postgres_store: PostgresStore):
    """Startup order: the admin bootstrap owns users.id = 1 before seeding runs."""
    await postgres_store.user_repo.ensure_admin_user(ADMIN_TOKEN)


@pytest.fixture
def logs():
    records: list[str] = []
    sink = logger.add(lambda message: records.append(str(message) + repr(message.record["extra"])), level="DEBUG")
    yield records
    logger.remove(sink)


async def _row(store: PostgresStore, external_user_id: str):
    return await store.pool.fetchrow("SELECT * FROM users WHERE external_user_id = $1", external_user_id)


async def _memberships(store: PostgresStore, user_id: int) -> dict[str, str]:
    rows = await store.pool.fetch(
        "SELECT partition_name, role FROM partition_memberships WHERE user_id = $1",
        user_id,
    )
    return {r["partition_name"]: r["role"] for r in rows}


async def _partitions(store: PostgresStore, *names: str) -> None:
    for name in names:
        await store.pool.execute("INSERT INTO partitions (partition, created_at) VALUES ($1, NOW())", name)


def _auth_service(store: PostgresStore) -> AuthService:
    from core.config.auth import OIDCConfig

    return AuthService(
        user_repo=store.user_repo,
        oidc_session_repo=store.oidc_session_repo,
        membership_repo=store.membership_repo,
        oidc_client=None,
        config=OIDCConfig(),
    )


class TestCreateAndRotate:
    async def test_creates_a_managed_account_with_its_memberships(self, postgres_store: PostgresStore):
        await _partitions(postgres_store, "twake")
        outcome = await postgres_store.user_repo.ensure_seed_users([_seed()], env={"SVC_A_TOKEN": TOKEN_A})

        assert outcome == {"svc-a": "created"}
        row = await _row(postgres_store, "svc-a")
        assert row["managed_by_config"] is True
        assert row["is_admin"] is False
        assert row["token"] == _hash_token(TOKEN_A)
        assert await _memberships(postgres_store, row["id"]) == {"twake": "editor"}
        user = await _auth_service(postgres_store).get_user_by_token_for_request(TOKEN_A)
        assert user is not None and user["id"] == row["id"]

    async def test_rotation_rewrites_the_hash(self, postgres_store: PostgresStore):
        await _partitions(postgres_store, "twake")
        repo = postgres_store.user_repo
        await repo.ensure_seed_users([_seed()], env={"SVC_A_TOKEN": TOKEN_A})
        outcome = await repo.ensure_seed_users(
            [_seed(display_name="renamed", is_admin=True)], env={"SVC_A_TOKEN": TOKEN_A2}
        )

        assert outcome == {"svc-a": "updated"}
        assert await repo.get_user_by_token(_hash_token(TOKEN_A)) is None
        rotated = await repo.get_user_by_token(_hash_token(TOKEN_A2))
        assert rotated is not None
        assert rotated.display_name == "renamed"
        assert rotated.is_admin is True

    async def test_admin_seed_is_warned(self, postgres_store: PostgresStore, logs):
        await postgres_store.user_repo.ensure_seed_users(
            [_seed(is_admin=True, partitions=[])], env={"SVC_A_TOKEN": TOKEN_A}
        )
        assert any("admin rights" in line for line in logs)

    async def test_concurrent_runs_converge(self, postgres_store: PostgresStore):
        await _partitions(postgres_store, "twake")
        repo = postgres_store.user_repo
        env = {"SVC_A_TOKEN": TOKEN_A}
        results = await asyncio.gather(*(repo.ensure_seed_users([_seed()], env=env) for _ in range(3)))

        assert sorted(r["svc-a"] for r in results) == ["created", "updated", "updated"]
        assert await postgres_store.pool.fetchval("SELECT count(*) FROM users WHERE external_user_id = 'svc-a'") == 1


class TestEnvUnset:
    async def test_existing_account_is_left_untouched(self, postgres_store: PostgresStore, logs):
        await _partitions(postgres_store, "twake")
        repo = postgres_store.user_repo
        await repo.ensure_seed_users([_seed()], env={"SVC_A_TOKEN": TOKEN_A})
        outcome = await repo.ensure_seed_users([_seed(display_name="changed", partitions=[])], env={})

        assert outcome == {"svc-a": "untouched"}
        row = await _row(postgres_store, "svc-a")
        assert row["token"] == _hash_token(TOKEN_A)
        assert row["display_name"] == "svc-a"
        assert await _memberships(postgres_store, row["id"]) == {"twake": "editor"}
        assert any("unset" in line for line in logs)

    async def test_new_account_is_not_created(self, postgres_store: PostgresStore):
        outcome = await postgres_store.user_repo.ensure_seed_users([_seed()], env={"SVC_A_TOKEN": "  "})

        assert outcome == {"svc-a": "skipped"}
        assert await _row(postgres_store, "svc-a") is None


class TestNeverTakesOver:
    async def test_unmanaged_account_with_the_same_external_id_is_skipped(self, postgres_store: PostgresStore, logs):
        await _partitions(postgres_store, "twake")
        # The shape an OIDC login leaves: external_user_id = sub, no token.
        oidc_user = await postgres_store.user_repo.create_user(User(display_name="Alice", external_user_id="svc-a"))
        outcome = await postgres_store.user_repo.ensure_seed_users([_seed()], env={"SVC_A_TOKEN": TOKEN_A})

        assert outcome == {"svc-a": "skipped"}
        row = await _row(postgres_store, "svc-a")
        assert row["id"] == oidc_user.id
        assert row["token"] is None
        assert row["managed_by_config"] is False
        assert await _memberships(postgres_store, row["id"]) == {}
        assert any("already exists" in line for line in logs)

    async def test_admin_row_is_never_touched(self, postgres_store: PostgresStore):
        repo = postgres_store.user_repo
        await postgres_store.pool.execute("UPDATE users SET external_user_id = 'svc-a' WHERE id = 1")
        await postgres_store.pool.execute("UPDATE users SET managed_by_config = TRUE WHERE id = 1")

        outcome = await repo.ensure_seed_users([_seed(partitions=[])], env={"SVC_A_TOKEN": TOKEN_A})
        assert outcome == {"svc-a": "skipped"}
        # Nor revoked when the config no longer lists it.
        await repo.ensure_seed_users([], env={})
        admin = await postgres_store.pool.fetchrow("SELECT token, is_admin FROM users WHERE id = 1")
        assert admin["token"] == _hash_token(ADMIN_TOKEN)
        assert admin["is_admin"] is True

    async def test_token_already_held_by_another_account_is_skipped(self, postgres_store: PostgresStore):
        repo = postgres_store.user_repo
        other = await repo.create_user(User(display_name="Bob"))
        await postgres_store.pool.execute("UPDATE users SET token = $1 WHERE id = $2", _hash_token(TOKEN_A), other.id)

        outcome = await repo.ensure_seed_users([_seed(partitions=[])], env={"SVC_A_TOKEN": TOKEN_A})
        assert outcome == {"svc-a": "skipped"}
        assert await _row(postgres_store, "svc-a") is None


class TestAdminFirst:
    async def test_nothing_is_seeded_before_the_admin_exists(self, postgres_store: PostgresStore):
        await postgres_store.pool.execute("DELETE FROM users WHERE id = 1")
        outcome = await postgres_store.user_repo.ensure_seed_users([_seed(partitions=[])], env={"SVC_A_TOKEN": TOKEN_A})

        assert outcome == {"svc-a": "skipped"}
        assert await postgres_store.pool.fetchval("SELECT count(*) FROM users") == 0


class TestTokenChecks:
    async def test_duplicate_token_rejects_both_entries(self, postgres_store: PostgresStore, logs):
        seeds = [_seed(partitions=[]), _seed("svc-b", "SVC_B_TOKEN", partitions=[])]
        outcome = await postgres_store.user_repo.ensure_seed_users(
            seeds, env={"SVC_A_TOKEN": TOKEN_A, "SVC_B_TOKEN": TOKEN_A}
        )

        assert outcome == {"svc-a": "skipped", "svc-b": "skipped"}
        assert await postgres_store.pool.fetchval("SELECT count(*) FROM users WHERE id <> 1") == 0
        assert any("shared by several seed users" in line for line in logs)

    async def test_admin_token_is_rejected(self, postgres_store: PostgresStore):
        outcome = await postgres_store.user_repo.ensure_seed_users(
            [_seed(partitions=[])], env={"SVC_A_TOKEN": TOKEN_A, "AUTH_TOKEN": TOKEN_A}
        )
        assert outcome == {"svc-a": "skipped"}

    @pytest.mark.parametrize("weak", ["weak-tk1", "or-openrag-1234"])
    async def test_weak_token_is_rejected(self, postgres_store: PostgresStore, weak):
        outcome = await postgres_store.user_repo.ensure_seed_users([_seed(partitions=[])], env={"SVC_A_TOKEN": weak})
        assert outcome == {"svc-a": "skipped"}

    async def test_allow_insecure_secrets_accepts_a_weak_token(self, postgres_store: PostgresStore):
        outcome = await postgres_store.user_repo.ensure_seed_users(
            [_seed(partitions=[])], env={"SVC_A_TOKEN": "weak-tk1", "ALLOW_INSECURE_SECRETS": "true"}
        )
        assert outcome == {"svc-a": "created"}

    async def test_a_skipped_entry_does_not_block_the_others(self, postgres_store: PostgresStore):
        seeds = [_seed(partitions=[]), _seed("svc-b", "SVC_B_TOKEN", partitions=[])]
        outcome = await postgres_store.user_repo.ensure_seed_users(
            seeds, env={"SVC_A_TOKEN": "weak-tk1", "SVC_B_TOKEN": TOKEN_B}
        )
        assert outcome == {"svc-a": "skipped", "svc-b": "created"}

    async def test_no_log_line_carries_a_token_or_its_hash(self, postgres_store: PostgresStore, logs):
        await _partitions(postgres_store, "twake")
        repo = postgres_store.user_repo
        other = await repo.create_user(User(display_name="Bob"))
        await postgres_store.pool.execute("UPDATE users SET token = $1 WHERE id = $2", _hash_token(TOKEN_B), other.id)
        seeds = [
            _seed(is_admin=True, partitions=[{"name": "twake", "role": "owner"}, {"name": "gone", "role": "viewer"}]),
            _seed("svc-b", "SVC_B_TOKEN"),
            _seed("svc-c", "SVC_C_TOKEN"),
            _seed("svc-d", "SVC_D_TOKEN"),
        ]
        env = {"SVC_A_TOKEN": TOKEN_A, "SVC_B_TOKEN": TOKEN_B, "SVC_C_TOKEN": "weak-tk1", "AUTH_TOKEN": TOKEN_A2}
        env["SVC_D_TOKEN"] = TOKEN_A2
        await repo.ensure_seed_users(seeds, env=env)
        await repo.ensure_seed_users([], env={})

        assert logs
        joined = "\n".join(logs)
        for secret in (TOKEN_A, TOKEN_A2, TOKEN_B, "weak-tk1"):
            assert secret not in joined
            assert _hash_token(secret) not in joined


class TestMemberships:
    async def test_unlisted_memberships_are_pruned_and_roles_updated(self, postgres_store: PostgresStore):
        await _partitions(postgres_store, "twake", "drive")
        repo = postgres_store.user_repo
        env = {"SVC_A_TOKEN": TOKEN_A}
        both = [{"name": "twake", "role": "editor"}, {"name": "drive", "role": "viewer"}]
        await repo.ensure_seed_users([_seed(partitions=both)], env=env)
        await repo.ensure_seed_users([_seed(partitions=[{"name": "twake", "role": "owner"}])], env=env)

        row = await _row(postgres_store, "svc-a")
        assert await _memberships(postgres_store, row["id"]) == {"twake": "owner"}

    async def test_missing_partition_is_skipped_not_created(self, postgres_store: PostgresStore, logs):
        await _partitions(postgres_store, "twake")
        partitions = [{"name": "twake", "role": "editor"}, {"name": "nowhere", "role": "viewer"}]
        outcome = await postgres_store.user_repo.ensure_seed_users(
            [_seed(partitions=partitions)], env={"SVC_A_TOKEN": TOKEN_A}
        )

        assert outcome == {"svc-a": "created"}
        row = await _row(postgres_store, "svc-a")
        assert await _memberships(postgres_store, row["id"]) == {"twake": "editor"}
        assert await postgres_store.pool.fetchval("SELECT count(*) FROM partitions WHERE partition = 'nowhere'") == 0
        assert any("does not exist" in line for line in logs)


class TestRemoval:
    async def test_removed_account_is_revoked_not_deleted(self, postgres_store: PostgresStore, logs):
        await _partitions(postgres_store, "twake")
        repo = postgres_store.user_repo
        await repo.ensure_seed_users([_seed(is_admin=True)], env={"SVC_A_TOKEN": TOKEN_A})
        outcome = await repo.ensure_seed_users([], env={"SVC_A_TOKEN": TOKEN_A})

        assert outcome == {"svc-a": "revoked"}
        row = await _row(postgres_store, "svc-a")
        assert row is not None
        assert row["token"] is None
        assert row["is_admin"] is False
        assert await _memberships(postgres_store, row["id"]) == {}
        assert any("revoked" in line for line in logs)

    async def test_auth_path_rejects_a_revoked_account(self, postgres_store: PostgresStore):
        repo = postgres_store.user_repo
        auth = _auth_service(postgres_store)
        await repo.ensure_seed_users([_seed(partitions=[])], env={"SVC_A_TOKEN": TOKEN_A})
        assert await auth.get_user_by_token_for_request(TOKEN_A) is not None

        await repo.ensure_seed_users([], env={})

        assert await auth.get_user_by_token_for_request(TOKEN_A) is None
        assert await repo.get_user_by_token(hash_session_token(TOKEN_A)) is None

    async def test_relisting_a_revoked_account_restores_it(self, postgres_store: PostgresStore):
        repo = postgres_store.user_repo
        await repo.ensure_seed_users([_seed(partitions=[])], env={"SVC_A_TOKEN": TOKEN_A})
        await repo.ensure_seed_users([], env={})
        outcome = await repo.ensure_seed_users([_seed(partitions=[])], env={"SVC_A_TOKEN": TOKEN_A2})

        assert outcome == {"svc-a": "updated"}
        assert await repo.get_user_by_token(_hash_token(TOKEN_A2)) is not None

    async def test_token_moved_from_a_removed_account_to_a_new_one(self, postgres_store: PostgresStore):
        repo = postgres_store.user_repo
        await repo.ensure_seed_users([_seed(partitions=[])], env={"SVC_A_TOKEN": TOKEN_A})
        outcome = await repo.ensure_seed_users(
            [_seed("svc-b", "SVC_B_TOKEN", partitions=[])], env={"SVC_B_TOKEN": TOKEN_A}
        )

        assert outcome == {"svc-a": "revoked", "svc-b": "created"}
        moved = await repo.get_user_by_token(_hash_token(TOKEN_A))
        assert moved is not None and moved.external_user_id == "svc-b"

    async def test_an_entry_skipped_for_its_token_is_not_revoked(self, postgres_store: PostgresStore):
        repo = postgres_store.user_repo
        await repo.ensure_seed_users([_seed(partitions=[])], env={"SVC_A_TOKEN": TOKEN_A})
        outcome = await repo.ensure_seed_users([_seed(partitions=[])], env={"SVC_A_TOKEN": "weak-tk1"})

        assert outcome == {"svc-a": "skipped"}
        assert (await _row(postgres_store, "svc-a"))["token"] == _hash_token(TOKEN_A)
