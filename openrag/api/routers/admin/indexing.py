"""Indexing routes — thin HTTP layer over :class:`IndexingService`.

Phase 8D.1: metadata assembly, existence/workspace checks and task
dispatch moved to
``services.orchestrators.indexing_service.IndexingService`` (the Ray
``Indexer`` / ``TaskStateManager`` actors now sit behind the
``IndexingDispatcher`` port). This module keeps HTTP transport only:
the saved-file IO, ``request.url_for`` link building, the shared
``Depends`` auth wrappers, and the conflict / not-found / bad-input
guards whose exact non-bracketed ``{"detail": ...}`` body the legacy
endpoints returned via ``HTTPException``.
"""

import json
from pathlib import Path
from typing import Any

from api.dependencies.auth import (
    check_user_file_quota,
    current_user_partitions,
    ensure_partition_role,
    require_partition_editor,
    require_task_owner,
)
from api.dependencies.files import (
    validate_file_format,
    validate_file_id,
    validate_metadata,
)
from api.routers.admin.task_logs import collect_task_logs
from components.indexer.utils.files import sanitize_filename, save_file_to_disk
from di.providers import get_auth_service, get_config, get_indexing_service, get_partition_service
from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from utils.logger import get_logger

logger = get_logger()


def build_url(request: Request, route_name: str, *, preferred_url_scheme: str | None = None, **path_params) -> str:
    """Build a URL using the preferred scheme if configured."""
    url = request.url_for(route_name, **path_params)
    if preferred_url_scheme:
        url = url.replace(scheme=preferred_url_scheme)
    return str(url)


router = APIRouter()


@router.get(
    "/supported/types",
    description="""Get supported file types for indexing.

**Response:**
Returns a list of supported file extensions and MIME types that can be indexed by the system.
""",
)
async def get_supported_types(config=Depends(get_config)):
    """
    Get a list of supported types for indexing.

    Returns:
        JSON object containing:
        - `extensions`: List of supported file extensions.
        - `mimetypes`: List of supported MIME types.
    """
    accepted_file_formats = config.loader.file_loaders.model_dump().keys()
    mimetypes = config.loader.mimetypes.to_dict()
    resp = {"extensions": list(accepted_file_formats), "mimetypes": list(mimetypes)}
    return JSONResponse(content=resp)


@router.post(
    "/partition/{partition}/file/{file_id}",
    description="""Upload and index a new file.

**File Type Support:**
- Supports standard file extensions listed in `/supported/types`
- For unsupported extensions, specify `mimetype` in metadata

**Metadata Format:**
JSON string containing file metadata. Example:
```json
{
    "mimetype": "text/plain",
    "author": "John Doe",
    ...
    "created_at": "2025-01-03T00:00:00+08:00"  // Optional temporal field (ISO 8601)
}
```

**Temporal Fields:**
- You can provide a temporal fields such as `created_at` in the metadata for time-based queries and filtering.
- Datetime values must be in ISO 8601 format (e.g., `2025-01-03T00:00:00+08:00`).

**Common Mimetypes:**
- `text/plain` - Plain text files
- `text/markdown` - Markdown files
- `application/pdf` - PDF documents
- `message/rfc822` - Email files

**Response:**
Returns 201 Created with a task status URL for tracking indexing progress.
""",
)
async def add_file(
    request: Request,
    partition: str,
    file_id: str = Depends(validate_file_id),
    file: UploadFile = Depends(validate_file_format),
    metadata: dict = Depends(validate_metadata),
    workspace_ids: str | None = Form(None, description="JSON array of workspace IDs to add the file to"),
    user=Depends(require_partition_editor),
    _quota_check=Depends(check_user_file_quota),
    config=Depends(get_config),
    service=Depends(get_indexing_service),
):
    if await service.file_exists(file_id, partition):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"File '{file_id}' already exists in partition {partition}",
        )

    original_filename = file.filename
    file.filename = sanitize_filename(file.filename)
    try:
        file_path = await save_file_to_disk(file, Path(config.paths.data_dir), with_random_prefix=True)
    except Exception as e:
        logger.exception("Failed to save file to disk.", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )

    parsed_workspace_ids = None
    if workspace_ids:
        try:
            parsed_workspace_ids = json.loads(workspace_ids)
            if not isinstance(parsed_workspace_ids, list):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="workspace_ids must be a JSON array of strings",
            )
        for ws_id in parsed_workspace_ids:
            ws = await service.get_workspace(ws_id)
            if not ws or ws["partition_name"] != partition:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Workspace '{ws_id}' not found in partition '{partition}'",
                )

    task_id = await service.add_file(
        file_path=str(file_path),
        file_id=file_id,
        partition=partition,
        metadata=metadata,
        sanitized_filename=file.filename,
        original_filename=original_filename,
        user=user,
        workspace_ids=parsed_workspace_ids,
    )

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "task_status_url": build_url(
                request,
                "get_task_status",
                preferred_url_scheme=config.server.preferred_url_scheme,
                task_id=task_id,
            )
        },
    )


