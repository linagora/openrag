from pathlib import Path
from typing import Any

import aiofiles
import consts
from core.indexing import validators as core_validators
from core.utils.filename import make_unique_filename
from di.providers import get_config
from fastapi import Depends, Form, UploadFile

FORBIDDEN_CHARS_IN_FILE_ID = set("/")


async def validate_file_id(file_id: str):
    return core_validators.validate_file_id(file_id, FORBIDDEN_CHARS_IN_FILE_ID)


async def validate_metadata(metadata: Any | None = Form(None)):
    return core_validators.parse_metadata(metadata)


async def validate_file_format(
    file: UploadFile,
    metadata: dict = Depends(validate_metadata),
    config=Depends(get_config),
):
    accepted_file_formats = config.loader.file_loaders.model_dump().keys()
    mimetypes = config.loader.mimetypes.to_dict()
    core_validators.validate_file_format(
        filename=file.filename,
        accepted_formats=accepted_file_formats,
        accepted_mimetypes=mimetypes.keys(),
        mimetype=metadata.get("mimetype"),
    )
    return file


async def save_file_to_disk(
    file: UploadFile,
    dest_dir: Path,
    chunk_size: int = consts.FILE_READ_CHUNK_SIZE,
    with_random_prefix: bool = False,
) -> Path:
    """Save an uploaded file to disk in chunks and return the saved path."""
    dest_dir.mkdir(parents=True, exist_ok=True)

    filename = make_unique_filename(file.filename) if with_random_prefix else file.filename
    file_path = dest_dir / filename

    async with aiofiles.open(file_path, "wb") as buffer:
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            await buffer.write(chunk)

    return file_path
