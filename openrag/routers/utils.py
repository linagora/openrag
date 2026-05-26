import os
from pathlib import Path
from typing import Any

import consts
import openai
from config import load_config
from core.indexing import validators as core_validators
from core.utils.exceptions import OpenRAGError
from di.providers import get_auth_service, get_job_service, get_partition_service
from fastapi import Depends, Form, HTTPException, Request, UploadFile, status
from openai import AsyncOpenAI
from services.orchestrators.auth_service import AuthService
from services.orchestrators.job_service import JobService
from services.orchestrators.partition_service import PartitionService
from utils.logger import get_logger

# load config
config = load_config()
logger = get_logger()

SUPER_ADMIN_MODE = os.getenv("SUPER_ADMIN_MODE", "false").lower() == "true"
DATA_DIR = config.paths.data_dir

FORBIDDEN_CHARS_IN_FILE_ID = set("/")  # set('"<>#%{}|\\^`[]')
LOG_FILE = Path(config.paths.log_dir or "logs") / "app.json"

# supported file formats or mimetypes
ACCEPTED_FILE_FORMATS = config.loader.file_loaders.model_dump().keys()
DICT_MIMETYPES = config.loader.mimetypes.to_dict()

# File quota per user
DEFAULT_FILE_QUOTA = config.rdb.default_file_quota


def current_user(request: Request):
    """Return the authenticated user from request.state"""
    return request.state.user


def current_user_partitions(request: Request):
    """Return the authenticated user's partitions from request.state"""
    return request.state.user_partitions


def current_user_or_admin_partitions(request: Request):
    """Return the authenticated user's partitions from request.state, or all partitions if admin"""
    user = request.state.user
    if user.get("is_admin") and SUPER_ADMIN_MODE:
        return [{"partition": "all", "created_at": 0, "role": "owner"}]
    return request.state.user_partitions


def current_user_or_admin_partitions_list(request: Request):
    """Return the authenticated user's partitions from request.state, or all partitions if admin"""
    return [p["partition"] for p in current_user_or_admin_partitions(request)]


def partitions_with_details(request: Request):
    return current_user_or_admin_partitions(request)


def request_partition(request: Request):
    """Return the partition from path params"""
    return request.path_params.get("partition", None)


def request_partitions(request: Request):
    """Return the partitions from query params"""
    partitions = request.query_params.getlist("partitions")
    return partitions


def request_task_id(request: Request):
    """Return the task_id from path params"""
    return request.path_params.get("task_id", None)


async def ensure_partition_role(
    partition: str,
    user,
    user_partitions,
    required_role: str,
    *,
    auth_service: AuthService,
    partition_service: PartitionService,
):
    """Ensure the user has at least `required_role` for the partition."""
    if SUPER_ADMIN_MODE and user.get("is_admin"):
        return True

    membership = next((p for p in user_partitions if p["partition"] == partition), None)
    if not membership:
        if await partition_service.partition_exists(partition):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access to partition '{partition}' forbidden",
            )
        return True

    try:
        auth_service.check_partition_access(
            user=user,
            partition=partition,
            user_partitions=user_partitions,
            required_role=required_role,
            super_admin_mode=SUPER_ADMIN_MODE,
        )
    except OpenRAGError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.message,
        ) from exc

    return True


async def require_partition_viewer(
    partition=Depends(request_partition),
    user=Depends(current_user),
    user_partitions=Depends(current_user_partitions),
    auth_service: AuthService = Depends(get_auth_service),
    partition_service: PartitionService = Depends(get_partition_service),
):
    await ensure_partition_role(
        partition,
        user,
        user_partitions,
        "viewer",
        auth_service=auth_service,
        partition_service=partition_service,
    )
    return user


async def require_partition_editor(
    partition=Depends(request_partition),
    user=Depends(current_user),
    user_partitions=Depends(current_user_partitions),
    auth_service: AuthService = Depends(get_auth_service),
    partition_service: PartitionService = Depends(get_partition_service),
):
    await ensure_partition_role(
        partition,
        user,
        user_partitions,
        "editor",
        auth_service=auth_service,
        partition_service=partition_service,
    )
    return user


async def require_partition_owner(
    partition=Depends(request_partition),
    user=Depends(current_user),
    user_partitions=Depends(current_user_partitions),
    auth_service: AuthService = Depends(get_auth_service),
    partition_service: PartitionService = Depends(get_partition_service),
):
    await ensure_partition_role(
        partition,
        user,
        user_partitions,
        "owner",
        auth_service=auth_service,
        partition_service=partition_service,
    )
    return user


