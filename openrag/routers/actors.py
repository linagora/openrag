from components.app.service import OpenRAGApplicationService
from components.utils import get_llm_semaphore, get_vlm_semaphore
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from ray.util.state import list_actors
from utils.dependencies import (
    get_indexer,
    get_marker_pool,
    get_serializer,
    get_task_state_manager,
    get_vectordb,
)
from utils.logger import get_logger

from .utils import require_admin

logger = get_logger()
app_service = OpenRAGApplicationService()

router = APIRouter(dependencies=[Depends(require_admin)])

actor_creation_map = {
    "TaskStateManager": get_task_state_manager,
    "MarkerPool": get_marker_pool,
    "DocSerializer": get_serializer,
    "Indexer": get_indexer,
    "Vectordb": get_vectordb,
    "llmSemaphore": get_llm_semaphore,
    "vlmSemaphore": get_vlm_semaphore,
}


@router.get(
    "/",
    name="list_ray_actors",
    description="""List all Ray actors and their current status.

**Permissions:**
- Requires admin role

**Response:**
Returns list of all Ray actors with:
- `actor_id`: Unique actor identifier
- `name`: Actor name
- `class_name`: Actor class type
- `state`: Current state (ALIVE, DEAD, etc.)
- `namespace`: Ray namespace

**Note:** This shows the internal distributed computing actors used by OpenRAG.
""",
)
async def list_ray_actors():
    """List all known Ray actors and their status."""
    try:
        actors = [
            {
                "actor_id": a.actor_id,
                "name": a.name,
                "class_name": a.class_name,
                "state": a.state,
                "namespace": a.ray_namespace,
            }
            for a in list_actors()
        ]
        payload = await app_service.list_ray_actors(actors=actors)
        return JSONResponse(status_code=status.HTTP_200_OK, content=payload)
    except Exception:
        logger.exception("Error getting actor summaries")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve actor summaries.",
        )


@router.post(
    "/{actor_name}/restart",
    name="restart_ray_actor",
    description="""Restart a specific Ray actor.

**Parameters:**
- `actor_name`: Name of the actor to restart

**Permissions:**
- Requires admin role

**Available Actors:**
- `TaskStateManager`: Manages task states
- `MarkerPool`: PDF processing actor pool
- `SerializerQueue`: Document serialization queue
- `Indexer`: Document indexing coordinator
- `Vectordb`: Vector database interface
- `llmSemaphore`: LLM request semaphore
- `vlmSemaphore`: Vision LM request semaphore

**Behavior:**
1. Kills the existing actor instance
2. Creates a new actor instance
3. Preserves actor configuration

**Response:**
Returns restart confirmation with new actor ID.

**Warning:** Restarting actors may interrupt ongoing operations.
""",
)
async def restart_actor(actor_name: str):
    """Restart a specific Ray actor by name (kill + recreate)."""
    try:
        result = await app_service.restart_actor(
            actor_name=actor_name,
            actor_creation_map=actor_creation_map,
        )
        logger.info(f"Restarted actor: {actor_name}")
        return JSONResponse(status_code=status.HTTP_200_OK, content=result)
    except KeyError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.exception("Failed to restart actor", actor=actor_name)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to restart actor {actor_name}: {e!s}",
        )
