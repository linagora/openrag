"""OpenAI-compatible RAG endpoints — thin HTTP layer over QueryService.

Phase 8C.2: the RAG flow (query generation, retrieval, web search,
map-reduce, context/prompt assembly, streaming, and the
``[Sources: N]`` citation filtering) moved to
``services.orchestrators.query_service.QueryService``. This module keeps
HTTP transport only: model→partition resolution, token-limit validation,
the OpenAI ``/models`` listing, request-bound source-link building
(``__prepare_sources`` uses ``request.url_for`` so it stays here and is
handed to the service as a callable), and ``StreamingResponse`` /
``JSONResponse`` wrapping with the SSE error envelope.
"""

import asyncio
import json
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import consts
from api.dependencies.auth import (
    current_user,
    current_user_or_admin_partitions,
    current_user_or_admin_partitions_list,
)
from api.dependencies.llm import (
    get_openai_models,
    get_partition_name,
    truncate,
)
from api.routers.user.source_links import build_document_source_link
from api.schemas.user.chat import OpenAIChatCompletionRequest, OpenAICompletionRequest
from core.config import load_config
from core.models.preset import resolve_partition_chat_llm
from core.utils.exceptions import OpenRAGError
from core.utils.logging import get_logger
from core.utils.text import get_num_tokens, sanitize_text
from di.providers import get_config, get_partition_service, get_query_service
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

logger = get_logger()
router = APIRouter()

if TYPE_CHECKING:
    from core.config.root import Settings

# Auto-probed max model token limits, keyed by LLM endpoint registry name.
# Populated by ``prime_max_model_tokens`` which the application lifespan
# invokes during startup, once the model-endpoint registry is loaded. It is
# only the *middle* tier of ``get_max_model_tokens``: an admin-configured
# context size on the resolved LLM endpoint takes precedence, and
# ``config.llm_context`` is the final fallback.
_max_model_tokens_by_name: dict[str, int] = {}


def _runtime_config(settings: "Settings | None" = None) -> "Settings":
    return settings if settings is not None else load_config()


async def prime_max_model_tokens(settings: "Settings | None" = None) -> None:
    """Populate the per-endpoint auto-probed max model token cache.

    Called once from the FastAPI lifespan in ``api/main.py`` (replaces the
    deprecated ``@router.on_event("startup")`` hook), after the model-endpoint
    registry is loaded. Probes every registered LLM endpoint's ``/v1/models``
    for ``max_model_len`` — not just the default — so a partition's
    ``chat_llm`` preset gets its own auto-probed budget instead of silently
    falling back to the global ``llm_context`` default. Endpoints that alias
    the same underlying config (the ``"default"`` name always points at
    whichever endpoint is ``is_default``) are probed once and the result
    cached under every alias; endpoints with no ``model_name`` can't be
    matched in the ``/v1/models`` listing and are skipped. One endpoint's
    probe failure doesn't affect the others. Safe to call again — it just
    refreshes the cache.
    """
    global _max_model_tokens_by_name
    config = _runtime_config(settings)
    probed_by_identity: dict[int, int | None] = {}
    results: dict[str, int] = {}
    for name, endpoint in config.models.llm.items():
        if not endpoint.model_name:
            continue
        identity = id(endpoint)
        if identity not in probed_by_identity:
            probed_by_identity[identity] = await _fetch_max_model_tokens(
                base_url=endpoint.endpoint,
                model_id=endpoint.model_name,
                api_key=endpoint.extra.get("api_key", ""),
            )
        value = probed_by_identity[identity]
        if value is not None:
            results[name] = value
    _max_model_tokens_by_name = results


def _make_sse_error(message: str, code: str) -> str:
    """Format an error as an SSE data chunk for streaming responses."""
    chunk = {"error": {"message": message, "type": "error", "param": None, "code": code}}
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"


