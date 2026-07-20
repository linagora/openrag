from api.dependencies.auth import require_admin
from core.utils.logging import get_logger
from di.workers import list_ray_actors as list_ray_actor_states
from di.workers import restart_ray_actor
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

logger = get_logger()


router = APIRouter(dependencies=[Depends(require_admin)])


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
    actors = list_ray_actor_states()
    return JSONResponse(status_code=status.HTTP_200_OK, content={"actors": actors})


@router.post(
    "/{actor_name}/restart",
    name="restart_ray_actor",
    description="""Restart a specific Ray actor.

**Parameters:**
- `actor_name`: Name of the actor to restart

**Permissions:**
- Requires admin role

**Available Actors:**
- `TaskStateManagerV2`: Manages task states
- `MarkerPool`: PDF processing actor pool
- `SerializerQueue`: Document serialization queue
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
async def restart_actor(
    actor_name: str,
):
    """Restart a specific Ray actor by name (kill + recreate)."""
    try:
        actor_id = restart_ray_actor(actor_name)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown actor: {actor_name}")

    logger.info(f"Restarted actor: {actor_name}")
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "message": f"Actor {actor_name} restarted successfully.",
            "actor_name": actor_name,
            "actor_id": actor_id,
        },
    )