@router.delete(
    "/partition/{partition}/file/{file_id}",
    description="""Delete a file from a partition.

**Parameters:**
- `partition`: The partition name
- `file_id`: The unique identifier of the file to delete

**Response:**
Returns 204 No Content on successful deletion.
""",
)
async def delete_file(
    partition: str,
    file_id: str,
    user=Depends(require_partition_editor),
    service=Depends(get_indexing_service),
):
    if not await service.file_exists(file_id, partition):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{file_id}' not found in partition '{partition}'",
        )
    await service.delete_file(file_id, partition)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/partition/{partition}/file/{file_id}",
    description="""Update an existing file by replacing it.

**Parameters:**
- `partition`: The partition name
- `file_id`: The unique identifier of the file to replace
- `file`: New file to upload
- `metadata`: Optional metadata as JSON string

**Behavior:**
- Deletes the existing file
- Uploads and indexes the new file
- Preserves the file_id

**Metadata Format:**
JSON string containing file metadata. Example:
```json
{
    "mimetype": "text/plain",
    "author": "John Doe",
    ...
    "created_at": "2024-01-01T12:00:00+00:00"  // Optional temporal field (ISO 8601)
}
```

**Temporal Fields:**
- You can provide the temporal fields `created_at` in the metadata for time-based queries and filtering.
- Datetime values must be in ISO 8601 format (e.g., `2024-01-01T12:00:00+00:00`).

**Response:**
Returns 202 Accepted with a task status URL for tracking indexing progress.
""",
)
async def put_file(
    request: Request,
    partition: str,
    file_id: str = Depends(validate_file_id),
    file: UploadFile = Depends(validate_file_format),
    metadata: dict = Depends(validate_metadata),
    user=Depends(require_partition_editor),
    config=Depends(get_config),
    service=Depends(get_indexing_service),
):
    if not await service.file_exists(file_id, partition):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{file_id}' not found in partition '{partition}'",
        )

    # No Milvus deletion here. The Indexer's add_file(replace=True) flow uses
    # insert-before-delete: it snapshots old chunk IDs, inserts new chunks,
    # then deletes old ones — so the file is never left in a half-replaced state.
    original_filename = file.filename
    file.filename = sanitize_filename(file.filename)
    file_path = await save_file_to_disk(file, Path(config.paths.data_dir), with_random_prefix=True)

    task_id = await service.add_file(
        file_path=str(file_path),
        file_id=file_id,
        partition=partition,
        metadata=metadata,
        sanitized_filename=file.filename,
        original_filename=original_filename,
        user=user,
        replace=True,
    )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "task_status_url": build_url(
                request,
                "get_task_status",
                preferred_url_scheme=config.server.preferred_url_scheme,
                task_id=task_id,
            )
        },
    )


@router.patch(
    "/partition/{partition}/file/{file_id}",
    description="""Update file metadata without re-uploading the file.

**Parameters:**
- `partition`: The partition name
- `file_id`: The unique identifier of the file
- `metadata`: Metadata fields to update as JSON string

**Behavior:**
- Updates only the specified metadata fields
- Does not require file re-upload
- Can change the file's partition if user has access

**Response:**
Returns 200 OK with a success message.
""",
)
async def patch_file(
    partition: str,
    file_id: str = Depends(validate_file_id),
    metadata: Any | None = Depends(validate_metadata),
    user=Depends(require_partition_editor),
    user_partitions=Depends(current_user_partitions),
    service=Depends(get_indexing_service),
    auth_service=Depends(get_auth_service),
    partition_service=Depends(get_partition_service),
):
    # Make sure partition role is valid if partition is being changed
    if "partition" in metadata:
        await ensure_partition_role(
            partition=metadata["partition"],
            user=user,
            user_partitions=user_partitions,
            required_role="editor",
            auth_service=auth_service,
            partition_service=partition_service,
        )

    await service.update_metadata(file_id, metadata, partition, user)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"message": f"Metadata for file '{file_id}' successfully updated."},
    )


