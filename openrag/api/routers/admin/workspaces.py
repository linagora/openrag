"""Workspace management endpoints — thin HTTP layer over WorkspaceService.

Phase 8B.2: workspace CRUD, file association and the cross-cutting
delete-with-orphan-cleanup moved to
``services.orchestrators.workspace_service.WorkspaceService``. This
module keeps HTTP transport only: request-scoped authorization, request
schema validation, and the guards whose exact non-bracketed
``{"detail": ...}`` body the legacy endpoints returned via
``HTTPException`` (409 duplicate, the workspace-in-partition 404, the
unknown/missing-file 404s, the not-removed 404).
"""

from api.dependencies.auth import require_partition_editor, require_partition_owner, require_partition_viewer
from api.schemas.admin.workspaces import AddFilesRequest, CreateWorkspaceRequest
from core.utils.logging import get_logger
from di.providers import get_workspace_service
from fastapi import APIRouter, Depends, HTTPException, status

router = APIRouter()
logger = get_logger()


async def require_workspace_in_partition(
    partition: str,
    workspace_id: str,
    service=Depends(get_workspace_service),
) -> str:
    """Validate that a workspace exists and belongs to the given partition.

    Returns the key the workspace is stored under (see ``workspace_key``).
    """
    key = await service.find_workspace_key(partition, workspace_id)
    if key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    return key


@router.post(
    "/partition/{partition}/workspaces",
    status_code=status.HTTP_201_CREATED,
)
async def create_workspace(
    partition: str,
    body: CreateWorkspaceRequest,
    user=Depends(require_partition_editor),
    service=Depends(get_workspace_service),
):
    if await service.find_workspace_key(partition, body.workspace_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Workspace '{body.workspace_id}' already exists.",
        )
    await service.create_workspace(
        workspace_id=body.workspace_id,
        partition=partition,
        user_id=user["id"],
        display_name=body.display_name,
    )
    return {"status": "created", "workspace_id": body.workspace_id}


@router.get(
    "/partition/{partition}/workspaces",
    dependencies=[Depends(require_partition_viewer)],
)
async def list_workspaces(
    partition: str,
    service=Depends(get_workspace_service),
):
    return {"workspaces": await service.list_workspaces(partition)}


@router.get(
    "/partition/{partition}/workspaces/{workspace_id}",
    dependencies=[Depends(require_partition_viewer)],
)
async def get_workspace(
    key=Depends(require_workspace_in_partition),
    service=Depends(get_workspace_service),
):
    return await service.get_workspace(key)


@router.delete(
    "/partition/{partition}/workspaces/{workspace_id}",
    dependencies=[Depends(require_partition_owner)],
)
async def delete_workspace(
    partition: str,
    key=Depends(require_workspace_in_partition),
    service=Depends(get_workspace_service),
):
    result = await service.delete_workspace(partition, key)
    return {"status": "deleted", **result}


@router.post(
    "/partition/{partition}/workspaces/{workspace_id}/files",
    dependencies=[Depends(require_partition_editor)],
)
async def add_files_to_workspace(
    partition: str,
    body: AddFilesRequest,
    key=Depends(require_workspace_in_partition),
    service=Depends(get_workspace_service),
):
    existing_ids = await service.get_existing_file_ids(partition, body.file_ids)
    unknown_ids = sorted(set(body.file_ids) - set(existing_ids))
    if unknown_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File IDs not found in partition '{partition}': {unknown_ids}",
        )
    missing = await service.add_files(key, body.file_ids)
    if missing:
        # TOCTOU: files were deleted between the pre-check and the insert.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File IDs not found in partition '{partition}': {sorted(missing)}",
        )
    return {"status": "added", "file_ids": body.file_ids}


@router.get(
    "/partition/{partition}/workspaces/{workspace_id}/files",
    dependencies=[Depends(require_partition_viewer)],
)
async def list_workspace_files(
    key=Depends(require_workspace_in_partition),
    service=Depends(get_workspace_service),
):
    return {"file_ids": await service.list_files(key)}


@router.get(
    "/partition/{partition}/files/{file_id}/workspaces",
    dependencies=[Depends(require_partition_viewer)],
)
async def list_file_workspaces(
    partition: str,
    file_id: str,
    service=Depends(get_workspace_service),
):
    workspace_ids = await service.get_file_workspaces(file_id, partition)
    return {"file_id": file_id, "workspace_ids": workspace_ids}


@router.delete(
    "/partition/{partition}/workspaces/{workspace_id}/files/{file_id}",
    dependencies=[Depends(require_partition_editor)],
)
async def remove_file_from_workspace(
    file_id: str,
    key=Depends(require_workspace_in_partition),
    service=Depends(get_workspace_service),
):
    removed = await service.remove_file(key, file_id)
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found in workspace")
    return {"status": "removed"}
