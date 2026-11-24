"""Reserved partition names against a real Postgres: never created on first use,
never a membership of a request."""

from __future__ import annotations

import pytest
from core.config.auth import OIDCConfig
from core.models.user import User
from core.utils.exceptions import ValidationError
from services.orchestrators.auth_service import AuthService
from services.storage.postgres_store import PostgresStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def _user(store: PostgresStore) -> int:
    user = await store.user_repo.create_user(User(display_name="Bob", is_admin=False))
    return user.id


async def _rows(store: PostgresStore, partition: str) -> tuple[int, int, int]:
    pool = store.document_repo.pool
    return (
        await pool.fetchval("SELECT count(*) FROM partitions WHERE partition = $1", partition),
        await pool.fetchval("SELECT count(*) FROM files WHERE partition_name = $1", partition),
        await pool.fetchval("SELECT count(*) FROM partition_memberships WHERE partition_name = $1", partition),
    )


@pytest.mark.parametrize("partition", ["all", "ALL", " all "])
async def test_a_file_never_creates_a_reserved_partition(postgres_store: PostgresStore, partition: str):
    user_id = await _user(postgres_store)

    with pytest.raises(ValidationError, match="reserved") as exc:
        await postgres_store.document_repo.add_file_to_partition(file_id="f1", partition=partition, user_id=user_id)
    assert (exc.value.status_code, exc.value.code) == (400, "RESERVED_PARTITION_NAME")

    assert await _rows(postgres_store, partition) == (0, 0, 0)


async def test_a_file_creates_a_missing_partition_with_its_owner(postgres_store: PostgresStore):
    user_id = await _user(postgres_store)

    assert await postgres_store.document_repo.add_file_to_partition(file_id="f1", partition="team", user_id=user_id)

    assert await _rows(postgres_store, "team") == (1, 1, 1)


@pytest.mark.parametrize("partition", ["all", "All"])
async def test_a_member_never_creates_a_reserved_partition(postgres_store: PostgresStore, partition: str):
    user_id = await _user(postgres_store)

    with pytest.raises(ValidationError, match="reserved") as exc:
        await postgres_store.membership_repo.add_partition_member(partition, user_id, "owner")
    assert (exc.value.status_code, exc.value.code) == (400, "RESERVED_PARTITION_NAME")

    assert await _rows(postgres_store, partition) == (0, 0, 0)


async def test_a_member_creates_a_missing_partition_as_its_owner(postgres_store: PostgresStore):
    user_id = await _user(postgres_store)

    assert await postgres_store.membership_repo.add_partition_member("team", user_id, "owner")

    assert await _rows(postgres_store, "team") == (1, 0, 1)


async def test_a_stored_reserved_membership_is_not_a_partition_of_the_request(postgres_store: PostgresStore):
    """Rows written before the name was reserved stay in the database."""
    user_id = await _user(postgres_store)
    pool = postgres_store.document_repo.pool
    for partition in ("all", "team"):
        await pool.execute("INSERT INTO partitions (partition, created_at) VALUES ($1, NOW())", partition)
        await pool.execute(
            "INSERT INTO partition_memberships (partition_name, user_id, role, added_at) VALUES ($1, $2, 'owner', NOW())",
            partition,
            user_id,
        )
    auth = AuthService(
        user_repo=postgres_store.user_repo,
        oidc_session_repo=postgres_store.oidc_session_repo,
        membership_repo=postgres_store.membership_repo,
        oidc_client=None,
        config=OIDCConfig(),
    )

    partitions = await auth.list_user_partitions_for_request(user_id)

    assert [(p["partition"], p["role"]) for p in partitions] == [("team", "owner")]