@router.get(
    "/models",
    summary="OpenAI-compatible model listing endpoint",
    description="""List available models in OpenAI-compatible format.

**Available Models:**
- `openrag-{partition_name}`: Query a specific partition
- `openrag-all`: Query across all accessible partitions

**Response Format:**
Returns models in OpenAI-compatible format with:
- `id`: Model identifier
- `object`: Always "model"
- `created`: Creation timestamp
- `owned_by`: Always "OpenRAG"

**Note:** Only partitions you have access to will be listed.
""",
    response_description="A list of available models in OpenAI format",
)
async def list_models(
    user_partitions=Depends(current_user_or_admin_partitions),
    partitions=Depends(get_partition_service),
):
    if [p["partition"] for p in user_partitions] == ["all"]:
        user_partitions = await partitions.list_partitions()
    logger.debug("Listing models", partition_count=len(user_partitions))

    models = [
        {
            "id": f"{consts.PARTITION_PREFIX}{partition['partition']}",
            "object": "model",
            "created": partition["created_at"],
            "owned_by": "OpenRAG",
        }
        for partition in user_partitions
    ]
    models.append({"id": f"{consts.PARTITION_PREFIX}all", "object": "model", "created": 0, "owned_by": "OpenRAG"})
    return JSONResponse(content={"object": "list", "data": models})


def __prepare_sources(request: Request, docs: list, web_results: list | None = None):
    def static_url(extract_id) -> str:
        # Authorized, partition-checked download keyed by chunk id (replaces the
        # open /static mount). The file is resolved server-side from the chunk.
        return str(request.url_for("download_source", extract_id=extract_id))

    def chunk_url(extract_id) -> str:
        return str(request.url_for("get_extract", extract_id=extract_id))

    links = []
    for doc in docs:
        doc_metadata = dict(doc.metadata)
        links.append(build_document_source_link(doc_metadata, static_url, chunk_url))
    for result in web_results or []:
        url = sanitize_text(result.url or "")
        if not url or urlparse(url).scheme not in ("http", "https"):
            continue
        links.append(
            {
                "source_type": "web",
                "url": url,
                "title": sanitize_text(result.title),
                "snippet": sanitize_text(result.snippet),
            }
        )
    return links


def is_direct_llm_model(
    request: OpenAIChatCompletionRequest | OpenAICompletionRequest,
    settings: "Settings | None" = None,
) -> bool:
    """True if the request should use the LLM directly (no RAG partition)."""
    config = _runtime_config(settings)
    return request.model is None or request.model == "" or request.model == config.llm.model


async def _fetch_max_model_tokens(*, base_url: str, model_id: str, api_key: str) -> int | None:
    """Fetch one endpoint's max model token limit from its ``/v1/models`` listing.

    Returns ``None`` — not a fallback value — when the endpoint is
    unreachable, doesn't serve ``model_id``, or doesn't report
    ``max_model_len``: this probes a single endpoint among potentially many,
    so the fallback decision belongs to the caller (``get_max_model_tokens``),
    not this helper.
    """
    try:
        openai_models = await get_openai_models(base_url=base_url, api_key=api_key)
        model = next((m for m in openai_models if m.id == model_id), None)
        if model is None:
            logger.warning(f"No model found for {model_id} at {base_url}.")
            return None
        model_data = model.model_dump() if hasattr(model, "model_dump") else model.dict()
        max_len = model_data.get("max_model_len") or model_data.get("model_extra", {}).get("max_model_len")
        if max_len is None:
            logger.warning(f"max_model_len not found for {model_id} at {base_url}.")
            return None
        logger.info(
            "Fetched max_model_len from vLLM at startup", model=model_id, base_url=base_url, max_model_len=int(max_len)
        )
        return int(max_len)
    except Exception as e:
        logger.warning("Failed to query /v1/models for max_model_len", base_url=base_url, model=model_id, error=str(e))
        return None


def _resolve_llm_endpoint_name(config: "Settings", partitions: list[str] | None) -> str:
    """Effective LLM endpoint registry name for *partitions*.

    Delegates to ``resolve_partition_chat_llm`` — the same consensus rule
    ``QueryService._resolve_llm`` uses to pick the LLM that actually answers
    the request — so the token preflight is checked against the budget of the
    model that will really be called, not always the global default. Falls
    back to the ``"default"`` alias (see ``ModelEndpointService``) when no
    single partition-scoped preset applies.
    """
    return resolve_partition_chat_llm(partitions, config.partitions) or "default"


def _effective_max_output_tokens(config: "Settings", partitions: list[str] | None = None) -> int:
    """Default output-token budget: the resolved LLM endpoint's admin-configured
    value when set, else the global ``llm_context`` fallback."""
    configured = config.models.llm_output_tokens(_resolve_llm_endpoint_name(config, partitions))
    return configured or int(config.llm_context.max_output_tokens)


