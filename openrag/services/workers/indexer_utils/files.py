import re
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path

import aiofiles
import consts
from services.utils import load_config
from fastapi import HTTPException, UploadFile, status

config = load_config()
SERIALIZE_TIMEOUT = config.ray.indexer.serialize_timeout


def sanitize_filename(filename: str) -> str:
    # Split filename into name and extension
    path = Path(filename)
    name = path.stem
    ext = path.suffix

    # Remove special characters (keep only word characters and hyphens temporarily)
    name = re.sub(r"[^\w\-]", "_", name)

    # Replace hyphens with underscores
    name = name.replace("-", "_")

    # Collapse multiple underscores
    name = re.sub(r"_+", "_", name)

    # Remove leading/trailing underscores
    name = name.strip("_")

    # Reconstruct filename
    return name + ext


def make_unique_filename(filename: str) -> Path:
    ts = int(time.time() * 1000)
    rand = secrets.token_hex(2)
    unique_name = f"{ts}_{rand}_{filename}"
    return unique_name


async def save_file_to_disk(
    file: UploadFile,
    dest_dir: Path,
    chunk_size: int = consts.FILE_READ_CHUNK_SIZE,
    with_random_prefix: bool = False,
) -> Path:
    """
    Save file to disk by chunks, to avoid reading the whole file at once in memory.
    Returns the path to the saved file.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    if with_random_prefix:
        filename = make_unique_filename(file.filename)
    else:
        filename = file.filename
    file_path = dest_dir / filename

    async with aiofiles.open(file_path, "wb") as buffer:
        # Non-blocking I/O
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            await buffer.write(chunk)

    return file_path


async def serialize_file(task_id: str, path: str, metadata: dict | None = None):
    import ray
    from services.workers.ray_utils import call_ray_actor_with_timeout

    metadata = metadata or {}

    serializer = ray.get_actor("DocSerializer", namespace="openrag")
    future = serializer.serialize_document.remote(task_id, path, metadata=metadata)

    return await call_ray_actor_with_timeout(
        future,
        timeout=SERIALIZE_TIMEOUT,
        task_description=f"Serialization task {task_id}",
    )


def extract_temporal_fields(metadata: dict, temporal_fields: list) -> dict:
    result = {}
    for field in temporal_fields:
        if field not in metadata or metadata[field] is None:
            continue

        datetime_str = metadata[field]
        try:
            # Try parsing the provided datetime to ensure it's valid
            d = datetime.fromisoformat(datetime_str)
            if d.tzinfo is None:
                d = d.replace(tzinfo=UTC)
            result[field] = d.isoformat()
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid ISO 8601 datetime field ({datetime_str}) for field '{field}'.",
            )

    return result
