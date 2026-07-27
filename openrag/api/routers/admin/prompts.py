"""Admin routes for the DB prompt library.

Transport-only: auth, request validation, and response shaping live here;
persistence and resolution are delegated to ``PromptService`` from the DI
container. Per-partition assignment routes live alongside the other partition
sub-resources in ``partitions.py``.
"""

from api.dependencies.auth import require_admin
from api.schemas.admin.prompt_schemas import (
    CreatePromptRequest,
    PromptResponse,
    PromptTypeName,
    UpdatePromptRequest,
)
from di.providers import get_prompt_service
from fastapi import APIRouter, Depends, Query, Response, status

router = APIRouter(dependencies=[Depends(require_admin)])


@router.post("/", response_model=PromptResponse, status_code=status.HTTP_201_CREATED)
async def create_prompt(
    body: CreatePromptRequest,
    service=Depends(get_prompt_service),
):
    """Add a prompt to the library."""
    return await service.create_prompt(**body.model_dump())


@router.get("/", response_model=list[PromptResponse])
async def list_prompts(
    prompt_type: PromptTypeName | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    service=Depends(get_prompt_service),
):
    """List library prompts (optionally by type), each with an override count."""
    return await service.list_prompts(prompt_type=prompt_type, offset=offset, limit=limit)


@router.get("/{prompt_id}", response_model=PromptResponse)
async def get_prompt(
    prompt_id: str,
    service=Depends(get_prompt_service),
):
    """Return one library prompt."""
    return await service.get_prompt(prompt_id)


@router.patch("/{prompt_id}", response_model=PromptResponse)
async def update_prompt(
    prompt_id: str,
    body: UpdatePromptRequest,
    service=Depends(get_prompt_service),
):
    """Edit a prompt's name/content and/or promote it to default."""
    return await service.update_prompt(prompt_id, **body.model_dump(exclude_unset=True))


@router.put("/{prompt_id}/default", response_model=PromptResponse)
async def set_prompt_default(
    prompt_id: str,
    service=Depends(get_prompt_service),
):
    """Promote a prompt to the default for its type."""
    return await service.set_default(prompt_id)


@router.delete("/{prompt_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_prompt(
    prompt_id: str,
    service=Depends(get_prompt_service),
):
    """Delete a library prompt (rejected if it is the current default)."""
    await service.delete_prompt(prompt_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
