"""Workspace management endpoints."""

import asyncio

from components.ray_utils import call_ray_actor_with_timeout
from config import load_config
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from utils.dependencies import get_vectordb
from utils.logger import get_logger

from .utils import require_partition_editor, require_partition_owner, require_partition_viewer

router = APIRouter()
logger = get_logger()

_config = load_config()
VECTORDB_TIMEOUT = _config.ray.indexer.get("vectordb_timeout", 30)


class CreateWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    workspace_id: str
    display_name: str | None = None


class AddFilesRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    file_ids: list[str]


async def require_workspace_in_partition(partition: str, workspace_id: str, vectordb=Depends(get_vectordb)) -> dict:
    """Validate that a workspace exists and belongs to the given partition."""
    ws = await call_ray_actor_with_timeout(
        vectordb.get_workspace.remote(workspace_id),
        timeout=VECTORDB_TIMEOUT,
        task_description=f"get_workspace({workspace_id})",
    )
    if not ws or ws["partition_name"] != partition:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    return ws


@router.post(
    "/partition/{partition}/workspaces",
    status_code=status.HTTP_201_CREATED,
)
async def create_workspace(
    partition: str,
    body: CreateWorkspaceRequest,
    user=Depends(require_partition_editor),
    vectordb=Depends(get_vectordb),
):
    existing = await call_ray_actor_with_timeout(
        vectordb.get_workspace.remote(body.workspace_id),
        timeout=VECTORDB_TIMEOUT,
        task_description=f"get_workspace({body.workspace_id})",
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Workspace '{body.workspace_id}' already exists.",
        )
    await call_ray_actor_with_timeout(
        vectordb.create_workspace.remote(
            workspace_id=body.workspace_id,
            partition=partition,
            user_id=user["id"],
            display_name=body.display_name,
        ),
        timeout=VECTORDB_TIMEOUT,
        task_description=f"create_workspace({body.workspace_id})",
    )
    return {"status": "created", "workspace_id": body.workspace_id}


@router.get(
    "/partition/{partition}/workspaces",
    dependencies=[Depends(require_partition_viewer)],
)
async def list_workspaces(partition: str, vectordb=Depends(get_vectordb)):
    workspaces = await call_ray_actor_with_timeout(
        vectordb.list_workspaces.remote(partition),
        timeout=VECTORDB_TIMEOUT,
        task_description=f"list_workspaces({partition})",
    )
    return {"workspaces": workspaces}


@router.get(
    "/partition/{partition}/workspaces/{workspace_id}",
    dependencies=[Depends(require_partition_viewer)],
)
async def get_workspace(ws=Depends(require_workspace_in_partition)):
    return ws


@router.delete(
    "/partition/{partition}/workspaces/{workspace_id}",
    dependencies=[Depends(require_partition_owner)],
)
async def delete_workspace(
    partition: str, workspace_id: str, vectordb=Depends(get_vectordb), _ws=Depends(require_workspace_in_partition)
):
    orphaned = await call_ray_actor_with_timeout(
        vectordb.delete_workspace.remote(workspace_id),
        timeout=VECTORDB_TIMEOUT,
        task_description=f"delete_workspace({workspace_id})",
    )
    deleted_count = 0
    failed_file_ids: list[str] = []
    if orphaned:
        results = await asyncio.gather(
            *[
                call_ray_actor_with_timeout(
                    vectordb.delete_file.remote(file_id, partition),
                    timeout=VECTORDB_TIMEOUT,
                    task_description=f"delete_file({file_id})",
                )
                for file_id in orphaned
            ],
            return_exceptions=True,
        )
        for file_id, result in zip(orphaned, results):
            if isinstance(result, Exception):
                logger.warning("Failed to delete orphaned file from Milvus", file_id=file_id, error=str(result))
                failed_file_ids.append(file_id)
            else:
                deleted_count += 1
    return {
        "status": "deleted",
        "orphaned_files_deleted": deleted_count,
        "orphaned_files_failed": failed_file_ids,
    }


@router.post(
    "/partition/{partition}/workspaces/{workspace_id}/files",
    dependencies=[Depends(require_partition_editor)],
)
async def add_files_to_workspace(
    workspace_id: str,
    body: AddFilesRequest,
    vectordb=Depends(get_vectordb),
    _ws=Depends(require_workspace_in_partition),
):
    await call_ray_actor_with_timeout(
        vectordb.add_files_to_workspace.remote(workspace_id, body.file_ids),
        timeout=VECTORDB_TIMEOUT,
        task_description=f"add_files_to_workspace({workspace_id})",
    )
    return {"status": "added", "file_ids": body.file_ids}


@router.get(
    "/partition/{partition}/workspaces/{workspace_id}/files",
    dependencies=[Depends(require_partition_viewer)],
)
async def list_workspace_files(
    workspace_id: str, vectordb=Depends(get_vectordb), _ws=Depends(require_workspace_in_partition)
):
    file_ids = await call_ray_actor_with_timeout(
        vectordb.list_workspace_files.remote(workspace_id),
        timeout=VECTORDB_TIMEOUT,
        task_description=f"list_workspace_files({workspace_id})",
    )
    return {"file_ids": file_ids}


@router.get(
    "/partition/{partition}/files/{file_id}/workspaces",
    dependencies=[Depends(require_partition_viewer)],
)
async def list_file_workspaces(partition: str, file_id: str, vectordb=Depends(get_vectordb)):
    workspace_ids = await call_ray_actor_with_timeout(
        vectordb.get_file_workspaces.remote(file_id, partition),
        timeout=VECTORDB_TIMEOUT,
        task_description=f"get_file_workspaces({file_id})",
    )
    return {"file_id": file_id, "workspace_ids": workspace_ids}


@router.delete(
    "/partition/{partition}/workspaces/{workspace_id}/files/{file_id}",
    dependencies=[Depends(require_partition_editor)],
)
async def remove_file_from_workspace(
    workspace_id: str, file_id: str, vectordb=Depends(get_vectordb), _ws=Depends(require_workspace_in_partition)
):
    removed = await call_ray_actor_with_timeout(
        vectordb.remove_file_from_workspace.remote(workspace_id, file_id),
        timeout=VECTORDB_TIMEOUT,
        task_description=f"remove_file_from_workspace({workspace_id}, {file_id})",
    )
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found in workspace")
    return {"status": "removed"}
