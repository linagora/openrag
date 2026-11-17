"""OpenAI-compatible RAG endpoints — thin HTTP layer over ChatService.

The RAG flow (query generation, retrieval, web search, map-reduce,
context/prompt assembly, streaming, and the ``[Sources: N]`` citation
filtering) lives in ``services.orchestrators.chat_service.ChatService`` and
``AnswerService``. This module keeps HTTP transport only: model→partition
resolution, the OpenAI ``/models`` listing, request-bound source-link building
(``__prepare_sources`` uses ``request.url_for`` so it stays here and is
handed to the service as a callable), and ``StreamingResponse`` /
``JSONResponse`` wrapping with the SSE error envelope. The token budget it
checks before calling the service is in ``api.dependencies.token_budget``, the
answering endpoint's window in ``api.dependencies.model_limits``.
"""

import asyncio
import json
from typing import TYPE_CHECKING

from api.dependencies.auth import (
    current_user,
    current_user_or_admin_partitions,
    current_user_or_admin_partitions_list,
)
from api.dependencies.llm import get_partition_name, truncate
from api.dependencies.retrieval_diagnostics import get_retrieval_diagnostics_guard
from api.dependencies.token_budget import apply_default_max_tokens, check_tokens_limit, max_prompt_tokens_for
from api.routers.user.source_links import build_document_source_link
from api.schemas.user.chat import OpenAIChatCompletionRequest, OpenAICompletionRequest
from core.chat.routing import is_direct_llm_model as _names_the_llm
from core.chat.routing import resolve_route
from core.config import load_config
from core.models.chat import ChatOptions
from core.utils import consts
from core.utils.exceptions import OpenRAGError
from core.utils.logging import get_logger
from core.utils.text import sanitize_text
from core.utils.web_url import normalize_web_url
from di.providers import get_chat_service, get_config, get_partition_service
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

logger = get_logger()
router = APIRouter()

if TYPE_CHECKING:
    from core.config.root import Settings


def _runtime_config(settings: "Settings | None" = None) -> "Settings":
    return settings if settings is not None else load_config()


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
        url = normalize_web_url(result.url)
        if url is None:
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
    return _names_the_llm(request.model, _runtime_config(settings).llm.model)


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
- `messages`: Array of chat messages (the last one must be a non-empty user message, or a tool result the client sends back)
- `model`: Model/partition to use
- `stream`: Optional streaming response (true/false)
- Standard OpenAI parameters (temperature, max_tokens, etc.)

**Response:**
Returns OpenAI-compatible response with an additional `extra` field -- a JSON
object (not a JSON-encoded string) -- containing:
- `sources`: Legacy field, kept for backward compatibility. Cited sources, or
  every presented source as a fallback when the model didn't report citations
- `presented_sources`: Array of every source actually shown to the model
  (after context-budget truncation), regardless of what it cited
- `cited_sources`: Array of only the sources the model explicitly cited; empty
  whenever no citations tag was found (never falls back like `sources` does)
- `citations_reported`: `true` only if the model emitted a citations tag
  (even an empty one); `false` means `sources` fell back to keeping everything
- `all_retrieved_sources`: Array of every source retrieval returned, unfiltered
  by citation or context-budget truncation — only included when the request's
  `metadata.include_all_retrieved_sources` is `true` (off by default; this is
  debug/evaluation telemetry and can be large)

Each entry in those arrays is shaped `{"source_type": "document", "chunk": {...},
"rerank_score": 0.64, "chunk_url": "...", "file_url": "..."}` — `chunk` holds the
chunk's own metadata, its siblings are what the server computed about it.
`rerank_score` is absent when no reranker ran, and `file_url` when the chunk has
no source file. Web entries (`source_type: "web"`) are flat: `url`, `title`,
`snippet`, no `chunk`.

