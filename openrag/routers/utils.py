import json
import os
from pathlib import Path
from typing import Any

import consts
import httpx
import openai
from config import load_config
from fastapi import Depends, Form, HTTPException, Request, UploadFile, status
from openai import AsyncOpenAI
from utils.dependencies import get_task_state_manager, get_vectordb
from utils.logger import get_logger

# load config
config = load_config()
logger = get_logger()
task_state_manager = get_task_state_manager()

SUPER_ADMIN_MODE = os.getenv("SUPER_ADMIN_MODE", "false").lower() == "true"
DATA_DIR = config.paths.data_dir

FORBIDDEN_CHARS_IN_FILE_ID = set("/")  # set('"<>#%{}|\\^`[]')
LOG_FILE = Path(config.paths.log_dir or "logs") / "app.json"

# supported file formats or mimetypes
ACCEPTED_FILE_FORMATS = dict(config.loader["file_loaders"]).keys()
DICT_MIMETYPES = dict(config.loader["mimetypes"])

ROLE_HIERARCHY = {
    "viewer": 1,
    "editor": 2,
    "owner": 3,
}

# File quota per user
DEFAULT_FILE_QUOTA = config.rdb.get("default_file_quota", -1)


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
):
    """Ensure the user has at least `required_role` for the partition."""
    # Super-admin bypass
    vectordb = get_vectordb()
    if SUPER_ADMIN_MODE and user.get("is_admin"):
        return True

    # Find membership
    membership = next((p for p in user_partitions if p["partition"] == partition), None)

    if not membership:
        # Partition exists but no membership
        partition_exists = await vectordb.partition_exists.remote(partition)
        if partition_exists:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access to partition '{partition}' forbidden",
            )
        else:
            return True

    user_role = membership.get("role")
    if ROLE_HIERARCHY[user_role] < ROLE_HIERARCHY[required_role]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{required_role.capitalize()} role required for partition '{partition}'",
        )

    return True


async def require_partition_viewer(
    partition=Depends(request_partition),
    user=Depends(current_user),
    user_partitions=Depends(current_user_partitions),
):
    await ensure_partition_role(partition, user, user_partitions, "viewer")
    return user


async def require_partition_editor(
    partition=Depends(request_partition),
    user=Depends(current_user),
    user_partitions=Depends(current_user_partitions),
):
    await ensure_partition_role(partition, user, user_partitions, "editor")
    return user


async def require_partition_owner(
    partition=Depends(request_partition),
    user=Depends(current_user),
    user_partitions=Depends(current_user_partitions),
):
    await ensure_partition_role(partition, user, user_partitions, "owner")
    return user


async def require_partitions_viewer(
    partitions=Depends(request_partitions),
    user=Depends(current_user),
    user_partitions=Depends(current_user_partitions),
):
    if SUPER_ADMIN_MODE and user.get("is_admin"):
        return user
    if isinstance(partitions, list) and len(partitions) == 1 and partitions[0] == "all":
        return user
    for partition in partitions:
        await ensure_partition_role(partition, user, user_partitions, "viewer")
        logger.info(f"User has viewer access to partition '{partition}'")
    return user


async def require_task_owner(task_id=Depends(request_task_id), user=Depends(current_user)):
    task_details = await task_state_manager.get_details.remote(task_id)
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


async def check_user_file_quota(
    user=Depends(current_user),
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

    # Admins have unlimited quota
    if user.get("is_admin"):
        return user

    if DEFAULT_FILE_QUOTA < 0:  # disabled quota checking
        return user

    # Determine quota
    user_quota = user.get("file_quota")

    if user_quota is None:
        # Use global quota
        user_quota = DEFAULT_FILE_QUOTA

    if user_quota < 0:  # unlimited quota
        return user

    # Now user_quota >= 0

    user_id = user.get("id")
    indexed_count = user.get("file_count", 0)  # Get indexed file count from user info
    pending_count = await task_state_manager.get_user_pending_task_count.remote(
        user_id
    )  # Get pending task count from task manager

    total = indexed_count + pending_count

    logger.debug(
        "User file quota check",
        user_id=user_id,
        indexed_count=indexed_count,
        pending_count=pending_count,
        user_quota=user_quota,
    )

    if total >= user_quota:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"File quota exceeded. You have {indexed_count} indexed files and {pending_count} pending tasks. Limit: {user_quota}",
        )

    return user


def is_file_id_valid(file_id: str) -> bool:
    return not any(c in file_id for c in FORBIDDEN_CHARS_IN_FILE_ID)


async def validate_file_id(file_id: str):
    if not is_file_id_valid(file_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File ID contains forbidden characters: {', '.join(FORBIDDEN_CHARS_IN_FILE_ID)}",
        )
    if not file_id.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File ID cannot be empty.")
    return file_id


async def validate_metadata(metadata: Any | None = Form(None)):
    try:
        processed_metadata = metadata or "{}"
        processed_metadata = json.loads(processed_metadata)
        return processed_metadata
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON in metadata")


async def validate_file_format(
    file: UploadFile,
    metadata: dict = Depends(validate_metadata),
):
    file_extension = file.filename.split(".")[-1].lower() if "." in file.filename else ""
    mimetype = metadata.get("mimetype", None)

    if file_extension not in ACCEPTED_FILE_FORMATS and mimetype not in DICT_MIMETYPES.keys():
        details = (
            f"Unsupported file format: {file_extension} or file mimetype.\n"
            f"Supported formats: {', '.join(ACCEPTED_FILE_FORMATS)}\n"
            f"Supported mimetypes: {', '.join(DICT_MIMETYPES.keys())}"
        )
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=details,
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
    models = {"LLM": config.llm, "VLM": config.vlm}
    for model_type, param in models.items():
        log = logger.bind(base_url=param["base_url"], model=param["model"])
        try:
            log.debug("Validating model", model_type=model_type)
            openai_models = await get_openai_models(base_url=param["base_url"], api_key=param["api_key"], timeout=30)
            available_models = {m.id for m in openai_models}
            if param["model"] not in available_models:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Only these models ({available_models}) are available for your `{model_type}`. Please check your configuration file.",
                )
        except HTTPException:
            raise
        except httpx.TimeoutException:
            log.warning("Model availability check timed out", model_type=model_type)
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=f"{model_type} service timed out",
            )
        except httpx.HTTPError as e:
            log.warning("Model availability check failed", model_type=model_type, error=str(e))
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"{model_type} service is unavailable",
            )
        except openai.APIError as e:
            log.error("API Endpoint error while validating model", model_type=model_type, error=str(e))
            status_code = getattr(e, "status_code", status.HTTP_500_INTERNAL_SERVER_ERROR)
            raise HTTPException(
                status_code=status_code,
                detail=f"OpenAI API Endpoint error: {str(e)}",
            )
        except Exception as e:
            log.exception("Failed to check model availability", model_type=model_type, error=str(e))
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to check {model_type} model availability",
            )


async def get_partition_name(model_name, user_partitions, is_admin=False):
    vectordb = get_vectordb()

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
    if partition != "all" and not await vectordb.partition_exists.remote(partition):
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
