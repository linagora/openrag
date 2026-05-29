import consts
import openai
from api.dependencies.auth import SUPER_ADMIN_MODE
from core.utils.logging import get_logger
from di.providers import get_config
from fastapi import Depends, HTTPException, status
from openai import AsyncOpenAI

logger = get_logger()


async def get_openai_models(base_url: str, api_key: str, timeout: int = 30):
    async with AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout) as client:
        models_response = await client.models.list()
        return models_response.data


async def check_llm_model_availability(config=Depends(get_config)):
    llm_param = config.llm
    base_url = llm_param.base_url
    model = llm_param.model
    api_key = llm_param.api_key

    missing = [k for k, v in {"base_url": base_url, "model": model, "api_key": api_key}.items() if not v]
    if missing:
        logger.error("Incomplete LLM configuration", missing_fields=missing)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="LLM configuration is incomplete",
        )

    log = logger.bind(base_url=base_url, model=model)

    try:
        log.debug("Validating model")
        timeout = int(llm_param.timeout)
        openai_models = await get_openai_models(base_url=base_url, api_key=api_key, timeout=timeout)
        available_models = {m.id for m in openai_models}
        if model not in available_models:
            available_str = ", ".join(available_models) if available_models else "(none)"
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"The underlying model '{model}' is not available via this endpoint. Available models: {available_str}",
            )

    except HTTPException:
        raise

    except openai.APITimeoutError as e:
        log.warning("Model availability check timed out", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Service timed out",
        )

    except openai.APIConnectionError as e:
        log.warning("Model availability check failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service is unavailable",
        )

    except openai.APIError as e:
        log.error("API Endpoint error while validating model", error=str(e))
        status_code = getattr(e, "status_code", None) or status.HTTP_500_INTERNAL_SERVER_ERROR
        raise HTTPException(status_code=status_code, detail="Upstream LLM service error")

    except Exception as e:
        log.exception("Failed to validate model", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


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
        return user_partitions
    return [partition]


def truncate(value: str, max_chars: int = 1000) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + f"... [truncated {len(value) - max_chars} chars]"
