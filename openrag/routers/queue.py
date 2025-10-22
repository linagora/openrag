from collections import Counter

from config import load_config
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from utils.dependencies import get_task_state_manager

from .utils import require_admin

# load config
config = load_config()

# Create an APIRouter instance
router = APIRouter(dependencies=[Depends(require_admin)])


def _format_pool_info(worker_info: dict[str, int]) -> dict[str, int]:
    """
    Convert SerializerQueue.pool_info() output into a concise dict for the API.
    """
    return {
        "total_slots": worker_info["total_capacity"],
        "pool_size": worker_info["pool_size"],
        "max_per_actor": worker_info["max_tasks_per_worker"],
    }


@router.get("/info")
async def get_queue_info(task_state_manager=Depends(get_task_state_manager)):
    all_states: dict = await task_state_manager.get_all_states.remote()
    status_counts = Counter(all_states.values())

    active_statuses = ["QUEUED", "SERIALIZING", "CHUNKING", "INSERTING"]
    active = {s: status_counts.get(s, 0) for s in active_statuses}

    task_summary = {
        "active": sum(active.values()),
        "active_statuses": active,
        "total_completed": status_counts.get("COMPLETED", 0),
        "total_failed": status_counts.get("FAILED", 0),
    }

    worker_info = await task_state_manager.get_pool_info.remote()
    workers_block = _format_pool_info(worker_info)

    return {"workers": workers_block, "tasks": task_summary}


@router.get("/tasks", name="list_tasks")
async def list_tasks(
    request: Request,
    task_status: str | None = None,
    task_state_manager=Depends(get_task_state_manager),
):
    """
    - ?task_status=active  → QUEUED | SERIALIZING | CHUNKING | INSERTING
    - ?task_status=<exact> → exact match (case-insensitive)
    - (none)               → all tasks
    """
    # fetck task info
    all_info: dict[str, dict] = await task_state_manager.get_all_info.remote()

    if task_status is None:
        filtered = all_info.items()
    else:
        if task_status.lower() == "active":
            active_states = {"QUEUED", "SERIALIZING", "CHUNKING", "INSERTING"}
            filtered = [
                (tid, info)
                for tid, info in all_info.items()
                if info["state"] in active_states
            ]
        else:
            filtered = [
                (tid, info)
                for tid, info in all_info.items()
                if info["state"].lower() == task_status.lower()
            ]

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
