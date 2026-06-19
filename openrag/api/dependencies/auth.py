import os

from core.utils.exceptions import OpenRAGError
from core.utils.logging import get_logger
from di.providers import get_auth_service, get_config, get_job_service, get_partition_service
from fastapi import Depends, HTTPException, Request, status

logger = get_logger()

SUPER_ADMIN_MODE = os.getenv("SUPER_ADMIN_MODE", "false").lower() == "true"


def current_user(request: Request):
    """Return the authenticated user from request.state."""
    return request.state.user


def current_user_partitions(request: Request):
    """Return the authenticated user's partitions from request.state."""
    return request.state.user_partitions


def current_user_or_admin_partitions(request: Request):
    """Return all partitions for super admins, otherwise the user's partitions."""
    user = request.state.user
    if user.get("is_admin") and SUPER_ADMIN_MODE:
        return [{"partition": "all", "created_at": 0, "role": "owner"}]
    return request.state.user_partitions


def current_user_or_admin_partitions_list(request: Request):
    """Return partition names visible to the authenticated user."""
    return [p["partition"] for p in current_user_or_admin_partitions(request)]


def partitions_with_details(request: Request):
    return current_user_or_admin_partitions(request)


def request_partition(request: Request):
    """Return the partition from path params."""
    return request.path_params.get("partition", None)


def request_partitions(request: Request):
    """Return the partitions from query params."""
    return request.query_params.getlist("partitions")


def request_task_id(request: Request):
    """Return the task_id from path params."""
    return request.path_params.get("task_id", None)


async def ensure_partition_role(
    partition: str,
    user,
    user_partitions,
    required_role: str,
    *,
    auth_service,
    partition_service,
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
    auth_service=Depends(get_auth_service),
    partition_service=Depends(get_partition_service),
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
    auth_service=Depends(get_auth_service),
    partition_service=Depends(get_partition_service),
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
    auth_service=Depends(get_auth_service),
    partition_service=Depends(get_partition_service),
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
    auth_service=Depends(get_auth_service),
    partition_service=Depends(get_partition_service),
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
        logger.bind(partition=partition, user_id=user.get("id")).info("User has viewer access")
    return user


async def require_task_owner(
    task_id=Depends(request_task_id),
    user=Depends(current_user),
    job_service=Depends(get_job_service),
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
    """Ensure the user has admin privileges."""
    if not user or not user.get("is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return user


def request_user_id(request: Request) -> int | None:
    """Return the user_id from path params as int, or None."""
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
    auth_service=Depends(get_auth_service),
    job_service=Depends(get_job_service),
    config=Depends(get_config),
):
    """Check if user has reached their file quota."""
    default_file_quota = config.rdb.default_file_quota
    if user.get("is_admin", False):
        return user
    user_quota = user.get("file_quota")
    effective_quota = default_file_quota if user_quota is None else user_quota
    if effective_quota < 0:
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
            default_quota=default_file_quota,
        )
    except OpenRAGError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.message,
        ) from exc

    return user