**Streaming:**
Set `stream: true` for Server-Sent Events (SSE) streaming responses.
""",
)
async def openai_chat_completion(
    request2: Request,
    request: OpenAIChatCompletionRequest = Body(...),
    user=Depends(current_user),
    user_partitions=Depends(current_user_or_admin_partitions_list),
    service=Depends(get_chat_service),
    partition_service=Depends(get_partition_service),
    config=Depends(get_config),
    diagnostics_guard=Depends(get_retrieval_diagnostics_guard),
):
    model_name = request.model or config.llm.model
    log = logger.bind(model=model_name, endpoint="/chat/completions")

    # The conversation ends with a message of the user, or with the result
    # of a tool call of the assistant that the client ran. A tool may return
    # nothing, but the result names the call it answers.
    last = request.messages[-1] if request.messages else None
    if last is None or not (
        (last.role == "user" and last.content)
        or (last.role == "tool" and last.content is not None and (last.model_extra or {}).get("tool_call_id"))
    ):
        log.warning("Invalid request: missing or malformed last message.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The last message must be a non-empty user message or a tool result with its tool_call_id",
        )

    request.apply_default_rag_mode(config.rag.default_mode)
    options = ChatOptions.from_metadata(request.metadata)
    if options.include_retrieval_trace or options.compare_original_query or options.bypass_query_contextualization:
        await diagnostics_guard.authorize(user)

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
        await service.refresh_partition_configs()
        log.debug(f"Using partitions: {partitions}")

    # The search runs on the latest message of the user: a tool result alone
    # gives it nothing to look for.
    latest_user = next((m for m in reversed(request.messages) if m.role == "user"), None)
    if (
        resolve_route(partitions, options).searches
        and last.role == "tool"
        and not (latest_user and latest_user.content)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A search needs the latest user message to be non-empty",
        )

    # Bound the caller's input size in every mode, against the resolved
    # partition's chat_llm endpoint budget when one applies (else the default
    # LLM endpoint). RAG-injected context is added server-side and separately
    # capped (top_n, and max_prompt_tokens below), but the user's own messages
    # must be limited regardless of direct-LLM vs RAG.
    # Resolve the output budget now that the answering endpoint is known, so the
    # preflight below and the payload sent downstream both use the resolved
    # endpoint's budget rather than the default endpoint's.
    apply_default_max_tokens(request, config, partitions)
    check_tokens_limit(request, log, config, partitions=partitions)
    max_prompt_tokens = max_prompt_tokens_for(request, config, partitions)

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
                    request_id=getattr(request2.state, "request_id", None),
                    max_prompt_tokens=max_prompt_tokens,
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
        request_id=getattr(request2.state, "request_id", None),
        max_prompt_tokens=max_prompt_tokens,
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
Returns OpenAI-compatible response with an additional `extra` field -- a JSON
object (not a JSON-encoded string) -- containing:
- `sources`: Legacy field, kept for backward compatibility. Cited sources, or
  every presented source as a fallback when the model didn't report citations
- `presented_sources`: Array of every source actually shown to the model
  (after context-budget truncation), regardless of what it cited
- `cited_sources`: Array of only the sources the model explicitly cited; empty
  whenever no citations tag was found (never falls back like `sources` does)
- `citations_reported`: `true` only if the model emitted a citations tag
  (even an empty one); `false` means `sources` fell back to keeping everything
- `all_retrieved_sources`: Array of every source retrieval returned, unfiltered
  by citation or context-budget truncation — only included when the request's
  `metadata.include_all_retrieved_sources` is `true` (off by default; this is
  debug/evaluation telemetry and can be large)

Each entry in those arrays is shaped `{"source_type": "document", "chunk": {...},
"rerank_score": 0.64, "chunk_url": "...", "file_url": "..."}` — `chunk` holds the
chunk's own metadata, its siblings are what the server computed about it.
`rerank_score` is absent when no reranker ran, and `file_url` when the chunk has
no source file. Web entries (`source_type: "web"`) are flat: `url`, `title`,
`snippet`, no `chunk`.

**Note:** Streaming is not supported for this endpoint.
""",
)
async def openai_completion(
    request2: Request,
    request: OpenAICompletionRequest,
    user=Depends(current_user),
    user_partitions=Depends(current_user_or_admin_partitions_list),
    service=Depends(get_chat_service),
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

    options = ChatOptions.from_metadata(request.metadata)
    if options.include_retrieval_trace or options.compare_original_query or options.bypass_query_contextualization:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Retrieval diagnostics are supported only by chat completions",
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
        await service.refresh_partition_configs()

    # Bound the caller's input size in every mode (RAG context is capped
    # separately), against the resolved partition's chat_llm endpoint budget
    # when one applies.
    # Resolve the output budget now that the answering endpoint is known, so the
    # preflight below and the payload sent downstream both use the resolved
    # endpoint's budget rather than the default endpoint's.
    apply_default_max_tokens(request, config, partitions)
    check_tokens_limit(request, log, config, partitions=partitions)

    resp = await service.complete(
        partitions=partitions,
        payload=request.model_dump(exclude_none=True),
        prepare_sources=lambda docs, _web: __prepare_sources(request2, docs),
        max_prompt_tokens=max_prompt_tokens_for(request, config, partitions),
    )
    log.debug("Returning completion response.")
    return JSONResponse(content=resp)
