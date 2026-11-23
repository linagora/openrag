import pytest
from api.dependencies.llm import get_partition_name
from core.utils.consts import LEGACY_PARTITION_PREFIX, PARTITION_PREFIX
from fastapi import HTTPException


class FakePartitionService:
    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing = existing or set()

    async def partition_exists(self, partition: str) -> bool:
        return partition in self.existing


@pytest.mark.asyncio
async def test_get_partition_name_all_fails_closed_for_user_without_partitions():
    with pytest.raises(HTTPException) as exc:
        await get_partition_name(
            "openrag-all",
            [],
            partition_service=FakePartitionService(),
            is_admin=False,
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "No accessible partitions"


@pytest.mark.asyncio
async def test_get_partition_name_all_uses_accessible_partitions():
    partitions = await get_partition_name(
        "openrag-all",
        ["legal", "finance"],
        partition_service=FakePartitionService(),
        is_admin=False,
    )

    assert partitions == ["legal", "finance"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", [PARTITION_PREFIX, LEGACY_PARTITION_PREFIX])
async def test_get_partition_name_retains_prefix_in_partition_name(prefix):
    partition = f"finance-{prefix}archive"
    partitions = await get_partition_name(
        f"{prefix}{partition}",
        [partition],
        partition_service=FakePartitionService({partition}),
    )

    assert partitions == [partition]
