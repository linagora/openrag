"""Postgres coverage for atomic indexing admission."""

import asyncio

import asyncpg
import pytest
import pytest_asyncio
from core.models.catalog import FileFenceKind, IndexationJob
from core.utils.exceptions import ConflictError, OpenRAGError
from cryptography.fernet import Fernet, InvalidToken
from services.persistence.queue_callback import queue_callback_cipher

from .test_file_quota_reservations import _create_user, _file_count, _reservations

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(loop_scope="session")
async def submission(postgres_store):
    partition = "queue-admission"
    await postgres_store.partition_repo.create_partition(partition)
    user_id = await _create_user(postgres_store, file_quota=2)
    return IndexationJob(partition=partition, file_id="file", user_id=user_id, lane="cpu", object_key="uploads/file")


async def test_enqueue_rolls_back_quota_and_fence_if_insert_fails(postgres_store, submission):
    # PostgreSQL rejects a NUL in a text value during the INSERT. The filename
    # is first sent to the database after taking the quota slot and the fence.
    invalid_job = submission.model_copy(update={"filename": "invalid\x00filename"})
    with pytest.raises(asyncpg.CharacterNotInRepertoireError):
        await postgres_store.job_repo.enqueue(invalid_job, default_quota=-1)

    assert await postgres_store.job_repo.get_job(submission.id) is None
    assert await postgres_store.file_fence_repo.get(partition=submission.partition, file_id=submission.file_id) is None
    assert await _file_count(postgres_store, submission.user_id) == 0
    assert await _reservations(postgres_store, submission.user_id) == 0


async def test_concurrent_retries_return_the_same_job_and_reserve_one_slot(postgres_store, submission):
    async def enqueue():
        return await postgres_store.job_repo.enqueue(submission, default_quota=-1)

    rows = await asyncio.gather(*(enqueue() for _ in range(4)))
    assert rows == [rows[0]] * 4
    assert rows[0].available_at is not None
    assert await _reservations(postgres_store, submission.user_id) == 1
    assert await _file_count(postgres_store, submission.user_id) == 1
    fence = await postgres_store.file_fence_repo.get(partition=submission.partition, file_id=submission.file_id)
    assert fence.holder_id == submission.id

    with pytest.raises(ConflictError, match="INDEXING_JOB_ID_CONFLICT"):
        await postgres_store.job_repo.enqueue(submission.model_copy(update={"lane": "gpu"}), default_quota=-1)


@pytest.mark.parametrize(
    ("kind", "replace", "code"),
    [
        (FileFenceKind.INDEX, False, "DOCUMENT_INDEXING_IN_PROGRESS"),
        (FileFenceKind.DELETE, True, "DOCUMENT_DELETE_IN_PROGRESS"),
        (FileFenceKind.METADATA, True, "DOCUMENT_METADATA_UPDATE_IN_PROGRESS"),
    ],
)
async def test_enqueue_refuses_incompatible_fence(postgres_store, submission, kind, replace, code):
    await postgres_store.file_fence_repo.acquire(
        partition=submission.partition, file_id=submission.file_id, kind=kind, holder_id="other", ttl_seconds=120
    )
    with pytest.raises(ConflictError, match=code):
        await postgres_store.job_repo.enqueue(submission, default_quota=-1, replace=replace)
    assert await postgres_store.job_repo.get_job(submission.id) is None
    assert await _reservations(postgres_store, submission.user_id) == 0
    assert await _file_count(postgres_store, submission.user_id) == 0


async def test_replacement_queues_behind_index_holder_without_reserving_quota(postgres_store, submission):
    await postgres_store.file_fence_repo.acquire(
        partition=submission.partition,
        file_id=submission.file_id,
        kind=FileFenceKind.INDEX,
        holder_id="other",
        ttl_seconds=120,
    )
    row = await postgres_store.job_repo.enqueue(submission, default_quota=-1, replace=True)
    assert row.status == "QUEUED"
    assert await _reservations(postgres_store, submission.user_id) == 0
    fence = await postgres_store.file_fence_repo.get(partition=submission.partition, file_id=submission.file_id)
    assert fence.holder_id == "other"


async def test_unfinished_job_cap_applies_to_admin_and_unlimited_users(postgres_store, submission):
    admin = await _create_user(postgres_store, is_admin=True)
    first = submission.model_copy(update={"user_id": admin})
    second = first.model_copy(update={"id": "second", "file_id": "other"})
    await postgres_store.job_repo.enqueue(first, default_quota=-1, max_unfinished_per_user=1)
    with pytest.raises(OpenRAGError, match="INDEXING_QUEUE_LIMIT_REACHED") as refusal:
        await postgres_store.job_repo.enqueue(second, default_quota=-1, max_unfinished_per_user=1)
    assert refusal.value.status_code == 429
    assert refusal.value.extra["retry_after"] == 60
    assert await postgres_store.job_repo.get_job(second.id) is None
    assert await _reservations(postgres_store, admin) == 0
    await postgres_store.job_repo.enqueue(second, default_quota=-1)


async def test_quota_refusal_leaves_no_job_or_fence(postgres_store, submission):
    user_id = await _create_user(postgres_store, file_quota=0)
    job = submission.model_copy(update={"user_id": user_id})
    with pytest.raises(OpenRAGError, match="FILE_QUOTA_EXCEEDED") as refusal:
        await postgres_store.job_repo.enqueue(job, default_quota=-1)
    assert refusal.value.status_code == 403
    assert await postgres_store.job_repo.get_job(job.id) is None
    assert await postgres_store.file_fence_repo.get(partition=job.partition, file_id=job.file_id) is None
    assert await _reservations(postgres_store, user_id) == 0


async def test_callback_tokens_use_dedicated_key_and_support_rotation(postgres_store, submission, monkeypatch):
    old_key, new_key, oidc_key = (Fernet.generate_key().decode() for _ in range(3))
    monkeypatch.setenv("INDEXING_SECRETS_KEY", old_key)
    monkeypatch.setenv("OIDC_TOKEN_ENCRYPTION_KEY", oidc_key)
    stored = await postgres_store.job_repo.enqueue(submission, default_quota=-1, callback_token="callback-secret")
    assert "callback-secret" not in str(stored.payload)
    ciphertext = stored.payload["callback_token_encrypted"].encode()
    assert Fernet(old_key).decrypt(ciphertext) == b"callback-secret"
    with pytest.raises(InvalidToken):
        Fernet(oidc_key).decrypt(ciphertext)

    monkeypatch.setenv("INDEXING_SECRETS_KEY", f"{new_key},{old_key}")
    replay = await postgres_store.job_repo.enqueue(submission, default_quota=-1, callback_token="callback-secret")
    assert replay == stored
    cipher = queue_callback_cipher()
    assert Fernet(new_key).decrypt(cipher.encrypt(b"new-token")) == b"new-token"
    assert cipher.decrypt(Fernet(oidc_key).encrypt(b"legacy-token")) == b"legacy-token"
    with pytest.raises(ConflictError, match="INDEXING_JOB_ID_CONFLICT"):
        await postgres_store.job_repo.enqueue(submission, default_quota=-1, callback_token="different-secret")
