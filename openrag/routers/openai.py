import asyncio
import json
from pathlib import Path
from urllib.parse import quote

import consts
from components.pipeline import RagPipeline
from config import load_config
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.documents.base import Document
from models.openai import (
    OpenAIChatCompletionRequest,
    OpenAICompletionRequest,
)
from ray.exceptions import RayTaskError, TaskCancelledError
from utils.dependencies import get_vectordb
from utils.exceptions.base import OpenRAGError
from utils.exceptions.vectordb import VDBPartitionNotFound
from utils.logger import get_logger

from .utils import (
    check_llm_model_availability,
    current_user,
    current_user_or_admin_partitions,
    current_user_or_admin_partitions_list,
    get_partition_name,
    truncate,
)

logger = get_logger()
config = load_config()
router = APIRouter()

ragpipe = RagPipeline(config=config)


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
    _: None = Depends(check_llm_model_availability),
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


def __prepare_sources(request: Request, docs: list[Document]):
    links = []
    for doc in docs:
        doc_metadata = dict(doc.metadata)
        filename = Path(doc_metadata.get("source")).name
        file_url = str(request.url_for("static", path=filename))
        encoded_url = quote(file_url, safe=":/")
        links.append(
            {
                "file_url": encoded_url,
                "chunk_url": str(request.url_for("get_extract", extract_id=doc_metadata["_id"])),
                **doc_metadata,
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
- `metadata.domains`: Optional list of domain IDs to filter retrieved documents (OR logic)
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
    _: None = Depends(check_llm_model_availability),
    user=Depends(current_user),
    user_partitions=Depends(current_user_or_admin_partitions_list),
):
    model_name = request.model or config.llm.get("model")
    log = logger.bind(model=model_name, endpoint="/chat/completions")

    if not request.messages or request.messages[-1].role != "user" or not request.messages[-1].content:
        log.warning("Invalid request: missing or malformed user message.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The last message must be a non-empty user message",
        )

    log.debug(
        "Received chat completion request with messages: {}",
        truncate(str(request.messages)),
    )

    try:
        if is_direct_llm_model(request):
            partitions = None
        else:
            partitions = await get_partition_name(model_name, user_partitions, is_admin=user["is_admin"])
            log.debug(f"Using partitions: {partitions}")
    except HTTPException:
        raise
    except VDBPartitionNotFound as e:
        log.warning("Partition not found", partition=model_name, error=e.message)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=e.message,
        )
    except Exception as e:
        log.warning("Invalid model or partition", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid model or partition name",
        )

    try:
        llm_output, docs = await ragpipe.chat_completion(partition=partitions, payload=request.model_dump())
        log.debug("RAG chat completion pipeline executed.")
    except HTTPException:
        raise
    except OpenRAGError as e:
        log.warning("Chat completion failed with OpenRAG error", code=e.code, error=e.message)
        raise HTTPException(
            status_code=e.status_code,
            detail=e.message,
        )
    except (RayTaskError, TaskCancelledError) as e:
        log.exception("Chat completion failed due to Ray task error", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Chat completion processing failed",
        )
    except Exception as e:
        log.exception("Chat completion failed with unexpected error", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred during chat completion",
        )

    metadata = __prepare_sources(request2, docs)
    metadata_json = json.dumps({"sources": metadata})

    if request.stream:

        async def stream_response():
            try:
                async for line in llm_output:
                    if line.startswith("data:"):
                        if "[DONE]" in line:
                            yield f"{line}\n\n"
                        else:
                            try:
                                data_str = line[len("data: ") :]
                                data = json.loads(data_str)
                                data["model"] = model_name
                                data["extra"] = metadata_json
                                yield f"data: {json.dumps(data)}\n\n"
                            except json.JSONDecodeError as e:
                                log.error("Failed to decode streamed chunk.", error=str(e))
                                raise
            except asyncio.CancelledError:
                log.info("Client disconnected during streaming")
                return
            except (RayTaskError, TaskCancelledError) as e:
                log.exception("Ray task error during streaming", error=str(e))
                error_chunk = {
                    "error": {
                        "message": "Streaming processing failed",
                        "type": "error",
                        "param": None,
                        "code": "RAY_TASK_ERROR",
                    }
                }
                yield f"data: {json.dumps(error_chunk)}\n\n"
                yield "data: [DONE]\n\n"
            except OpenRAGError as e:
                log.warning("OpenRAG error during streaming", code=e.code, error=e.message)
                error_chunk = {
                    "error": {
                        "message": e.message,
                        "type": "error",
                        "param": None,
                        "code": e.code,
                    }
                }
                yield f"data: {json.dumps(error_chunk)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                log.exception("Unexpected error during streaming", error=str(e))
                error_chunk = {
                    "error": {
                        "message": "An unexpected error occurred during streaming",
                        "type": "error",
                        "param": None,
                        "code": "ERROR_ANSWER_GENERATION",
                    }
                }
                yield f"data: {json.dumps(error_chunk)}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(stream_response(), media_type="text/event-stream")
    else:
        try:
            chunk = await llm_output.__anext__()
            chunk["model"] = model_name
            chunk["extra"] = metadata_json
            log.debug("Returning non-streaming completion chunk.")
            return JSONResponse(content=chunk)
        except HTTPException:
            raise
        except OpenRAGError as e:
            log.warning("Error generating non-streaming answer", code=e.code, error=e.message)
            raise HTTPException(
                status_code=e.status_code,
                detail=e.message,
            )
        except Exception as e:
            log.exception("Unexpected error generating non-streaming answer", error=str(e))
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="An unexpected error occurred while generating answer",
            )


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
- `metadata.domains`: Optional list of domain IDs to filter retrieved documents (OR logic)
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
    _: None = Depends(check_llm_model_availability),
    user=Depends(current_user),
    user_partitions=Depends(current_user_or_admin_partitions_list),
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

    try:
        if is_direct_llm_model(request):
            partitions = None
        else:
            partitions = await get_partition_name(model_name, user_partitions, is_admin=user["is_admin"])
    except HTTPException:
        raise
    except VDBPartitionNotFound as e:
        log.warning("Partition not found", partition=model_name, error=e.message)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=e.message,
        )
    except Exception as e:
        log.warning("Invalid model or partition", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid model or partition name",
        )

    try:
        llm_output, docs = await ragpipe.completions(partition=partitions, payload=request.model_dump())
        log.debug("RAG completion pipeline executed.")
    except HTTPException:
        raise
    except OpenRAGError as e:
        log.warning("Completion failed with OpenRAG error", code=e.code, error=e.message)
        raise HTTPException(
            status_code=e.status_code,
            detail=e.message,
        )
    except (RayTaskError, TaskCancelledError) as e:
        log.exception("Completion failed due to Ray task error", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Completion processing failed",
        )
    except Exception as e:
        log.exception("Completion failed with unexpected error", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred during completion",
        )

    metadata = __prepare_sources(request2, docs)
    metadata_json = json.dumps({"sources": metadata})

    try:
        complete_response = await llm_output.__anext__()
        complete_response["extra"] = metadata_json
        log.debug("Returning completion response.")
        return JSONResponse(content=complete_response)
    except HTTPException:
        raise
    except OpenRAGError as e:
        log.warning("Error getting completion response", code=e.code, error=e.message)
        raise HTTPException(
            status_code=e.status_code,
            detail=e.message,
        )
    except Exception as e:
        log.exception("Unexpected error getting completion response", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while getting completion response",
        )
