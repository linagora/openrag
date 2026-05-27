from api.dependencies.auth import require_admin
from fastapi import APIRouter, Depends, HTTPException, status

router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("/", name="list_ray_actors")
async def list_ray_actors():
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Cluster actor operations need a service boundary before moving into api/routers.",
    )


@router.post("/{actor_name}/restart", name="restart_ray_actor")
async def restart_actor(actor_name: str):
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Cluster actor operations need a service boundary before moving into api/routers.",
    )
