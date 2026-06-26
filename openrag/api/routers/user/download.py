"""Authorized source-file download, keyed by chunk id.

Serving ``DATA_DIR`` as a static mount would expose every tenant's source
documents to any authenticated caller who learned a filename (from a source
``file_url``, logs, or a since-revoked membership). Instead, this route mirrors
``/extract``'s partition-membership check: it resolves the file from the chunk's
stored ``source`` path and confines it to ``DATA_DIR``. The URL lives under
``/static`` so the middleware's browser ``?token=`` access works the same way.
"""

from pathlib import Path

from api.dependencies.auth import current_user_or_admin_partitions_list
from core.config import load_config
from core.indexing.validators import validate_file_id
from core.utils.exceptions import ValidationError
from core.utils.logging import get_logger
from di.providers import get_conversion_service
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

logger = get_logger()

_DATA_DIR = Path(load_config().paths.data_dir).resolve()

router = APIRouter()


@router.get("/static/{extract_id}", name="download_source")
async def download_source(
    extract_id: str,
    user_partitions=Depends(current_user_or_admin_partitions_list),
    service=Depends(get_conversion_service),
):
    """Download the source document a chunk came from, authorized by partition."""
    log = logger.bind(extract_id=extract_id)
    try:
        extract_id = validate_file_id(extract_id)
    except ValidationError as exc:
        raise HTTPException(status_code=exc.status_code or status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    chunk = await service.get_chunk(extract_id)
    if chunk is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found.")

    metadata = chunk.get("metadata", {})
    chunk_partition = metadata.get("partition")
    if chunk_partition not in user_partitions and user_partitions != ["all"]:
        log.warning("User does not have access to this file.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User does not have access to this file.",
        )

    source = metadata.get("source")
    if not source:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found.")

    # Resolve and confine the path to DATA_DIR to defeat any traversal.
    file_path = Path(source).resolve()
    if not file_path.is_relative_to(_DATA_DIR) or not file_path.is_file():
        log.warning("Resolved source path is outside DATA_DIR or missing.")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found.")

    return FileResponse(file_path, filename=file_path.name)
