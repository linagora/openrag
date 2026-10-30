"""File-slot reservations against a real Postgres (#664).

The invariant every test checks: ``users.file_count`` equals the user's files
plus their outstanding reservations, whatever order reserve, consume, release
and sweep run in.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
import pytest_asyncio
from core.config.infrastructure import RDBConfig
from core.models.user import FileSlotGrant
from services.persistence.user_repo import PgUserRepository
from services.storage.postgres_store import PostgresStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

_BURST = 20


@pytest_asyncio.fixture(loop_scope="session")
async def wide_user_repo(test_rdb_config: RDBConfig, postgres_store: PostgresStore):
    """A user repo whose pool can hold a whole burst of admissions at once.

    The shared store's pool is capped at four connections, which would queue
    most of a burst in the pool instead of on the row lock under test.
    """
    pool = await asyncpg.create_pool(
        host=test_rdb_config.host,
        port=test_rdb_config.port,
        user=test_rdb_config.user,
        password=test_rdb_config.password,
        database=test_rdb_config.database,
        min_size=_BURST,
        max_size=_BURST,
    )
    try:
        yield PgUserRepository(lambda: pool)
    finally:
        await pool.close()


async def _create_user(
    store: PostgresStore,
    *,
    file_quota: int | None = None,
    is_admin: bool = False,
    file_count: int = 0,
) -> int:
    user = await store.user_repo.create_legacy_user(
        display_name=f"quota-{uuid.uuid4().hex[:8]}",
        external_user_id=None,
        email=None,
        is_admin=is_admin,
        file_quota=file_quota,
    )
    if file_count:
        await store.pool.execute("UPDATE users SET file_count = $1 WHERE id = $2", file_count, user["id"])
    return user["id"]


async def _file_count(store: PostgresStore, user_id: int) -> int:
    return await store.pool.fetchval("SELECT file_count FROM users WHERE id = $1", user_id)


async def _reservations(store: PostgresStore, user_id: int) -> int:
    return await store.user_repo.count_file_slot_reservations(user_id)


async def _assert_counter_matches(store: PostgresStore, user_id: int, *, baseline: int = 0) -> None:
    """``file_count`` is the files created since ``baseline`` plus held slots."""
    files = await store.pool.fetchval("SELECT COUNT(*)::int FROM files WHERE created_by = $1", user_id)
    held = await _reservations(store, user_id)
    assert await _file_count(store, user_id) == baseline + files + held


async def _add_file(store: PostgresStore, file_id: str, user_id: int, reservation_id: str | None) -> bool:
    if not await store.partition_repo.partition_exists("quota"):
        await store.partition_repo.create_partition("quota")
    return await store.document_repo.add_file_to_partition(
        file_id,
        "quota",
        user_id=user_id,
        quota_reservation_id=reservation_id,
    )


async def _burst(repo: PgUserRepository, user_id: int, *, default_quota: int) -> list[FileSlotGrant]:
    """Admit ``_BURST`` uploads at once, all queued on the user's row lock."""
    ids = [uuid.uuid4().hex for _ in range(_BURST)]
    gate = asyncio.Event()

    async def admit(reservation_id: str) -> FileSlotGrant:
        await gate.wait()
        return await repo.try_reserve_file_slot(user_id, reservation_id, default_quota=default_quota)

    tasks = [asyncio.create_task(admit(reservation_id)) for reservation_id in ids]
    await asyncio.sleep(0)
    gate.set()
    return await asyncio.gather(*tasks)


# ── Admission ────────────────────────────────────────────────────────


@pytest.mark.parametrize("quota", [0, 1, 5, _BURST - 1])
async def test_a_burst_admits_exactly_the_free_slots(postgres_store, wide_user_repo, quota):
    user_id = await _create_user(postgres_store, file_quota=quota)

    grants = await _burst(wide_user_repo, user_id, default_quota=-1)

    assert grants.count(FileSlotGrant.RESERVED) == quota
    assert grants.count(FileSlotGrant.DENIED) == _BURST - quota
    assert await _file_count(postgres_store, user_id) == quota
    assert await _reservations(postgres_store, user_id) == quota


async def test_a_burst_counts_the_files_a_user_already_has(postgres_store, wide_user_repo):
    """An existing user's count is already right; only what is left is admitted."""
    user_id = await _create_user(postgres_store, file_quota=5, file_count=3)

    grants = await _burst(wide_user_repo, user_id, default_quota=-1)

    assert grants.count(FileSlotGrant.RESERVED) == 2
    assert await _file_count(postgres_store, user_id) == 5
    await _assert_counter_matches(postgres_store, user_id, baseline=3)


async def test_a_burst_under_the_default_quota_admits_exactly_the_free_slots(postgres_store, wide_user_repo):
    user_id = await _create_user(postgres_store, file_quota=None)

    grants = await _burst(wide_user_repo, user_id, default_quota=3)

    assert grants.count(FileSlotGrant.RESERVED) == 3
    assert await _file_count(postgres_store, user_id) == 3


