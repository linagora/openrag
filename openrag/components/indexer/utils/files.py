import asyncio
import re
import secrets
import time
from pathlib import Path
from typing import Dict, Optional

import aiofiles
import consts
from fastapi import UploadFile


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


async def serialize_file(task_id: str, path: str, metadata: Optional[Dict] = {}):
    import ray
    from ray.exceptions import TaskCancelledError

    serializer = ray.get_actor("DocSerializer", namespace="openrag")
    # Kick off the remote task
    future = serializer.serialize_document.remote(task_id, path, metadata=metadata)

    # Wait for it to complete, with timeout
    ready, _ = await asyncio.to_thread(ray.wait, [future])

    if ready:
        try:
            doc = await ready[0]
            return doc
        except TaskCancelledError:
            raise
        except Exception:
            raise
    else:
        ray.cancel(future, recursive=True)
        raise TimeoutError(f"Serialization task {task_id} timed out after seconds")
