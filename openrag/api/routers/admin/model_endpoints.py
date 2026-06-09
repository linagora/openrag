"""Admin routes for the Phase 14 model endpoint registry.

The router is intentionally transport-only: auth, request validation and
response shaping live here, while endpoint persistence and validation are
delegated to the service resolved from the DI container.
"""

from datetime import UTC, datetime

from api.dependencies.auth import require_admin
from api.schemas.admin.model_endpoint_schemas import (
    CreateModelEndpointRequest,
    ModelEndpointResponse,
    ModelEndpointType,
    UpdateModelEndpointRequest,
    ValidateEndpointResponse,
)
from core.config.model_endpoints import ModelEndpointRow
from di.providers import get_model_endpoint_service
from fastapi import APIRouter, Depends, Response, status

router = APIRouter(dependencies=[Depends(require_admin)])


@router.post(
    "/",
    response_model=ModelEndpointResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_model_endpoint(
    body: CreateModelEndpointRequest,
    service=Depends(get_model_endpoint_service),
):
    """Register a named inference endpoint."""
    now = datetime.now(UTC)
    row = ModelEndpointRow(**body.model_dump(), created_at=now, updated_at=now)
    return await service.create_model_endpoint(row)


@router.get("/", response_model=list[ModelEndpointResponse])
async def list_model_endpoints(
    model_type: ModelEndpointType | None = None,
    service=Depends(get_model_endpoint_service),
):
    """List registered inference endpoints, optionally filtered by type."""
    return await service.list_model_endpoints(model_type=model_type)


@router.get("/{model_type}/{name}", response_model=ModelEndpointResponse)
async def get_model_endpoint(
    model_type: ModelEndpointType,
    name: str,
    service=Depends(get_model_endpoint_service),
):
    """Return one registered inference endpoint."""
    return await service.get_model_endpoint(name=name, model_type=model_type)


@router.put("/{model_type}/{name}", response_model=ModelEndpointResponse)
async def update_model_endpoint(
    model_type: ModelEndpointType,
    name: str,
    body: UpdateModelEndpointRequest,
    service=Depends(get_model_endpoint_service),
):
    """Update a registered inference endpoint."""
    return await service.update_model_endpoint(
        name=name,
        model_type=model_type,
        **body.model_dump(exclude_unset=True),
    )


@router.delete("/{model_type}/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model_endpoint(
    model_type: ModelEndpointType,
    name: str,
    service=Depends(get_model_endpoint_service),
):
    """Delete a registered inference endpoint."""
    await service.delete_model_endpoint(name=name, model_type=model_type)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{model_type}/{name}/set-default", response_model=ModelEndpointResponse)
async def set_default_model_endpoint(
    model_type: ModelEndpointType,
    name: str,
    service=Depends(get_model_endpoint_service),
):
    """Promote a registered endpoint to the default for its type."""
    await service.set_default(model_type=model_type, name=name)
    return await service.get_model_endpoint(name=name, model_type=model_type)


@router.post("/{model_type}/{name}/validate", response_model=ValidateEndpointResponse)
async def validate_model_endpoint(
    model_type: ModelEndpointType,
    name: str,
    service=Depends(get_model_endpoint_service),
):
    """Probe a registered endpoint for reachability and model availability."""
    endpoint = await service.get_model_endpoint(name=name, model_type=model_type)
    return await service.validate_endpoint(url=endpoint.endpoint, model_name=endpoint.model_name)
