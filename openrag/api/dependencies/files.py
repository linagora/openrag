from typing import Any

from config import load_config
from core.indexing import validators as core_validators
from fastapi import Depends, Form, UploadFile

config = load_config()

FORBIDDEN_CHARS_IN_FILE_ID = set("/")
ACCEPTED_FILE_FORMATS = config.loader.file_loaders.model_dump().keys()
DICT_MIMETYPES = config.loader.mimetypes.to_dict()


async def validate_file_id(file_id: str):
    return core_validators.validate_file_id(file_id, FORBIDDEN_CHARS_IN_FILE_ID)


async def validate_metadata(metadata: Any | None = Form(None)):
    return core_validators.parse_metadata(metadata)


async def validate_file_format(
    file: UploadFile,
    metadata: dict = Depends(validate_metadata),
):
    core_validators.validate_file_format(
        filename=file.filename,
        accepted_formats=ACCEPTED_FILE_FORMATS,
        accepted_mimetypes=DICT_MIMETYPES.keys(),
        mimetype=metadata.get("mimetype"),
    )
    return file
