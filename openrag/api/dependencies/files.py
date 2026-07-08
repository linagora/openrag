import os
from pathlib import Path
from typing import Any

import aiofiles
import consts
from core.indexing import validators as core_validators
from core.utils.exceptions import ValidationError
from core.utils.filename import make_unique_filename
from di.providers import get_config
from fastapi import Depends, Form, UploadFile

FORBIDDEN_CHARS_IN_FILE_ID = set("/")


def _max_upload_size_bytes() -> int:
    """Maximum accepted upload size in bytes, resolved at call time.

    Streamed writes are bounded so a single request cannot exhaust disk/RAM
    (the per-user quota limits file count, not bytes). 0 or negative disables
    the limit. Override with ``MAX_UPLOAD_SIZE_MB``.

    Read lazily rather than at import: ``api.main`` imports the admin routers
    (and therefore this module) before it calls ``load_config()`` /
    ``load_dotenv()``, so evaluating at import would ignore a
    ``MAX_UPLOAD_SIZE_MB`` set in ``.env`` and silently use the default.
    """
    return int(os.getenv("MAX_UPLOAD_SIZE_MB", "1024")) * 1024 * 1024


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
    dest_dir = dest_dir.resolve()

    raw_filename = file.filename or ""
    safe_basename = raw_filename.replace("\\", "/").split("/")[-1].strip()
    if not safe_basename:
        raise ValidationError("Uploaded file must have a filename.", status_code=400)

    filename = make_unique_filename(safe_basename) if with_random_prefix else safe_basename
    file_path = (dest_dir / filename).resolve()
    try:
        file_path.relative_to(dest_dir)
    except ValueError:
        raise ValidationError("Uploaded filename resolves outside destination directory.", status_code=400)

    max_bytes = _max_upload_size_bytes()
    total = 0
    try:
        async with aiofiles.open(file_path, "wb") as buffer:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                total += len(chunk)
                if max_bytes > 0 and total > max_bytes:
                    raise ValidationError(
                        f"File exceeds the maximum allowed size of {max_bytes // (1024 * 1024)} MB.",
                        status_code=413,
                    )
                await buffer.write(chunk)
    except ValidationError:
        # Remove the partially written file before propagating.
        file_path.unlink(missing_ok=True)
        raise

    return file_path
