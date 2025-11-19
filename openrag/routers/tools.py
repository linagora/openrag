import json
from typing import Dict
from pathlib import Path

from config import load_config
from fastapi import APIRouter, Depends, Form, HTTPException, status, UploadFile
from fastapi.responses import JSONResponse
from components.files import save_file_to_disk, serialize_file
from utils.logger import get_logger
import ray
from pydantic import BaseModel
from .utils import (
    validate_file_format,
    validate_metadata,
)

logger = get_logger()
config = load_config()
data_dir = config.paths.data_dir

router = APIRouter()


class ToolInfo(BaseModel):
    name: str
    description: str


AVAILABLE_TOOLS: Dict[str, ToolInfo] = {
    "extractText": ToolInfo(
        name="Extract text",
        description="Extract raw text from a file (PDF, Office, audio, etc.)",
    ),
}


def validate_tool(tool: str = Form(...)):
    try:
        json_tool = json.loads(tool)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400,
            detail="Invalid 'tool' field: must be valid JSON.",
        )
    
    if json_tool['name'] not in AVAILABLE_TOOLS.keys():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Tool {tool} not found",
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
        "tool_name": {
            "name": "Tool name",
            "description": "Tool description"
        }
    }
]
""",
)
async def list_tools():
    return list(AVAILABLE_TOOLS.values())


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
    metadata: dict = Depends(validate_metadata)):
    save_dir = Path(data_dir)
    try:
        if tool['name'] == "extractText":
            file_path = await save_file_to_disk(file, save_dir, with_random_prefix=True)
            metadata.update({"source": str(file_path), "filename": file.filename})

            task_id = ray.get_runtime_context().get_task_id()

            logger.debug(
                f"Execute tool extractText for task {task_id} with file {file.filename}"
            )
            doc = await serialize_file(task_id, path=file_path, metadata=metadata)
            logger.debug(f"extractText done for task {task_id}")

            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={"message": doc.page_content},
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Tool {tool} not found",
            )

    except Exception as e:
        logger.exception("Failed during tool execution.", extra={"error": str(e)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )
    finally:
        # Cleanup of the temporary file
        try:
            if file_path.exists():
                file_path.unlink()
                logger.debug(f"Temporary file {file_path} deleted from disk.")
        except Exception as cleanup_err:
            logger.warning(
                "Failed to delete temporary file.",
                extra={"error": str(cleanup_err), "path": str(file_path)},
            )
