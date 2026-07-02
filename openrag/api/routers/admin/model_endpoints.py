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
    RevealApiKeyResponse,
    UpdateModelEndpointRequest,
    ValidateEndpointRequest,
    ValidateEndpointResponse,
)
from core.config.model_endpoints import ModelEndpointRow
from core.utils.logging import get_logger
from di.providers import get_model_endpoint_service
from fastapi import APIRouter, Depends, HTTPException, Response, status

router = APIRouter(dependencies=[Depends(require_admin)])
logger = get_logger()


def _same_endpoint_url(left: str, right: str) -> bool:
    """Compare endpoint URLs after the schema-level normalization rules."""
    return left.strip().rstrip("/") == right.strip().rstrip("/")


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
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields:
        fields["new_name"] = fields.pop("name")
    return await service.update_model_endpoint(
        name=name,
        model_type=model_type,
        **fields,
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


@router.post("/{model_type}/{name}/reveal-api-key", response_model=RevealApiKeyResponse)
async def reveal_model_endpoint_api_key(
    model_type: ModelEndpointType,
    name: str,
    service=Depends(get_model_endpoint_service),
):
    """Return the stored API key only after an explicit admin reveal action."""
    endpoint = await service.get_model_endpoint(name=name, model_type=model_type)
    api_key = endpoint.extra.get("api_key")
    logger.bind(
        model_type=model_type,
        name=name,
        has_api_key=isinstance(api_key, str),
    ).info("Model endpoint API key revealed.")
    return {"api_key": api_key if isinstance(api_key, str) else None}


@router.post("/validate", response_model=ValidateEndpointResponse)
async def validate_endpoint_draft(
    body: ValidateEndpointRequest,
    service=Depends(get_model_endpoint_service),
):
    """Probe arbitrary endpoint values (before they are saved) for reachability
    and model availability."""
    api_key = body.api_key
    if api_key is None and body.stored_api_key_model_type and body.stored_api_key_name:
        endpoint = await service.get_model_endpoint(
            name=body.stored_api_key_name,
            model_type=body.stored_api_key_model_type,
        )
        if not _same_endpoint_url(body.endpoint, endpoint.endpoint):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Stored API key can only be reused with its saved endpoint URL.",
            )
        api_key = endpoint.extra.get("api_key")
    return await service.validate_endpoint(
        url=body.endpoint,
        model_name=body.model_name,
        api_key=api_key,
    )


@router.post("/{model_type}/{name}/validate", response_model=ValidateEndpointResponse)
async def validate_model_endpoint(
    model_type: ModelEndpointType,
    name: str,
    service=Depends(get_model_endpoint_service),
):
    """Probe a registered endpoint for reachability and model availability."""
    endpoint = await service.get_model_endpoint(name=name, model_type=model_type)
    return await service.validate_endpoint(
        url=endpoint.endpoint,
        model_name=endpoint.model_name,
        api_key=endpoint.extra.get("api_key"),
    )
