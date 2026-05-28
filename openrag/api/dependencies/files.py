from typing import Any

from core.indexing import validators as core_validators
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
