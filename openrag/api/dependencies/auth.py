import os
import uuid

from core.indexing.validators import validate_partition_name
from core.models.user import FileSlotGrant
from core.utils.exceptions import OpenRAGError
from core.utils.logging import get_logger
from di.providers import get_auth_service, get_config, get_job_service, get_partition_service
from fastapi import Depends, HTTPException, Request, status

logger = get_logger()

SUPER_ADMIN_MODE = os.getenv("SUPER_ADMIN_MODE", "false").lower() == "true"


def current_user(request: Request):
    """Return the authenticated user from request.state.

    AuthMiddleware sets it on every request it lets through to a protected
    route; answer 401 if it is missing rather than fail on the attribute.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthenticated")
    return user


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


class FileSlotReservation:
    """One file slot an upload holds against its uploader's quota (#664).

    ``users.file_count`` counts the slot from admission, so concurrent uploads
    cannot overshoot the quota. The slot ends one of two ways: the catalog
    insert consumes it, or someone releases it. Both are idempotent in the
    database, so a release after the slot was consumed changes nothing.

    Until :meth:`hand_off`, the request owns the slot and the dependency's
    teardown releases it. A route that dispatches an indexing task hands it off
    once the task is queued under ``id``: from then on the completion tracker
    releases it when the task settles without a file.
    """

    __slots__ = ("handed_off", "id", "user_id")

    def __init__(self, reservation_id: str, user_id: int) -> None:
        self.id = reservation_id
        self.user_id = user_id
        self.handed_off = False

    def hand_off(self) -> None:
        self.handed_off = True


def file_slot_reservation_id(reservation: object) -> str | None:
    """The id of a real reservation, or ``None``.

    ``None`` covers users with nothing to reserve, and a test that overrides
    :func:`check_user_file_quota` with a stub.
    """
    return reservation.id if isinstance(reservation, FileSlotReservation) else None


def hand_off_file_slot(reservation: object) -> None:
    if isinstance(reservation, FileSlotReservation):
        reservation.hand_off()


async def check_user_file_quota(
    user=Depends(current_user),
    auth_service=Depends(get_auth_service),
    config=Depends(get_config),
):
    """Reserve one file slot for this upload, or reject it with 403.

    The admission gate of the two quota-bearing routes, ``add_file`` and
    ``copy_file``. The reserve is atomic in Postgres, so a burst of uploads
    admits exactly the slots the quota leaves; the in-memory pending-task count
    is no longer consulted (it was a read-then-check race, and a restart reset
    it).

    Yields a :class:`FileSlotReservation`, or ``None`` when the user has an
    unlimited quota. On the way out the reservation is released unless the
    route handed it off. That covers every exit between admission and dispatch:
    a 409, an upload rejected while saving, an unknown workspace, a dispatch
    error, a cancelled request, because FastAPI raises the route's exception
    into this generator.
    """
    default_file_quota = config.rdb.default_file_quota
    user_id = user.get("id")
    if user_id is None or auth_service.has_unlimited_file_quota(user, default_quota=default_file_quota):
        yield None
        return

    reservation_id = uuid.uuid4().hex
    try:
        grant = await auth_service.reserve_file_slot(user_id, reservation_id, default_quota=default_file_quota)
    except OpenRAGError as exc:
        logger.bind(user_id=user_id).info("Upload rejected: file quota exceeded")
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if grant is not FileSlotGrant.RESERVED:
        # The quota was lifted after this request authenticated.
        yield None
        return

    reservation = FileSlotReservation(reservation_id, user_id)
    try:
        yield reservation
    finally:
        if not reservation.handed_off:
            await auth_service.release_file_slot(reservation_id)
