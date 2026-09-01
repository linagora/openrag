"""Admin routes for the Phase 14 pipeline preset registry."""

from api.dependencies.auth import require_admin
from api.schemas.admin.preset_schemas import (
    CreatePresetRequest,
    PresetOptionsResponse,
    PresetResponse,
    PresetType,
    UpdatePresetRequest,
)
from core.chunking import chunking_registry
from core.config.indexation_pipeline import PARSING_STRATEGIES
from core.config.table_reconstruction import TABLE_RECONSTRUCTION_MODES
from core.rerankers.registry import reranker_registry
from core.retrieval import retriever_registry
from di.providers import get_preset_service
from fastapi import APIRouter, Depends, Response, status

router = APIRouter(dependencies=[Depends(require_admin)])

_DEFAULT_RERANKER_PROVIDERS = ["infinity", "openai", "tei"]

# The selectable PDF backends. Shares the IndexationPipelineConfig constant so
# the exposed options can never drift from what the model accepts. ``None``
# (inherit the global PDFLOADER) is the field default, not an explicit choice,
# so it is not surfaced here.
_PARSING_STRATEGIES = list(PARSING_STRATEGIES)


def _registered_or_default(registered: list[str], defaults: list[str]) -> list[str]:
    """Return registry values, falling back to known defaults before DI imports providers."""
    return registered or defaults


@router.get("/options", response_model=PresetOptionsResponse)
async def get_preset_options():
    """Return available preset strategy choices."""
    return PresetOptionsResponse(
        chunking_strategies=chunking_registry.list_registered(),
        parsing_strategies=_PARSING_STRATEGIES,
        table_reconstruction_modes=list(TABLE_RECONSTRUCTION_MODES),
        retrieval_types=retriever_registry.list_registered(),
        reranker_providers=_registered_or_default(
            reranker_registry.list_registered(),
            _DEFAULT_RERANKER_PROVIDERS,
        ),
    )


@router.post("/", response_model=PresetResponse, status_code=status.HTTP_201_CREATED)
async def create_preset(
    body: CreatePresetRequest,
    service=Depends(get_preset_service),
):
    """Create a named pipeline preset."""
    return await service.create_preset(**body.model_dump())


@router.get("/", response_model=list[PresetResponse])
async def list_presets(
    preset_type: PresetType | None = None,
    service=Depends(get_preset_service),
):
    """List pipeline presets, optionally filtered by type."""
    return await service.list_presets(preset_type=preset_type)


@router.get("/{preset_type}/{name}", response_model=PresetResponse)
async def get_preset(
    preset_type: PresetType,
    name: str,
    service=Depends(get_preset_service),
):
    """Return one pipeline preset."""
    return await service.get_preset(name=name, preset_type=preset_type)


@router.put("/{preset_type}/{name}", response_model=PresetResponse)
async def update_preset(
    preset_type: PresetType,
    name: str,
    body: UpdatePresetRequest,
    service=Depends(get_preset_service),
):
    """Update a pipeline preset."""
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields:
        fields["new_name"] = fields.pop("name")
    return await service.update_preset(
        name=name,
        preset_type=preset_type,
        **fields,
    )


@router.delete("/{preset_type}/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_preset(
    preset_type: PresetType,
    name: str,
    service=Depends(get_preset_service),
):
    """Delete a pipeline preset."""
    await service.delete_preset(name=name, preset_type=preset_type)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
