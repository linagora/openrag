import asyncio
import json
from pathlib import Path
from urllib.parse import quote, urlparse

import consts
from components.indexer.utils.text_sanitizer import sanitize_text
from components.pipeline import RagPipeline
from components.utils import (
    extract_and_strip_sources_block,
    filter_sources_by_citations,
    get_num_tokens,
    stream_with_source_filtering,
)
from config import load_config
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.documents.base import Document
from models.openai import OpenAIChatCompletionRequest, OpenAICompletionRequest
from utils.dependencies import get_vectordb
from utils.exceptions.base import OpenRAGError
from utils.logger import get_logger

from .utils import (
    check_llm_model_availability,
    current_user,
    current_user_or_admin_partitions,
    current_user_or_admin_partitions_list,
    get_openai_models,
    get_partition_name,
    truncate,
)

logger = get_logger()
config = load_config()
router = APIRouter()

ragpipe = RagPipeline()

# Cached max model token limit, populated at startup
_max_model_tokens: int | None = None


@router.on_event("startup")
async def _cache_max_model_tokens():
    global _max_model_tokens
    _max_model_tokens = await _fetch_max_model_tokens()


def _make_sse_error(message: str, code: str) -> str:
    """Format an error as an SSE data chunk for streaming responses."""
    chunk = {
        "error": {
            "message": message,
            "type": "error",
            "param": None,
            "code": code,
        }
    }
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
    vectordb=Depends(get_vectordb),
    user_partitions=Depends(current_user_or_admin_partitions),
):
    if [p["partition"] for p in user_partitions] == ["all"]:
        user_partitions = await vectordb.list_partitions.remote()
    logger.debug("Listing models", partition_count=len(user_partitions))

    models = []
    for partition in user_partitions:
        model_id = f"{consts.PARTITION_PREFIX}{partition['partition']}"
        models.append(
            {
                "id": model_id,
                "object": "model",
                "created": partition["created_at"],
                "owned_by": "OpenRAG",
            }
        )

    models.append(
        {
            "id": f"{consts.PARTITION_PREFIX}all",
            "object": "model",
            "created": 0,
            "owned_by": "OpenRAG",
        }
    )
    return JSONResponse(content={"object": "list", "data": models})


def __prepare_sources(request: Request, docs: list[Document], web_results: list | None = None):
    links = []
    for doc in docs:
        doc_metadata = dict(doc.metadata)
        filename = Path(doc_metadata.get("source")).name
        file_url = str(request.url_for("static", path=filename))
        encoded_url = quote(file_url, safe=":/")
        links.append(
            {
                "source_type": "document",
                "file_url": encoded_url,
                "chunk_url": str(request.url_for("get_extract", extract_id=doc_metadata["_id"])),
                **doc_metadata,
            }
        )
    for result in web_results or []:
        display_url = sanitize_text(result.display_url or "")
        if display_url and urlparse(display_url).scheme not in ("http", "https"):
            display_url = ""
        links.append(
            {
                "source_type": "web",
                "url": result.url,
                "title": sanitize_text(result.title),
                "snippet": sanitize_text(result.snippet),
                "display_url": display_url,
            }
        )
    return links


def is_direct_llm_model(
    request: OpenAIChatCompletionRequest | OpenAICompletionRequest,
) -> bool:
    """Check if request should use direct LLM (no RAG partition).

    Returns True if model is None, empty, or matches the configured default model.
    """
    return request.model is None or request.model == "" or request.model == config.llm.get("model")


async def _fetch_max_model_tokens() -> int:
    """Fetch the maximum model token limit from vLLM's OpenAI server.

    Queries `/v1/models` and looks for `max_model_len` for the configured LLM model.
    Falls back to `config.llm_context.max_llm_context_size` (default 8192) if unavailable.
    """
    default_limit = int(config.llm_context.get("max_llm_context_size", 8192))
    model_id = config.llm.get("model")
    try:
        openai_models = await get_openai_models(base_url=config.llm["base_url"], api_key=config.llm["api_key"])
        model = next((m for m in openai_models if m.id == model_id), None)
        if model is None:
            logger.warning(f"No model found for {model_id}. Using default context size.")
            return default_limit

        model_data = model.model_dump() if hasattr(model, "model_dump") else model.dict()
        max_len = model_data.get("max_model_len") or model_data.get("model_extra", {}).get("max_model_len")

        if max_len is None:
            logger.warning(f"max_model_len not found for {model_id}. Using default context size.")
            return default_limit

        logger.info("Fetched max_model_len from vLLM at startup", model=model_id, max_model_len=int(max_len))
        return int(max_len)

    except Exception as e:
        logger.warning("Failed to query /v1/models for max_model_len; using default", error=str(e))
        return default_limit


def get_max_model_tokens() -> int:
    """Return the cached max model token limit (populated at startup)."""
    if _max_model_tokens is not None:
        return _max_model_tokens
    return int(config.llm_context.get("max_llm_context_size", 8192))


