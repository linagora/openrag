import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import ray
from config.config import load_config
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
from utils.dependencies import Indexer, get_indexer, vectordb
from utils.logger import get_logger

# load logger
logger = get_logger()

# load config
config = load_config()
DATA_DIR = config.paths.data_dir
ACCEPTED_FILE_FORMATS = dict(config.loader["file_loaders"]).keys()
FORBIDDEN_CHARS_IN_FILE_ID = set("/")  # set('"<>#%{}|\\^`[]')
LOG_FILE = Path(config.paths.log_dir or "logs") / "app.json"

# Get the TaskStateManager actor
task_state_manager = ray.get_actor("TaskStateManager", namespace="ragondin")

# Create an APIRouter instance
router = APIRouter()


def is_file_id_valid(file_id: str) -> bool:
    return not any(c in file_id for c in FORBIDDEN_CHARS_IN_FILE_ID)


async def validate_file_id(file_id: str):
    if not is_file_id_valid(file_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File ID contains forbidden characters: {', '.join(FORBIDDEN_CHARS_IN_FILE_ID)}",
        )
    if not file_id.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="File ID cannot be empty."
        )
    return file_id


async def validate_file_format(file: UploadFile):
    file_extension = (
        file.filename.split(".")[-1].lower() if "." in file.filename else ""
    )
    if file_extension not in ACCEPTED_FILE_FORMATS:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file format: {file_extension}. Supported formats are: {', '.join(ACCEPTED_FILE_FORMATS)}",
        )
    return file


async def validate_metadata(metadata: Optional[Any] = Form(None)):
    try:
        processed_metadata = metadata or "{}"
        processed_metadata = json.loads(processed_metadata)
        return processed_metadata
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON in metadata"
        )


def _human_readable_size(size_bytes: int) -> str:
    """Convert bytes to a human-readable format (e.g., '2.4 MB')."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.2f} PB"

@router.get("/supported/types")
async def get_supported_types():
    list_extensions = list(ACCEPTED_FILE_FORMATS)
    return JSONResponse(content={"supported_types": list_extensions})


@router.post("/partition/{partition}/file/{file_id}")
async def add_file(
    request: Request,
    partition: str,
    file_id: str = Depends(validate_file_id),
    file: UploadFile = Depends(validate_file_format),
    metadata: dict = Depends(validate_metadata),
    indexer: Indexer = Depends(get_indexer),
):
    log = logger.bind(file_id=file_id, partition=partition, filename=file.filename)

    if vectordb.file_exists(file_id, partition):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"File '{file_id}' already exists in partition {partition}",
        )

    save_dir = Path(DATA_DIR)
    save_dir.mkdir(parents=True, exist_ok=True)
    file_path = save_dir / Path(file.filename).name
    metadata.update({"source": str(file_path), "filename": file.filename})

    try:
        with open(file_path, "wb") as buffer:
            buffer.write(await file.read())
        log.debug("File saved to disk.")
    except Exception:
        log.exception("Failed to save file to disk.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save uploaded file.",
        )
    file_stat = Path(file_path).stat()

    # Append extra metadata
    metadata["file_size"] = _human_readable_size(file_stat.st_size)
    metadata["created_at"] = datetime.fromtimestamp(file_stat.st_ctime).isoformat()
    metadata["file_id"] = file_id
    try:
        task = indexer.add_file.remote(
            path=file_path, metadata=metadata, partition=partition
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to queue file for indexing.",
        )

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "task_status_url": str(
                request.url_for("get_task_status", task_id=task.task_id().hex())
            )
        },
    )


@router.delete("/partition/{partition}/file/{file_id}")
async def delete_file(
    partition: str, file_id: str, indexer: Indexer = Depends(get_indexer)
):
    try:
        deleted = ray.get(indexer.delete_file.remote(file_id, partition))
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"File '{file_id}' not found in partition '{partition}'.",
            )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete file.",
        )

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/partition/{partition}/file/{file_id}")
async def put_file(
    request: Request,
    partition: str,
    file_id: str = Depends(validate_file_id),
    file: UploadFile = Depends(validate_file_format),
    metadata: dict = Depends(validate_metadata),
    indexer: Indexer = Depends(get_indexer),
):
    log = logger.bind(file_id=file_id, partition=partition, filename=file.filename)

    if not vectordb.file_exists(file_id, partition):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File '{file_id}' not found in partition '{partition}'.",
        )

    try:
        ray.get(indexer.delete_file.remote(file_id, partition))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete existing file.",
        )

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
    metadata["file_size"] = _human_readable_size(file_stat.st_size)
    metadata["created_at"] = datetime.fromtimestamp(file_stat.st_ctime).isoformat()
    metadata["file_id"] = file_id
    try:
        task = indexer.add_file.remote(
            path=file_path, metadata=metadata, partition=partition
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to queue file for indexing.",
        )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "task_status_url": str(
                request.url_for("get_task_status", task_id=task.task_id().hex())
            )
        },
    )


@router.patch("/partition/{partition}/file/{file_id}")
async def patch_file(
    partition: str,
    file_id: str = Depends(validate_file_id),
    metadata: Optional[Any] = Depends(validate_metadata),
    indexer: Indexer = Depends(get_indexer),
):
    if not vectordb.file_exists(file_id, partition):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File '{file_id}' not found in partition '{partition}'.",
        )

    metadata["file_id"] = file_id

    try:
        ray.get(indexer.update_file_metadata.remote(file_id, metadata, partition))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update metadata.",
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"message": f"Metadata for file '{file_id}' successfully updated."},
    )


@router.get("/task/{task_id}")
async def get_task_status(
    request: Request, task_id: str, indexer: Indexer = Depends(get_indexer)
):
    # fetch task state
    state = await indexer.get_task_status.remote(task_id)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' not found.",
        )

    # fetch task details
    details = await task_state_manager.get_details.remote(task_id)

    # format the response
    content: dict[str, Any] = {
        "task_id": task_id,
        "task_state": state,
        "details": details,
    }

    if state == "FAILED":
        content["error_url"] = str(request.url_for("get_task_error", task_id=task_id))

    return JSONResponse(status_code=status.HTTP_200_OK, content=content)


@router.get("/task/{task_id}/error")
async def get_task_error(task_id: str):
    try:
        error = await task_state_manager.get_error.remote(task_id)
        if error is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No error found for task '{task_id}'.",
            )
        return {"task_id": task_id, "traceback": error.splitlines()}
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve task error.",
        )


@router.get("/task/{task_id}/logs")
async def get_task_logs(task_id: str, max_lines: int = 100):
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
