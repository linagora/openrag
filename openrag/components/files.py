from pathlib import Path
import aiofiles
from fastapi import UploadFile
import consts
import time
import secrets

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