async def test_a_user_over_quota_is_denied_and_left_untouched(postgres_store):
    """An admin can lower a quota below what a user already holds."""
    user_id = await _create_user(postgres_store, file_quota=2, file_count=4)

    grant = await postgres_store.user_repo.try_reserve_file_slot(user_id, "r-1", default_quota=-1)

    assert grant is FileSlotGrant.DENIED
    assert await _file_count(postgres_store, user_id) == 4
    assert await _reservations(postgres_store, user_id) == 0


@pytest.mark.parametrize(
    ("file_quota", "is_admin", "default_quota"),
    [
        (-1, False, 10),  # explicit unlimited
        (None, False, -1),  # unlimited by default
        (0, True, 0),  # admins bypass any quota
    ],
    ids=["explicit-unlimited", "default-unlimited", "admin"],
)
async def test_unlimited_users_reserve_nothing(postgres_store, wide_user_repo, file_quota, is_admin, default_quota):
    user_id = await _create_user(postgres_store, file_quota=file_quota, is_admin=is_admin, file_count=7)

    grants = await _burst(wide_user_repo, user_id, default_quota=default_quota)

    assert set(grants) == {FileSlotGrant.UNLIMITED}
    assert await _file_count(postgres_store, user_id) == 7
    assert await _reservations(postgres_store, user_id) == 0


async def test_a_negative_default_does_not_lift_an_explicit_limit(postgres_store):
    user_id = await _create_user(postgres_store, file_quota=1)
    repo = postgres_store.user_repo

    assert await repo.try_reserve_file_slot(user_id, "r-1", default_quota=-1) is FileSlotGrant.RESERVED
    assert await repo.try_reserve_file_slot(user_id, "r-2", default_quota=-1) is FileSlotGrant.DENIED


async def test_an_unknown_user_is_denied(postgres_store):
    assert await postgres_store.user_repo.try_reserve_file_slot(999, "r-1", default_quota=-1) is FileSlotGrant.DENIED


async def test_a_reused_reservation_id_takes_no_second_slot(postgres_store):
    user_id = await _create_user(postgres_store, file_quota=5)
    repo = postgres_store.user_repo
    await repo.try_reserve_file_slot(user_id, "r-1", default_quota=-1)

    with pytest.raises(asyncpg.UniqueViolationError):
        await repo.try_reserve_file_slot(user_id, "r-1", default_quota=-1)

    assert await _file_count(postgres_store, user_id) == 1


# ── Release ──────────────────────────────────────────────────────────


async def test_release_gives_the_slot_back_exactly_once(postgres_store):
    user_id = await _create_user(postgres_store, file_quota=1)
    repo = postgres_store.user_repo
    await repo.try_reserve_file_slot(user_id, "r-1", default_quota=-1)

    assert await repo.release_file_slot("r-1") is True
    assert await repo.release_file_slot("r-1") is False

    assert await _file_count(postgres_store, user_id) == 0
    assert await repo.try_reserve_file_slot(user_id, "r-2", default_quota=-1) is FileSlotGrant.RESERVED


async def test_concurrent_releases_of_one_slot_give_back_one(postgres_store, wide_user_repo):
    user_id = await _create_user(postgres_store, file_quota=5, file_count=2)
    await postgres_store.user_repo.try_reserve_file_slot(user_id, "r-1", default_quota=-1)

    released = await asyncio.gather(*(wide_user_repo.release_file_slot("r-1") for _ in range(_BURST)))

    assert released.count(True) == 1
    assert await _file_count(postgres_store, user_id) == 2


async def test_releasing_an_unknown_reservation_changes_nothing(postgres_store):
    user_id = await _create_user(postgres_store, file_quota=5, file_count=3)

    assert await postgres_store.user_repo.release_file_slot("never-reserved") is False
    assert await _file_count(postgres_store, user_id) == 3


async def test_deleting_the_user_drops_their_reservations(postgres_store):
    user_id = await _create_user(postgres_store, file_quota=5)
    await postgres_store.user_repo.try_reserve_file_slot(user_id, "r-1", default_quota=-1)

    await postgres_store.user_repo.delete_user(user_id)

    assert await postgres_store.pool.fetchval("SELECT COUNT(*)::int FROM file_quota_reservations") == 0


# ── Consume ──────────────────────────────────────────────────────────


async def test_the_catalog_insert_consumes_the_reservation(postgres_store):
    user_id = await _create_user(postgres_store, file_quota=1)
    repo = postgres_store.user_repo
    await repo.try_reserve_file_slot(user_id, "r-1", default_quota=-1)

    assert await _add_file(postgres_store, "doc-1", user_id, "r-1") is True

    assert await _file_count(postgres_store, user_id) == 1
    assert await _reservations(postgres_store, user_id) == 0
    # A cleanup path that runs after the file landed must not hand it back.
    assert await repo.release_file_slot("r-1") is False
    assert await _file_count(postgres_store, user_id) == 1


async def test_a_duplicate_insert_leaves_the_reservation_to_be_released(postgres_store):
    user_id = await _create_user(postgres_store, file_quota=5)
    repo = postgres_store.user_repo
    await _add_file(postgres_store, "doc-1", user_id, None)
    await repo.try_reserve_file_slot(user_id, "r-1", default_quota=-1)

    assert await _add_file(postgres_store, "doc-1", user_id, "r-1") is False
    assert await _reservations(postgres_store, user_id) == 1

    assert await repo.release_file_slot("r-1") is True
    assert await _file_count(postgres_store, user_id) == 1