async def require_partitions_viewer(
    partitions=Depends(request_partitions),
    user=Depends(current_user),
    user_partitions=Depends(current_user_partitions),
    auth_service: AuthService = Depends(get_auth_service),
    partition_service: PartitionService = Depends(get_partition_service),
):
    if SUPER_ADMIN_MODE and user.get("is_admin"):
        return user
    if isinstance(partitions, list) and len(partitions) == 1 and partitions[0] == "all":
        return user
    for partition in partitions:
        await ensure_partition_role(
            partition,
            user,
            user_partitions,
            "viewer",
            auth_service=auth_service,
            partition_service=partition_service,
        )
        logger.info(f"User has viewer access to partition '{partition}'")
    return user


async def require_task_owner(
    task_id=Depends(request_task_id),
    user=Depends(current_user),
    job_service: JobService = Depends(get_job_service),
):
    task_details = await job_service.get_task_details(task_id)
    if not task_details:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found",
        )
    if task_details.get("user_id") != user.get("id"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to access this task",
        )
    return task_details


def require_admin(user=Depends(current_user)):
    """Ensure the user has admin privileges"""
    if not user or not user.get("is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return user


def request_user_id(request: Request) -> int | None:
    """Return the user_id from path params (as int), or None."""
    raw = request.path_params.get("user_id", None)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def require_admin_or_self(
    target_user_id: int | None = Depends(request_user_id),
    user=Depends(current_user),
):
    """Ensure the caller is admin or is acting on their own account."""
    if not user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Authentication required",
        )
    if user.get("is_admin", False):
        return user
    if target_user_id is not None and user.get("id") == target_user_id:
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin privileges or self-access required",
    )


async def check_user_file_quota(
    user=Depends(current_user),
    auth_service: AuthService = Depends(get_auth_service),
    job_service: JobService = Depends(get_job_service),
):
    """
    Check if user has reached their file quota.
    Quota = indexed files + pending indexing tasks.

    Quota logic:
    - Admins bypass this check
    - DEFAULT_FILE_QUOTA < 0 → disabled quota checking
    - user.file_quota = None → use global DEFAULT_FILE_QUOTA
    - user.file_quota < 0 → unlimited
    - user.file_quota >= 0 → specific limit
    """

    if user.get("is_admin", False):
        return user
    if DEFAULT_FILE_QUOTA < 0:
        return user
    user_quota = user.get("file_quota")
    if user_quota is not None and user_quota < 0:
        return user

    user_id = user.get("id")
    pending_count = await job_service.get_user_pending_task_count(user_id)

    logger.debug(
        "User file quota check",
        user_id=user_id,
        pending_count=pending_count,
    )

    try:
        auth_service.validate_file_quota(
            user,
            pending_task_count=pending_count,
            default_quota=DEFAULT_FILE_QUOTA,
        )
    except OpenRAGError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.message,
        ) from exc

    return user


async def validate_file_id(file_id: str):
    return core_validators.validate_file_id(file_id, FORBIDDEN_CHARS_IN_FILE_ID)


async def validate_metadata(metadata: Any | None = Form(None)):
    return core_validators.parse_metadata(metadata)


async def validate_file_format(
    file: UploadFile,
    metadata: dict = Depends(validate_metadata),
):
    core_validators.validate_file_format(
        filename=file.filename,
        accepted_formats=ACCEPTED_FILE_FORMATS,
        accepted_mimetypes=DICT_MIMETYPES.keys(),
        mimetype=metadata.get("mimetype"),
    )
    return file


def human_readable_size(size_bytes: int) -> str:
    """Convert bytes to a human-readable format (e.g., '2.4 MB')."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.2f} PB"


def get_app_state(request: Request):
    return request.app.state.app_state


async def get_openai_models(base_url: str, api_key: str, timeout: int = 30):
    async with AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout) as client:
        models_response = await client.models.list()
        return models_response.data


async def check_llm_model_availability(request: Request):
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
    partition_service: PartitionService,
    is_admin=False,
):
    partition_prefix = consts.PARTITION_PREFIX
    if model_name.startswith(consts.LEGACY_PARTITION_PREFIX):
        # XXX - This is for backward compatibility, but should eventually be removed
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