def get_max_model_tokens(partitions: list[str] | None = None, settings: "Settings | None" = None) -> int:
    """Effective max context size for the token preflight.

    Precedence: the resolved LLM endpoint's admin-configured
    ``max_llm_context_size`` (editable in the admin UI; resolved from the
    partition's ``chat_llm`` preset when the request is scoped to partitions
    that agree on one, else the "default" endpoint) > the value auto-probed
    from that same endpoint's ``/v1/models`` at startup (see
    ``prime_max_model_tokens`` — cached per endpoint name, not just the
    default) > the global ``llm_context`` fallback.
    """
    config = _runtime_config(settings)
    name = _resolve_llm_endpoint_name(config, partitions)
    configured = config.models.llm_context_size(name)
    if configured is not None:
        return configured
    probed = _max_model_tokens_by_name.get(name)
    return probed or int(config.llm_context.max_llm_context_size)


def validate_tokens_limit(
    request: OpenAIChatCompletionRequest | OpenAICompletionRequest,
    max_tokens_allowed: int,
    settings: "Settings | None" = None,
    partitions: list[str] | None = None,
) -> tuple[bool, str]:
    """Validate if the request respects the maximum token limit."""
    try:
        config = _runtime_config(settings)
        _length_function = get_num_tokens()

        if isinstance(request, OpenAIChatCompletionRequest):
            message_tokens = sum(_length_function(m.content or "") + 4 for m in request.messages)
            default_output_tokens = _effective_max_output_tokens(config, partitions)
            requested_tokens = request.max_tokens or default_output_tokens
            total_tokens_needed = message_tokens + requested_tokens
            if total_tokens_needed > max_tokens_allowed:
                return False, (
                    f"Request exceeds maximum token limit. "
                    f"Messages: {message_tokens} tokens + "
                    f"Requested output: {requested_tokens} tokens = "
                    f"{total_tokens_needed} tokens. "
                    f"Maximum allowed: {max_tokens_allowed} tokens."
                )

        elif isinstance(request, OpenAICompletionRequest):
            prompt_tokens = _length_function(request.prompt)
            default_output_tokens = _effective_max_output_tokens(config, partitions)
            requested_tokens = request.max_tokens or default_output_tokens
            total_tokens_needed = prompt_tokens + requested_tokens
            if total_tokens_needed > max_tokens_allowed:
                return False, (
                    f"Request exceeds maximum token limit. "
                    f"Prompt: {prompt_tokens} tokens + "
                    f"Requested output: {requested_tokens} tokens = "
                    f"{total_tokens_needed} tokens. "
                    f"Maximum allowed: {max_tokens_allowed} tokens."
                )

        return True, ""
    except Exception as e:
        logger.warning("Error during token validation, skipping check", error=str(e))
        return True, ""


def check_tokens_limit(
    request: OpenAIChatCompletionRequest | OpenAICompletionRequest,
    log,
    settings: "Settings | None" = None,
    partitions: list[str] | None = None,
):
    """Validate token limit and raise HTTPException(413) if exceeded."""
    is_valid, error_message = validate_tokens_limit(
        request,
        max_tokens_allowed=get_max_model_tokens(partitions=partitions, settings=settings),
        settings=settings,
        partitions=partitions,
    )
    if not is_valid:
        log.info("Request exceeds token limit", detail=error_message)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=error_message,
        )