@pytest.mark.parametrize("reservation_id", [None, "released"], ids=["never-reserved", "already-released"])
async def test_an_insert_without_a_held_slot_counts_the_file(postgres_store, reservation_id):
    """Unlimited uploads, MCP tools, and a slot released early still get counted."""
    user_id = await _create_user(postgres_store, file_quota=5)
    repo = postgres_store.user_repo
    if reservation_id is not None:
        await repo.try_reserve_file_slot(user_id, reservation_id, default_quota=-1)
        await repo.release_file_slot(reservation_id)

    assert await _add_file(postgres_store, "doc-1", user_id, reservation_id) is True

    assert await _file_count(postgres_store, user_id) == 1
    await _assert_counter_matches(postgres_store, user_id)


async def test_a_reservation_held_by_another_user_is_not_consumed(postgres_store):
    owner = await _create_user(postgres_store, file_quota=5)
    other = await _create_user(postgres_store, file_quota=5)
    await postgres_store.user_repo.try_reserve_file_slot(owner, "r-1", default_quota=-1)

    assert await _add_file(postgres_store, "doc-1", other, "r-1") is True

    await _assert_counter_matches(postgres_store, owner)
    await _assert_counter_matches(postgres_store, other)


async def test_consume_racing_release_keeps_the_counter_true(postgres_store):
    """Whichever wins, the slot is either consumed or released, never both."""
    user_id = await _create_user(postgres_store, file_quota=100)
    repo = postgres_store.user_repo
    for attempt in range(10):
        reservation_id = f"r-{attempt}"
        await repo.try_reserve_file_slot(user_id, reservation_id, default_quota=-1)
        await asyncio.gather(
            _add_file(postgres_store, f"doc-{attempt}", user_id, reservation_id),
            repo.release_file_slot(reservation_id),
        )
        await _assert_counter_matches(postgres_store, user_id)


async def test_deleting_a_consumed_file_frees_its_slot(postgres_store):
    user_id = await _create_user(postgres_store, file_quota=1)
    repo = postgres_store.user_repo
    await repo.try_reserve_file_slot(user_id, "r-1", default_quota=-1)
    await _add_file(postgres_store, "doc-1", user_id, "r-1")

    assert await postgres_store.document_repo.remove_file_from_partition("doc-1", "quota") is True

    assert await _file_count(postgres_store, user_id) == 0
    assert await repo.try_reserve_file_slot(user_id, "r-2", default_quota=-1) is FileSlotGrant.RESERVED


# ── Sweep ────────────────────────────────────────────────────────────


async def test_the_sweep_keeps_live_and_recent_slots_and_releases_the_rest(postgres_store):
    user_id = await _create_user(postgres_store, file_quota=10)
    repo = postgres_store.user_repo
    for reservation_id in ("live-old", "settled-new", "orphan-old", "pending-new"):
        await repo.try_reserve_file_slot(user_id, reservation_id, default_quota=-1)
    await postgres_store.pool.execute(
        "UPDATE file_quota_reservations SET created_at = now() - interval '1 hour' WHERE id LIKE '%-old'"
    )

    released = await repo.release_stale_file_slots(
        live_ids=["live-old"],
        settled_ids=["settled-new"],
        created_before=datetime.now(UTC) - timedelta(minutes=5),
    )

    assert released == 2
    remaining = await postgres_store.pool.fetch("SELECT id FROM file_quota_reservations ORDER BY id")
    assert [row["id"] for row in remaining] == ["live-old", "pending-new"]
    assert await _file_count(postgres_store, user_id) == 2


async def test_the_sweep_decrements_each_user_by_their_own_slots(postgres_store):
    first = await _create_user(postgres_store, file_quota=10, file_count=1)
    second = await _create_user(postgres_store, file_quota=10, file_count=4)
    repo = postgres_store.user_repo
    for index in range(3):
        await repo.try_reserve_file_slot(first, f"first-{index}", default_quota=-1)
    await repo.try_reserve_file_slot(second, "second-0", default_quota=-1)

    released = await repo.release_stale_file_slots(
        live_ids=[],
        settled_ids=[],
        created_before=datetime.now(UTC) + timedelta(seconds=1),
    )

    assert released == 4
    assert await _file_count(postgres_store, first) == 1
    assert await _file_count(postgres_store, second) == 4


async def test_the_sweep_with_nothing_to_release_changes_nothing(postgres_store):
    user_id = await _create_user(postgres_store, file_quota=10, file_count=2)
    await postgres_store.user_repo.try_reserve_file_slot(user_id, "r-1", default_quota=-1)

    released = await postgres_store.user_repo.release_stale_file_slots(
        live_ids=[],
        settled_ids=[],
        created_before=datetime.now(UTC) - timedelta(minutes=5),
    )

    assert released == 0
    assert await _file_count(postgres_store, user_id) == 3
