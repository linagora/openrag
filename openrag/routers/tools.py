import json
from pathlib import Path

from components.app.service import OpenRAGApplicationService
from components.indexer.utils.files import save_file_to_disk
from config import load_config
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from utils.logger import get_logger

from .utils import (
    validate_file_format,
    validate_metadata,
)

logger = get_logger()
config = load_config()
data_dir = config.paths.data_dir

router = APIRouter()
app_service = OpenRAGApplicationService()


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
    payload = await app_service.list_tools(tools=AVAILABLE_TOOLS)
    return payload["tools"]


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
):
    save_dir = Path(data_dir)
    file_path = None
    try:
        file_path = await save_file_to_disk(file, save_dir, with_random_prefix=True)
        metadata.update({"source": str(file_path), "filename": file.filename})

        logger.debug(f"Execute tool {tool['name']} with file {file.filename}")
        result = await app_service.execute_tool(
            tool_name=tool["name"],
            file_path=file_path,
            metadata=metadata,
        )
        logger.debug(f"Tool {tool['name']} done for file {file.filename}")

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=result,
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except HTTPException:
        raise
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
