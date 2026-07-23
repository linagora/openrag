import consts
from api.dependencies.auth import SUPER_ADMIN_MODE
from fastapi import HTTPException, status
from openai import AsyncOpenAI


async def get_openai_models(base_url: str, api_key: str, timeout: float = 30):
    async with AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout) as client:
        models_response = await client.models.list()
        return models_response.data


async def get_partition_name(
    model_name,
    user_partitions,
    *,
    partition_service,
    is_admin=False,
):
    partition_prefix = consts.PARTITION_PREFIX
    if model_name.startswith(consts.LEGACY_PARTITION_PREFIX):
        partition_prefix = consts.LEGACY_PARTITION_PREFIX

    if not model_name.startswith(partition_prefix):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model not found. Model should respect this format: {consts.PARTITION_PREFIX}partition_name",
        )
    partition = model_name.split(partition_prefix)[1]
    if partition != "all" and not await partition_service.partition_exists(partition):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Partition `{partition}` not found for given model `{model_name}`",
        )
    if partition != "all" and partition not in user_partitions and not (is_admin and SUPER_ADMIN_MODE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access to model `{model_name}` is forbidden for the current user",
        )
    if partition == "all" and not (is_admin and SUPER_ADMIN_MODE):
        if not user_partitions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No accessible partitions",
            )
        return user_partitions
    return [partition]


def truncate(value: str, max_chars: int = 1000) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + f"... [truncated {len(value) - max_chars} chars]"
