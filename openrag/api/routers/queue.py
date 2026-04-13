from collections import Counter

from config import load_config
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from di.dependencies import get_task_state_manager

from .utils import current_user, require_admin

# load config
config = load_config()

# Create an APIRouter instance
router = APIRouter()


def _format_pool_info(worker_info: dict[str, int]) -> dict[str, int]:
    """
    Convert SerializerQueue.pool_info() output into a concise dict for the API.
    """
    return {
        "total_slots": worker_info["total_capacity"],
        "pool_size": worker_info["pool_size"],
        "max_per_actor": worker_info["max_tasks_per_worker"],
    }


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
- `active_statuses`: Breakdown by status (QUEUED, SERIALIZING, CHUNKING, INSERTING)
- `total_completed`: Count of completed tasks
- `total_cancelled`: Count of cancelled tasks
- `total_failed`: Count of failed tasks

**Use Case:**
Monitor system load and worker utilization.
""",
)
async def get_queue_info(admin=Depends(require_admin), task_state_manager=Depends(get_task_state_manager)):
    all_states: dict = await task_state_manager.get_all_states.remote()
    status_counts = Counter(all_states.values())

    active_statuses = ["QUEUED", "SERIALIZING", "CHUNKING", "INSERTING"]
    active = {s: status_counts.get(s, 0) for s in active_statuses}

    task_summary = {
        "active": sum(active.values()),
        "active_statuses": active,
        "total_cancelled": status_counts.get("CANCELLED", 0),
        "total_completed": status_counts.get("COMPLETED", 0),
        "total_failed": status_counts.get("FAILED", 0),
    }

    worker_info = await task_state_manager.get_pool_info.remote()
    workers_block = _format_pool_info(worker_info)

    return {"workers": workers_block, "tasks": task_summary}


@router.get(
    "/tasks",
    name="list_tasks",
    description="""List indexing tasks with optional filtering.

**Query Parameters:**
- `task_status`: Filter by status (optional)
  - `active`: Show QUEUED, SERIALIZING, CHUNKING, or INSERTING tasks
  - `completed`: Show completed tasks
  - `failed`: Show failed tasks
  - `cancelled`: Show cancelled tasks
  - Any exact status name (case-insensitive)
  - Omit to show all tasks

**Permissions:**
- Regular users: See only their own tasks
- Admins: See all tasks

**Response:**
Returns list of tasks with:
- `task_id`: Unique task identifier
- `state`: Current task state
- `details`: Task metadata (file_id, partition, etc.)
- `url`: Link to detailed task status
- `error_url`: Link to error details (if failed)

**Task States:**
- `QUEUED`: Waiting to start
- `SERIALIZING`: Converting document format
- `CHUNKING`: Splitting into chunks
- `INSERTING`: Adding to vector database
- `COMPLETED`: Successfully finished
- `CANCELLED`: Cancelled by user/admin
- `FAILED`: Error occurred
""",
)
async def list_tasks(
    request: Request,
    task_status: str | None = None,
    task_state_manager=Depends(get_task_state_manager),
    user=Depends(current_user),
):
    """
    - ?task_status=active  → QUEUED | SERIALIZING | CHUNKING | INSERTING
    - ?task_status=<exact> → exact match (case-insensitive)
    - (none)               → all tasks
    """
    # fetch task info
    if user.get("is_admin"):
        all_info: dict[str, dict] = await task_state_manager.get_all_info.remote()
    else:
        all_info: dict[str, dict] = await task_state_manager.get_all_user_info.remote(user.get("id"))

    if task_status is None:
        filtered = all_info.items()
    else:
        if task_status.lower() == "active":
            active_states = {"QUEUED", "SERIALIZING", "CHUNKING", "INSERTING"}
            filtered = [(tid, info) for tid, info in all_info.items() if info["state"] in active_states]
        else:
            filtered = [(tid, info) for tid, info in all_info.items() if info["state"].lower() == task_status.lower()]

    # format the response
    tasks = []
    for task_id, info in filtered:
        item = {
            "task_id": task_id,
            "state": info["state"],
            "details": info["details"],
            # include an error URL if applicable
            **(
                {"error_url": str(request.url_for("get_task_error", task_id=task_id))}
                if info["state"] == "FAILED"
                else {}
            ),
            "url": str(request.url_for("get_task_status", task_id=task_id)),
        }
        tasks.append(item)

    return JSONResponse(status_code=status.HTTP_200_OK, content={"tasks": tasks})
