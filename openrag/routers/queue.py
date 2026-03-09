from components.app.service import OpenRAGApplicationService
from config import load_config
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from utils.dependencies import get_task_state_manager

from .utils import current_user, require_admin

# load config
config = load_config()

# Create an APIRouter instance
router = APIRouter()
app_service = OpenRAGApplicationService()


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
- `total_failed`: Count of failed tasks

**Use Case:**
Monitor system load and worker utilization.
""",
)
async def get_queue_info(admin=Depends(require_admin), task_state_manager=Depends(get_task_state_manager)):
    return await app_service.get_queue_info(task_state_manager=task_state_manager)


@router.get(
    "/tasks",
    name="list_tasks",
    description="""List indexing tasks with optional filtering.

**Query Parameters:**
- `task_status`: Filter by status (optional)
  - `active`: Show QUEUED, SERIALIZING, CHUNKING, or INSERTING tasks
  - `completed`: Show completed tasks
  - `failed`: Show failed tasks
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
    payload = await app_service.list_my_tasks(
        user_id=None if user.get("is_admin") else user.get("id"),
        task_status=task_status,
    )
    tasks = []
    for task in payload.get("tasks", []):
        item = {
            "task_id": task["task_id"],
            "state": task["state"],
            "details": task.get("details"),
            "url": str(request.url_for("get_task_status", task_id=task["task_id"])),
        }
        if task["state"] == "FAILED":
            item["error_url"] = str(request.url_for("get_task_error", task_id=task["task_id"]))
        tasks.append(item)
    return JSONResponse(status_code=status.HTTP_200_OK, content={"tasks": tasks})
