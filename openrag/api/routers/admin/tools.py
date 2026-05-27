"""Tools routes — thin HTTP layer over :class:`ConversionService`.

Phase 8E: the ``extractText`` serialization moved to
``services.orchestrators.conversion_service.ConversionService`` (the Ray
``DocSerializer`` actor now sits behind the ``FileSerializer`` port).
This module keeps HTTP transport only: the saved-file IO + cleanup,
tool validation/dispatch, and the 4xx/5xx error mapping whose exact
``{"detail": ...}`` body the legacy endpoint returned via
``HTTPException``.
"""

import json
from pathlib import Path

from api.dependencies.files import validate_file_format, validate_metadata
from components.indexer.utils.files import save_file_to_disk
from config import load_config
from di.providers import get_conversion_service
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from utils.logger import get_logger

logger = get_logger()
config = load_config()
data_dir = config.paths.data_dir

router = APIRouter()


class ToolInfo(BaseModel):
    name: str
    description: str


AVAILABLE_TOOLS: list[ToolInfo] = [
    ToolInfo(
        name="extractText",
        description="Extract raw text from a file (PDF, Office, audio, etc.)",
    ),
]


def validate_tool(tool: str = Form(...)):
    try:
        json_tool = json.loads(tool)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400,
            detail="Invalid 'tool' field: must be valid JSON.",
        )

    name = json_tool.get("name")
    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid 'tool' field: missing 'name'.",
        )

    if not any(t.name == name for t in AVAILABLE_TOOLS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Tool {name} not found",
        )

    return json_tool


@router.get(
    "/tools",
    response_model=list[ToolInfo],
    summary="List available tools",
    description="""List available tools
**Response Format:**
[
    {
        "name": "Tool name",
        "description": "Tool description"
    }
]
""",
)
async def list_tools():
    return AVAILABLE_TOOLS


@router.post(
    "/tools/execute",
    summary="Tools execution",
    description="""Execute given tool
**Response Format:**
{
    "message": "<Tool output>"
}
""",
)
async def execute_tool(
    file: UploadFile = Depends(validate_file_format),
    tool: str = Depends(validate_tool),
    metadata: dict = Depends(validate_metadata),
    service=Depends(get_conversion_service),
):
    file_path = None
    try:
        if tool["name"] == "extractText":
            file_path = await save_file_to_disk(file, Path(data_dir), with_random_prefix=True)

            logger.debug(f"Execute tool extractText with file {file.filename}")
            sanitized_content = await service.serialize_file(
                file_path=str(file_path),
                filename=file.filename,
                metadata=metadata,
            )
            logger.debug("extractText done")

            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={"message": sanitized_content},
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Tool {tool['name']} not found",
            )

    except HTTPException:
        raise
    except TimeoutError:
        logger.warning("Tool execution timed out.", extra={"filename": file.filename})
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Tool execution timed out. The file may be too large or complex to process.",
        )
    except Exception as e:
        logger.exception("Failed during tool execution.", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Tool execution failed due to an internal error.",
        )
    finally:
        # Cleanup of the temporary file
        if file_path is not None:
            try:
                if file_path.exists():
                    file_path.unlink()
                    logger.debug(f"Temporary file {file_path} deleted from disk.")
            except Exception as cleanup_err:
                logger.warning(
                    "Failed to delete temporary file.",
                    extra={"error": str(cleanup_err), "path": str(file_path)},
                )
