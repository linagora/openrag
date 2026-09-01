"""Admin routes for the Phase 14 model endpoint registry.

The router is intentionally transport-only: auth, request validation and
response shaping live here, while endpoint persistence and validation are
delegated to the service resolved from the DI container.
"""

from datetime import UTC, datetime

from api.dependencies.auth import require_admin
from api.routers.user.chat import invalidate_max_model_tokens, prime_max_model_tokens
from api.schemas.admin.model_endpoint_schemas import (
    CreateModelEndpointRequest,
    ModelEndpointResponse,
    ModelEndpointType,
    RevealApiKeyResponse,
    UpdateModelEndpointRequest,
    ValidateEndpointRequest,
    ValidateEndpointResponse,
    validate_llm_token_extra,
    validate_stt_fields,
)
from core.config.model_endpoints import ModelEndpointRow
from core.utils.exceptions import ValidationError
from core.utils.logging import get_logger
from di.providers import get_model_endpoint_service
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status

router = APIRouter(dependencies=[Depends(require_admin)])
logger = get_logger()


def _same_endpoint_url(left: str, right: str) -> bool:
    """Compare endpoint URLs after the schema-level normalization rules."""
    return left.strip().rstrip("/") == right.strip().rstrip("/")


async def _reprime_llm_token_cache(model_type: str) -> None:
    """Refresh the auto-probed ``max_model_len`` cache after an LLM endpoint
    write (create/update/delete/set-default).

    ``config.models.llm`` itself is already refreshed synchronously inside
    the service call (``ModelEndpointService.load_all()``), but the
    ``/v1/models`` auto-probe cache (``chat._max_model_tokens_by_name``) is
    a separate cache that ``prime_max_model_tokens`` otherwise only
    populates once at process startup — without this, a newly added or
    edited LLM endpoint keeps falling back to the global context-size
    default until the next restart. No-op for non-LLM endpoint types.
    Best-effort: a probe failure must not fail the admin's CRUD request.

    Scheduled as a FastAPI background task (runs after the response is sent)
    rather than awaited inline: ``prime_max_model_tokens`` probes every
    registered LLM endpoint's ``/v1/models`` serially, so a single slow or
    unreachable endpoint would otherwise stall the admin's write by several
    probe timeouts for a refresh that is only best-effort anyway.
    """
    if model_type != "llm":
        return
    try:
        await prime_max_model_tokens()
    except Exception:
        logger.exception("Failed to refresh auto-probed LLM token cache after endpoint write")


def _refresh_llm_token_cache(background_tasks: BackgroundTasks, model_type: str) -> None:
    """Invalidate the auto-probed token cache now; re-probe after the response.

    The two halves are deliberately split across the response boundary. The
    service has already swapped ``config.models.llm`` synchronously, so leaving
    the probed cache untouched until the background task finishes would let a
    chat request in that window resolve the *new* endpoint but preflight
    against the *old* one's probed ``max_model_len``. Invalidating inline makes
    that window fall back to the conservative global default instead of a wrong
    value, while the slow part — probing every endpoint — still runs off the
    request path so a dead endpoint can't stall an admin write.

    No-op for non-LLM endpoint types: they have no entry in this cache, so
    neither half has anything to do.
    """
    if model_type != "llm":
        return
    invalidate_max_model_tokens()
    background_tasks.add_task(_reprime_llm_token_cache, model_type)


def _reject_non_llm_token_budgets(model_type: str, extra: dict | None) -> None:
    """Apply the LLM token-budget rules to *extra*, but only for LLM endpoints.

    ``UpdateModelEndpointRequest`` carries no ``model_type`` (it is a path
    parameter), so unlike the create schema it cannot scope this check itself.
    Validating here keeps ``max_llm_context_size`` / ``max_output_tokens`` from
    being globally reserved names: an embedder, reranker or VLM may legitimately
    carry same-named provider metadata of any shape, and the admin UI now
    preserves those keys, so re-submitting an untouched endpoint must not 422.
    """
    if model_type != "llm":
        return
    try:
        validate_llm_token_extra(extra)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


async def _reject_invalid_stt_fields(model_type: str, name: str, fields: dict, service) -> None:
    """Validate STT-only fields, including the model retained by a partial update."""
    if model_type != "stt":
        return
    if "model_name" not in fields and "extra" not in fields:
        return
    try:
        if "model_name" in fields:
            model_name = fields["model_name"]
        else:
            # ``extra``-only writes retain the stored model name. Fetch it so
            # an env-seeded endpoint with no model cannot be updated into a
            # silently ignored STT configuration.
            model_name = (await service.get_model_endpoint(name=name, model_type=model_type)).model_name
        validate_stt_fields(model_name, fields.get("extra"))
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


@router.post(
    "/",
    response_model=ModelEndpointResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_model_endpoint(
    body: CreateModelEndpointRequest,
    background_tasks: BackgroundTasks,
    service=Depends(get_model_endpoint_service),
):
    """Register a named inference endpoint."""
    now = datetime.now(UTC)
    row = ModelEndpointRow(**body.model_dump(), created_at=now, updated_at=now)
    result = await service.create_model_endpoint(row)
    _refresh_llm_token_cache(background_tasks, body.model_type)
    return result


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
    background_tasks: BackgroundTasks,
    service=Depends(get_model_endpoint_service),
):
    """Update a registered inference endpoint."""
    fields = body.model_dump(exclude_unset=True)
    _reject_non_llm_token_budgets(model_type, fields.get("extra"))
    await _reject_invalid_stt_fields(model_type, name, fields, service)
    if "name" in fields:
        fields["new_name"] = fields.pop("name")
    result = await service.update_model_endpoint(
        name=name,
        model_type=model_type,
        **fields,
    )
    _refresh_llm_token_cache(background_tasks, model_type)
    return result


@router.delete("/{model_type}/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model_endpoint(
    model_type: ModelEndpointType,
    name: str,
    background_tasks: BackgroundTasks,
    service=Depends(get_model_endpoint_service),
):
    """Delete a registered inference endpoint."""
    await service.delete_model_endpoint(name=name, model_type=model_type)
    _refresh_llm_token_cache(background_tasks, model_type)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{model_type}/{name}/set-default", response_model=ModelEndpointResponse)
async def set_default_model_endpoint(
    model_type: ModelEndpointType,
    name: str,
    background_tasks: BackgroundTasks,
    service=Depends(get_model_endpoint_service),
):
    """Promote a registered endpoint to the default for its type."""
    await service.set_default(model_type=model_type, name=name)
    _refresh_llm_token_cache(background_tasks, model_type)
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
    and supported model capabilities."""
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
        model_type=body.model_type,
        model_name=body.model_name,
        api_key=api_key,
        timeout=body.timeout,
        extra=body.extra if body.model_type == "stt" else None,
    )


@router.post("/{model_type}/{name}/validate", response_model=ValidateEndpointResponse)
async def validate_model_endpoint(
    model_type: ModelEndpointType,
    name: str,
    service=Depends(get_model_endpoint_service),
):
    """Probe a registered endpoint for reachability and model capabilities."""
    endpoint = await service.get_model_endpoint(name=name, model_type=model_type)
    return await service.validate_endpoint(
        url=endpoint.endpoint,
        model_type=model_type,
        model_name=endpoint.model_name,
        api_key=endpoint.extra.get("api_key"),
        timeout=endpoint.timeout,
        extra=endpoint.extra if model_type == "stt" else None,
    )
