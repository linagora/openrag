import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import ray
from components.files import save_file_to_disk
from config import load_config
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
from utils.dependencies import get_indexer, get_task_state_manager, get_vectordb
from utils.logger import get_logger

from .utils import (
    current_user_partitions,
    ensure_partition_role,
    human_readable_size,
    require_partition_editor,
    require_task_owner,
    validate_file_format,
    validate_file_id,
    validate_metadata,
)

# load logger
logger = get_logger()

# load config
config = load_config()
DATA_DIR = config.paths.data_dir

FORBIDDEN_CHARS_IN_FILE_ID = set("/")  # set('"<>#%{}|\\^`[]')
LOG_FILE = Path(config.paths.log_dir or "logs") / "app.json"

# supported file formats or mimetypes
ACCEPTED_FILE_FORMATS = dict(config.loader["file_loaders"]).keys()
DICT_MIMETYPES = dict(config.loader["mimetypes"])

# Create an APIRouter instance
router = APIRouter()


@router.get(
    "/supported/types",
    description="""Get supported file types for indexing.

**Response:**
Returns a list of supported file extensions and MIME types that can be indexed by the system.
""",
)
async def get_supported_types():
    """
    Get a list of supported types for indexing.

    Returns:
        JSON object containing:
        - `extensions`: List of supported file extensions.
        - `mimetypes`: List of supported MIME types.
    """
    list_extensions = list(ACCEPTED_FILE_FORMATS)
    list_mimetypes = list(DICT_MIMETYPES)
    resp = {"extensions": list_extensions, "mimetypes": list_mimetypes}
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
}
```

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
    indexer=Depends(get_indexer),
    task_state_manager=Depends(get_task_state_manager),
    vectordb=Depends(get_vectordb),
    user=Depends(require_partition_editor),
):
    log = logger.bind(
        file_id=file_id,
        partition=partition,
        filename=file.filename,
        user=user.get("display_name"),
    )

    if await vectordb.file_exists.remote(file_id, partition):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"File '{file_id}' already exists in partition {partition}",
        )

    save_dir = Path(DATA_DIR)
    try:
        file_path = await save_file_to_disk(file, save_dir, with_random_prefix=True)
    except Exception as e:
        log.exception("Failed to save file to disk.", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )

    metadata.update({"source": str(file_path), "filename": file.filename})
    file_stat = Path(file_path).stat()

    # Append extra metadata
    metadata["file_size"] = human_readable_size(file_stat.st_size)
    metadata["created_at"] = datetime.fromtimestamp(file_stat.st_ctime).isoformat()
    metadata["file_id"] = file_id

    # Indexing the file
    task = indexer.add_file.remote(
        path=file_path, metadata=metadata, partition=partition, user=user
    )
    await task_state_manager.set_state.remote(task.task_id().hex(), "QUEUED")
    await task_state_manager.set_object_ref.remote(task.task_id().hex(), {"ref": task})
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "task_status_url": str(
                request.url_for("get_task_status", task_id=task.task_id().hex())
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
    indexer=Depends(get_indexer),
    vectordb=Depends(get_vectordb),
    user=Depends(require_partition_editor),
):
    if not await vectordb.file_exists.remote(file_id, partition):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{file_id}' not found in partition '{partition}'",
        )
    await indexer.delete_file.remote(file_id, partition)
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
}
```

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
    indexer=Depends(get_indexer),
    task_state_manager=Depends(get_task_state_manager),
    vectordb=Depends(get_vectordb),
    user=Depends(require_partition_editor),
):
    log = logger.bind(file_id=file_id, partition=partition, filename=file.filename)

    if not await vectordb.file_exists.remote(file_id, partition):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{file_id}' not found in partition '{partition}'",
        )

    # Delete the existing file from the vector database
    await indexer.delete_file.remote(file_id, partition)

    save_dir = Path(DATA_DIR)
    save_dir.mkdir(parents=True, exist_ok=True)
    file_path = save_dir / Path(file.filename).name
    metadata.update({"source": str(file_path), "filename": file.filename})

    try:
        with open(file_path, "wb") as buffer:
            buffer.write(await file.read())
        log.info("File saved to disk.")
    except Exception:
        log.exception("Failed to save file to disk.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save uploaded file.",
        )
    file_stat = Path(file_path).stat()

    # Append extra metadata
    metadata["file_size"] = human_readable_size(file_stat.st_size)
    metadata["created_at"] = datetime.fromtimestamp(file_stat.st_ctime).isoformat()
    metadata["file_id"] = file_id

    # Indexing the file
    task = indexer.add_file.remote(
        path=file_path, metadata=metadata, partition=partition
    )
    await task_state_manager.set_state.remote(task.task_id().hex(), "QUEUED")
    await task_state_manager.set_object_ref.remote(task.task_id().hex(), {"ref": task})

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "task_status_url": str(
                request.url_for("get_task_status", task_id=task.task_id().hex())
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
    metadata: Optional[Any] = Depends(validate_metadata),
    indexer=Depends(get_indexer),
    user=Depends(require_partition_editor),
    user_partitions=Depends(current_user_partitions),
):
    metadata["file_id"] = file_id

    # Make sure partition role is valid if partition is being changed
    if "partition" in metadata:
        await ensure_partition_role(
            partition=metadata["partition"],
            user=user,
            user_partitions=user_partitions,
            required_role="editor",
        )

    await indexer.update_file_metadata.remote(file_id, metadata, partition, user=user)
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
    metadata: Optional[Any] = Depends(validate_metadata),
    source_partition: str = Form(...),
    source_file_id: str = Form(...),
    indexer=Depends(get_indexer),
    user=Depends(require_partition_editor),
    user_partitions=Depends(current_user_partitions),
):
    # Make sure user has access to destination partition
    await ensure_partition_role(
        partition=source_partition,
        user=user,
        user_partitions=user_partitions,
        required_role="viewer",
    )
    metadata["file_id"] = file_id
    metadata["partition"] = partition

    await indexer.copy_file.remote(
        file_id=source_file_id, metadata=metadata, partition=source_partition, user=user
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
    task_state_manager=Depends(get_task_state_manager),
    task_details=Depends(require_task_owner),
):
    # fetch task state
    state = await task_state_manager.get_state.remote(task_id)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found.",
        )

    # format the response
    content: dict[str, Any] = {
        "task_id": task_id,
        "task_state": state,
        "details": task_details,
    }

    if state == "FAILED":
        content["error_url"] = str(request.url_for("get_task_error", task_id=task_id))

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
    task_state_manager=Depends(get_task_state_manager),
    task_details=Depends(require_task_owner),
):
    try:
        error = await task_state_manager.get_error.remote(task_id)
        if error is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No error found for task '{task_id}'.",
            )
        return {"task_id": task_id, "traceback": error.splitlines()}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve task error.",
        )


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
    task_id: str, max_lines: int = 100, task_details=Depends(require_task_owner)
):
    try:
        if not LOG_FILE.exists():
            raise HTTPException(status_code=500, detail="Log file not found.")

        logs = []
        with open(LOG_FILE, "r", errors="replace") as f:
            for line in reversed(list(f)):
                try:
                    record = json.loads(line).get("record", {})
                    if record.get("extra", {}).get("task_id") == task_id:
                        logs.append(
                            f"{record['time']['repr']} - {record['level']['name']} - {record['message']} - {(record['extra'])}"
                        )
                        if len(logs) >= max_lines:
                            break
                except json.JSONDecodeError:
                    continue

        if not logs:
            raise HTTPException(
                status_code=404, detail=f"No logs found for task '{task_id}'"
            )

        return JSONResponse(
            content={"task_id": task_id, "logs": logs[::-1]}
        )  # restore order
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch logs: {str(e)}")


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
    task_state_manager=Depends(get_task_state_manager),
    task_details=Depends(require_task_owner),
):
    try:
        obj_ref = await task_state_manager.get_object_ref.remote(task_id)
        if obj_ref is None:
            raise HTTPException(404, f"No ObjectRef stored for task {task_id}")

        ray.cancel(obj_ref["ref"], recursive=True)
        return {"message": f"Cancellation signal sent for task {task_id}"}
    except Exception as e:
        logger.exception("Failed to cancel task.")
        raise HTTPException(status_code=500, detail=str(e))