@router.post(
    "/chat/completions",
    summary="OpenAI compatible chat completion endpoint using RAG",
    description="""Generate chat completions with Retrieval-Augmented Generation (RAG).

**Model Selection:**
- `openrag-{partition_name}`: Query only the specified partition
- `openrag-all`: Query across all available partitions
- empty or model name: Use the LLM directly

**Request Format:**
Accepts OpenAI-compatible chat completion requests with:
- `messages`: Array of chat messages (last must be from user)
- `model`: Model/partition to use
- `stream`: Optional streaming response (true/false)
- Standard OpenAI parameters (temperature, max_tokens, etc.)

**Response:**
Returns OpenAI-compatible response with additional `extra` field containing:
- `sources`: Array of source documents with metadata and URLs

**Streaming:**
Set `stream: true` for Server-Sent Events (SSE) streaming responses.
""",
)
async def openai_chat_completion(
    request2: Request,
    request: OpenAIChatCompletionRequest = Body(...),
    user=Depends(current_user),
    user_partitions=Depends(current_user_or_admin_partitions_list),
    service=Depends(get_query_service),
    partition_service=Depends(get_partition_service),
    config=Depends(get_config),
):
    model_name = request.model or config.llm.model
    log = logger.bind(model=model_name, endpoint="/chat/completions")

    if not request.messages or request.messages[-1].role != "user" or not request.messages[-1].content:
        log.warning("Invalid request: missing or malformed user message.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The last message must be a non-empty user message",
        )

    log.debug("Received chat completion request with messages: {}", truncate(str(request.messages)))

    if is_direct_llm_model(request, config):
        partitions = None
    else:
        partitions = await get_partition_name(
            model_name,
            user_partitions,
            partition_service=partition_service,
            is_admin=user["is_admin"],
        )
        log.debug(f"Using partitions: {partitions}")

    # Bound the caller's input size in every mode, against the resolved
    # partition's chat_llm preset budget when one applies (else the default
    # LLM endpoint). RAG-injected context is added server-side and separately
    # capped (max_context_tokens), but the user's own messages must be limited
    # regardless of direct-LLM vs RAG.
    check_tokens_limit(request, log, config, partitions=partitions)

    def prep(docs, web):
        return __prepare_sources(request2, docs, web)

    if request.stream:

        async def stream_response():
            try:
                async for sse_line in service.chat_stream(
                    partitions=partitions,
                    payload=request.model_dump(exclude_none=True),
                    prepare_sources=prep,
                    model_name=model_name,
                ):
                    yield sse_line
            except asyncio.CancelledError:
                log.info("Client disconnected during streaming")
                return
            except OpenRAGError as e:
                log.warning("OpenRAG error during streaming", code=e.code, error=e.message)
                yield _make_sse_error(e.message, e.code)
            except Exception as e:
                # Unexpected (non-OpenRAGError) failure: attach the traceback so the
                # actual fault is visible, not just its str() — the client only gets
                # a generic message, so the log is the only record of the root cause.
                log.opt(exception=e).error("Error during streaming", error=str(e))
                yield _make_sse_error("An unexpected error occurred during streaming", "UNEXPECTED_ERROR")

        return StreamingResponse(stream_response(), media_type="text/event-stream")

    chunk = await service.chat(
        partitions=partitions,
        payload=request.model_dump(exclude_none=True),
        prepare_sources=prep,
        model_name=model_name,
    )
    log.debug("Returning non-streaming completion chunk.")
    return JSONResponse(content=chunk)


@router.post(
    "/completions",
    summary="OpenAI compatible completion endpoint using RAG",
    description="""Generate text completions with Retrieval-Augmented Generation (RAG).

**Model Selection:**
- `openrag-{partition_name}`: Query only the specified partition
- `openrag-all`: Query across all available partitions
- empty or model name: Use the LLM directly

**Request Format:**
Accepts OpenAI-compatible completion requests with:
- `prompt`: Text prompt for completion
- `model`: Model/partition to use
- Standard OpenAI parameters (temperature, max_tokens, etc.)

**Response:**
Returns OpenAI-compatible response with additional `extra` field containing:
- `sources`: Array of source documents with metadata and URLs

**Note:** Streaming is not supported for this endpoint.
""",
)
async def openai_completion(
    request2: Request,
    request: OpenAICompletionRequest,
    user=Depends(current_user),
    user_partitions=Depends(current_user_or_admin_partitions_list),
    service=Depends(get_query_service),
    partition_service=Depends(get_partition_service),
    config=Depends(get_config),
):
    model_name = request.model or config.llm.model
    log = logger.bind(model=model_name, endpoint="/completions")

    if not request.prompt:
        log.warning("Prompt is missing.")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The prompt is required")

    if request.stream:
        log.warning("Streaming not supported for this endpoint.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Streaming is not supported for this endpoint",
        )

    if is_direct_llm_model(request, config):
        partitions = None
    else:
        partitions = await get_partition_name(
            model_name,
            user_partitions,
            partition_service=partition_service,
            is_admin=user["is_admin"],
        )

    # Bound the caller's input size in every mode (RAG context is capped
    # separately), against the resolved partition's chat_llm preset budget
    # when one applies.
    check_tokens_limit(request, log, config, partitions=partitions)

    resp = await service.complete(
        partitions=partitions,
        payload=request.model_dump(exclude_none=True),
        prepare_sources=lambda docs, _web: __prepare_sources(request2, docs),
    )
    log.debug("Returning completion response.")
    return JSONResponse(content=resp)