def validate_tokens_limit(
    request: OpenAIChatCompletionRequest | OpenAICompletionRequest,
    max_tokens_allowed: int,
) -> tuple[bool, str]:
    """Validate if the request respects the maximum token limit.

    Args:
        request: The OpenAI request object
        max_tokens_allowed: Maximum allowed tokens for the request.

    Returns:
        Tuple of (is_valid, error_message)
    """
    try:
        _length_function = get_num_tokens()

        if isinstance(request, OpenAIChatCompletionRequest):
            message_tokens = sum(_length_function(m.content or "") + 4 for m in request.messages)
            default_output_tokens = int(config.llm_context.get("max_output_tokens", 1024))
            requested_tokens = request.max_tokens or default_output_tokens
            total_tokens_needed = message_tokens + requested_tokens

            logger.debug(
                "Token validation for chat completion",
                message_tokens=message_tokens,
                requested_tokens=requested_tokens,
                total_tokens=total_tokens_needed,
                max_allowed=max_tokens_allowed,
            )

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
            default_output_tokens = int(config.llm_context.get("max_output_tokens", 1024))
            requested_tokens = request.max_tokens or default_output_tokens
            total_tokens_needed = prompt_tokens + requested_tokens

            logger.debug(
                "Token validation for completion",
                prompt_tokens=prompt_tokens,
                requested_tokens=requested_tokens,
                total_tokens=total_tokens_needed,
                max_allowed=max_tokens_allowed,
            )

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
):
    """Validate token limit and raise HTTPException(413) if exceeded."""
    max_tokens_allowed = get_max_model_tokens()
    is_valid, error_message = validate_tokens_limit(request, max_tokens_allowed=max_tokens_allowed)
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

**RAG Process:**
1. Extracts query from conversation
2. Retrieves relevant documents from specified partition(s)
3. Enriches prompt with document context
4. Generates completion using LLM

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
    _: None = Depends(check_llm_model_availability),
):
    model_name = request.model or config.llm.get("model")
    log = logger.bind(model=model_name, endpoint="/chat/completions")

    if not request.messages or request.messages[-1].role != "user" or not request.messages[-1].content:
        log.warning("Invalid request: missing or malformed user message.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The last message must be a non-empty user message",
        )

    check_tokens_limit(request, log)

    log.debug(
        "Received chat completion request with messages: {}",
        truncate(str(request.messages)),
    )

    if is_direct_llm_model(request):
        partitions = None
    else:
        partitions = await get_partition_name(model_name, user_partitions, is_admin=user["is_admin"])
        log.debug(f"Using partitions: {partitions}")

    llm_output, docs, web_results = await ragpipe.chat_completion(partition=partitions, payload=request.model_dump())
    log.debug("RAG chat completion pipeline executed.")

    sources = __prepare_sources(request2, docs, web_results=web_results)

    if request.stream:

        async def stream_response():
            try:
                async for sse_line in stream_with_source_filtering(llm_output, sources, model_name):
                    yield sse_line
            except asyncio.CancelledError:
                log.info("Client disconnected during streaming")
                return
            except OpenRAGError as e:
                log.warning("OpenRAG error during streaming", code=e.code, error=e.message)
                yield _make_sse_error(e.message, e.code)
            except Exception as e:
                log.warning("Error during streaming", error=str(e))
                yield _make_sse_error("An unexpected error occurred during streaming", "UNEXPECTED_ERROR")

        return StreamingResponse(stream_response(), media_type="text/event-stream")
    else:
        chunk = await llm_output.__anext__()
        chunk["model"] = model_name

        content = chunk.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        clean_content, citations = extract_and_strip_sources_block(content)
        chunk["choices"][0]["message"]["content"] = clean_content

        filtered = filter_sources_by_citations(sources, citations)
        chunk["extra"] = json.dumps({"sources": filtered})
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

**RAG Process:**
1. Retrieves relevant documents from specified partition(s)
2. Enriches prompt with document context
3. Generates completion using LLM

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
    _: None = Depends(check_llm_model_availability),
):
    model_name = request.model or config.llm.get("model")
    log = logger.bind(model=model_name, endpoint="/completions")

    if not request.prompt:
        log.warning("Prompt is missing.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The prompt is required",
        )

    if request.stream:
        log.warning("Streaming not supported for this endpoint.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Streaming is not supported for this endpoint",
        )

    check_tokens_limit(request, log)

    if is_direct_llm_model(request):
        partitions = None
    else:
        partitions = await get_partition_name(model_name, user_partitions, is_admin=user["is_admin"])

    llm_output, docs = await ragpipe.completions(partition=partitions, payload=request.model_dump())
    log.debug("RAG completion pipeline executed.")

    sources = __prepare_sources(request2, docs)

    complete_response = await llm_output.__anext__()

    text = complete_response.get("choices", [{}])[0].get("text", "") or ""
    clean_text, citations = extract_and_strip_sources_block(text)
    complete_response["choices"][0]["text"] = clean_text

    filtered = filter_sources_by_citations(sources, citations)
    complete_response["extra"] = json.dumps({"sources": filtered})
    log.debug("Returning completion response.")
    return JSONResponse(content=complete_response)
