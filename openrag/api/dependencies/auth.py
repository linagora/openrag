import os

from core.indexing.validators import validate_partition_name
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
    # Reject crafted partition names before they reach any filter expression.
    validate_partition_name(partition)
    if SUPER_ADMIN_MODE and user.get("is_admin"):
        return True

    membership = next((p for p in user_partitions if p["partition"] == partition), None)
    if not membership:
        if await partition_service.partition_exists(partition):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access to partition '{partition}' forbidden",
            )
        # Partition does not exist. The only legitimate reason a non-member may
        # act on a missing partition is the create-on-write path (file upload),
        # which is `editor` and which later creates the partition with the
        # uploader as owner. Reading or owning a partition that does not exist
        # must NOT silently succeed, or a non-member could pass an owner/viewer
        # check by naming an unknown partition.
        if required_role == "editor":
            return True
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Partition '{partition}' not found",
        )

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
        if not user_partitions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No accessible partitions",
            )
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
    auth_service=Depends(get_auth_service),
):
    task_details = await job_service.get_task_details(task_id)
    if not task_details:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found",
        )
    # Delegate the decision to the central PDP (AuthService.authorize, reached via DI
    # — the api layer must not import services directly) rather than inlining policy
    # here — admins may access any task (parity with the admin jobs list), owners
    # their own. Without this, an admin opening another user's job in the admin UI
    # hits a 403 dead-end.
    if not auth_service.authorize(user=user, action="task:access", resource=task_details):
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


class QuotaReservation:
    """One reserved file slot, owned by the request that admitted it.

    Issue #664: admission increments ``users.file_count`` *before* the
    upload is dispatched, so the counter is now "reserved + completed"
    rather than "completed". That makes the reservation a resource with an
    owner — it must either be **consumed** (a ``files`` row is created for
    it) or **released**.

    The handover point is dispatch: until the job is queued the request
    owns the slot and :func:`check_user_file_quota`'s teardown releases it
    on any error; once ``commit()`` is called the indexing worker owns it
    and is responsible for releasing on failure/cancellation. So the
    router must call ``commit()`` — via :func:`commit_quota_reservation` —
    at exactly the moment responsibility transfers, and never earlier.
    """

    __slots__ = ("user_id", "committed")

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self.committed = False

    def commit(self) -> None:
        """Hand the slot off to the worker; teardown will not release it."""
        self.committed = True


def commit_quota_reservation(reservation: object) -> None:
    """Commit ``reservation`` when it is a real one; no-op otherwise.

    Routers receive whatever ``check_user_file_quota`` yields, and tests
    routinely override that dependency with a plain stub. Guarding on the
    type here keeps the routers free of ``if ... is not None`` noise and
    keeps an overridden dependency from turning into an AttributeError.
    """
    if isinstance(reservation, QuotaReservation):
        reservation.commit()


async def check_user_file_quota(
    user=Depends(current_user),
    auth_service=Depends(get_auth_service),
    config=Depends(get_config),
):
    """Atomically reserve one file slot for this upload, or reject it.

    This is the admission gate for the two quota-bearing routes (``add_file``
    and ``copy_file``). It replaces the pre-#664 read-then-check, which
    compared the request's stale ``file_count`` snapshot plus an in-memory
    pending-task count against the quota and admitted every racer.

    The in-memory ``TaskStateManager`` count is deliberately **not** an
    input any more: it is not durable (a restart zeroes it, reopening the
    gate) and it is no longer needed — a reserved slot is already counted
    in the durable ``file_count``.

    Yields a :class:`QuotaReservation`; on the way out, an uncommitted
    reservation is released. That covers every path between admission and
    dispatch — a 409 duplicate, a rejected/oversize upload, workspace
    validation, a dispatch error, or a client disconnect — because FastAPI
    propagates the endpoint's exception into this generator.
    """
    user_id = user.get("id")
    if user_id is None:
        # No durable identity to charge (e.g. auth disabled). Nothing to
        # reserve, so nothing can be enforced or leaked.
        yield None
        return

    try:
        new_count = await auth_service.reserve_file_slot(
            user_id,
            default_quota=config.rdb.default_file_quota,
        )
    except OpenRAGError as exc:
        logger.bind(user_id=user_id).info("Upload rejected: file quota exceeded.")
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    logger.bind(user_id=user_id, file_count=new_count).debug("Reserved a file slot.")
    reservation = QuotaReservation(user_id)
    try:
        yield reservation
    finally:
        if not reservation.committed:
            logger.bind(user_id=user_id).debug("Releasing an unconsumed file-slot reservation.")
            await auth_service.release_file_slot(user_id)
