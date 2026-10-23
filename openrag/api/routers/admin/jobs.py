"""Queue routes — thin HTTP layer over :class:`JobService`.

Phase 8D.2: queue aggregation and the ``?task_status=`` filtering moved
to ``services.orchestrators.job_service.JobService``. This module keeps
HTTP transport only: the shared admin/current-user ``Depends`` wrappers
and the ``request.url_for`` link building.
"""

from typing import Annotated

from api.dependencies.auth import current_user, require_admin
from di.providers import get_job_service
from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get(
    "/info",
    description="""Get queue and worker pool information.

**Permissions:**
- Requires admin role

**Response:**
Returns system status including:

**Workers:**
- `total_slots`: Total available worker capacity
- `pool_size`: Number of worker actors
- `max_per_actor`: Max concurrent tasks per worker

**Tasks:**
- `active`: Total active tasks
- `active_statuses`: Breakdown by status (QUEUED, SERIALIZING)
- `total_completed`: Count of completed tasks
- `total_cancelled`: Count of cancelled tasks
- `total_failed`: Count of failed tasks

**Use Case:**
Monitor system load and worker utilization.
""",
)
async def get_queue_info(
    admin=Depends(require_admin),
    service=Depends(get_job_service),
):
    return await service.get_queue_info()


@router.get(
    "/tasks",
    name="list_tasks",
    description="""List indexing tasks with optional filtering.

**Query Parameters:**
- `task_status`: Filter by status (optional)
  - `active`: Show QUEUED or SERIALIZING tasks
  - `completed`: Show completed tasks
  - `failed`: Show failed tasks
  - `cancelled`: Show cancelled tasks
  - Any exact status name (case-insensitive)
  - Omit to show all tasks
- `limit` and `offset`: Optional server-side pagination. When either is supplied, limit defaults to 50 and is capped at 100.

**Permissions:**
- Regular users: See only their own tasks
- Admins: See all tasks

**Response:**
Returns list of tasks with:
- `task_id`: Unique task identifier
- `state`: Current task state
- `outcome`: Completion result, including `completed_degraded`
- `details`: Task metadata (file_id, partition, etc.)
- `created_at`: Date and time when the task was created
- `duration_ms`: Total task duration in milliseconds
- `stage_progress`: Compact current stage and history quality, when available
- `error_summary`: Concise failure reason for admins (if failed)
- `url`: Link to detailed task status
- `error_url`: Link to error details (if failed)

**Task States:**
- `QUEUED`: Waiting to start
- `SERIALIZING`: Parsing, chunking, embedding, and storing the document
- `COMPLETED`: Successfully finished
- `CANCELLED`: Cancelled by user/admin
- `FAILED`: Error occurred
""",
)
async def list_tasks(
    request: Request,
    task_status: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    offset: Annotated[int | None, Query(ge=0)] = None,
    user=Depends(current_user),
    service=Depends(get_job_service),
):
    """
    - ?task_status=active  → QUEUED | SERIALIZING
    - ?task_status=<exact> → exact match (case-insensitive)
    - (none)               → all tasks
    """
    result = await service.list_tasks(
        is_admin=bool(user.get("is_admin")),
        user_id=user.get("id"),
        task_status=task_status,
        limit=limit,
        offset=offset,
    )
    pagination = None
    if isinstance(result, dict):
        rows = result["tasks"]
        pagination = result["pagination"]
    else:
        rows = result

    tasks = []
    for row in rows:
        task_id = row["task_id"]
        item = {
            "task_id": task_id,
            "state": row["state"],
            "outcome": row["outcome"],
            "details": row["details"],
            "created_at": row["created_at"],
            "duration_ms": row["duration_ms"],
            **({"stage_progress": row["stage_progress"]} if row.get("stage_progress") is not None else {}),
            **({"error_summary": row["error_summary"]} if user.get("is_admin") and row.get("error_summary") else {}),
            **(
                {"error_url": str(request.url_for("get_task_error", task_id=task_id))}
                if row["state"] == "FAILED"
                else {}
            ),
            "url": str(request.url_for("get_task_status", task_id=task_id)),
        }
        tasks.append(item)

    payload = {"tasks": tasks}
    if pagination is not None:
        payload["pagination"] = pagination
    return JSONResponse(status_code=status.HTTP_200_OK, content=payload)