@router.post(
    "/partition/{partition}/file/{file_id}/copy",
    description="""Copy a file from one partition to another.

**Parameters:**
- `partition`: Destination partition name
- `file_id`: New file ID in destination partition
- `source_partition`: Source partition name (form data)
- `source_file_id`: Source file ID (form data)
- `metadata`: Optional metadata to override as JSON string

**Permissions:**
- Requires viewer access to source partition
- Requires editor access to destination partition

**Response:**
Returns 201 Created on successful copy.
""",
)
async def copy_file_between_partitions(
    partition: str,
    file_id: str = Depends(validate_file_id),
    metadata: Any | None = Depends(validate_metadata),
    source_partition: str = Form(...),
    source_file_id: str = Form(...),
    user=Depends(require_partition_editor),
    user_partitions=Depends(current_user_partitions),
    _quota_check=Depends(check_user_file_quota),
    service=Depends(get_indexing_service),
    auth_service=Depends(get_auth_service),
    partition_service=Depends(get_partition_service),
):
    # Make sure user has access to the source partition
    await ensure_partition_role(
        partition=source_partition,
        user=user,
        user_partitions=user_partitions,
        required_role="viewer",
        auth_service=auth_service,
        partition_service=partition_service,
    )

    await service.copy_file(
        source_file_id=source_file_id,
        source_partition=source_partition,
        target_file_id=file_id,
        target_partition=partition,
        metadata=metadata,
        user=user,
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"message": "File copied successfully."},
    )


@router.get(
    "/task/{task_id}",
    description="""Get the status of an indexing task.

**Parameters:**
- `task_id`: The unique task identifier returned when uploading a file

**Response:**
Returns task status information including:
- `task_id`: The task identifier
- `task_state`: Current state (QUEUED, RUNNING, SUCCESS, FAILED)
- `details`: Additional task details
- `error_url`: URL to get error details (if task failed)
""",
)
async def get_task_status(
    request: Request,
    task_id: str,
    task_details=Depends(require_task_owner),
    config=Depends(get_config),
    service=Depends(get_indexing_service),
):
    state = await service.get_task_state(task_id)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found.",
        )

    content: dict[str, Any] = {
        "task_id": task_id,
        "task_state": state,
        "details": task_details,
    }

    if state == "FAILED":
        content["error_url"] = build_url(
            request,
            "get_task_error",
            preferred_url_scheme=config.server.preferred_url_scheme,
            task_id=task_id,
        )

    return JSONResponse(status_code=status.HTTP_200_OK, content=content)


@router.get(
    "/task/{task_id}/error",
    description="""Get error details for a failed task.

**Parameters:**
- `task_id`: The unique task identifier

**Response:**
Returns error information including:
- `task_id`: The task identifier
- `traceback`: Error traceback as an array of lines

**Note:** Only available if task state is FAILED.
""",
)
async def get_task_error(
    task_id: str,
    task_details=Depends(require_task_owner),
    service=Depends(get_indexing_service),
):
    error = await service.get_task_error(task_id)
    if error is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No error found for task '{task_id}'.",
        )
    return {"task_id": task_id, "traceback": error.splitlines()}


@router.get(
    "/task/{task_id}/logs",
    description="""Get logs for a specific task.

**Parameters:**
- `task_id`: The unique task identifier
- `max_lines`: Maximum number of log lines to return (default: 100)

**Response:**
Returns task logs including:
- `task_id`: The task identifier
- `logs`: Array of log entries with timestamps and messages

**Note:** Logs are returned in chronological order (oldest first).
""",
)
async def get_task_logs(
    task_id: str,
    max_lines: int = 100,
    task_details=Depends(require_task_owner),
    config=Depends(get_config),
):
    log_file = Path(config.paths.log_dir or "logs") / "app.json"
    if not log_file.exists():
        raise HTTPException(status_code=500, detail="Log file not found.")

    try:
        logs = collect_task_logs(log_file, task_id, max_lines)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    if not logs:
        raise HTTPException(status_code=404, detail=f"No logs found for task '{task_id}'")

    return JSONResponse(content={"task_id": task_id, "logs": logs})


@router.delete(
    "/task/{task_id}",
    name="cancel_task",
    description="""Cancel a running or queued task.

**Parameters:**
- `task_id`: The unique task identifier

**Behavior:**
- Sends cancellation signal to the task
- Recursively cancels all subtasks
- Does not guarantee immediate cancellation

**Response:**
Returns confirmation message that cancellation signal was sent.
""",
)
async def cancel_task(
    task_id: str,
    task_details=Depends(require_task_owner),
    service=Depends(get_indexing_service),
):
    cancelled = await service.cancel_task(task_id)
    if not cancelled:
        raise HTTPException(404, f"No ObjectRef stored for task {task_id}")
    return {"message": f"Cancellation signal sent for task {task_id}"}
