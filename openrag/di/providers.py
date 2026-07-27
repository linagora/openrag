"""FastAPI dependency providers.

This module is the API layer's bridge to the composition root. During
the Phase 10 -> 11 transition it supports both access patterns:

* request-scoped lookup from ``request.app.state.container``;
* process-level override via :func:`set_container` for tests and the
  Phase 11 lifespan.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request, status

if TYPE_CHECKING:
    from di.container import ServiceContainer
    from services.orchestrators.auth_service import AuthService
    from services.orchestrators.conversion_service import ConversionService
    from services.orchestrators.indexing_service import IndexingService
    from services.orchestrators.job_service import JobService
    from services.orchestrators.mcp_service import MCPService
    from services.orchestrators.partition_service import PartitionService
    from services.orchestrators.query_service import QueryService
    from services.orchestrators.retrieval_service import RetrievalService
    from services.orchestrators.user_service import UserService
    from services.orchestrators.workspace_service import WorkspaceService

_container: ServiceContainer | None = None
_container_lock = threading.Lock()


def set_container(container: ServiceContainer | None) -> None:
    """Override the process-level container.

    Tests use this to install a fake container. The app lifespan can use
    it after constructing the real container. Passing ``None`` clears the
    override.
    """
    global _container
    with _container_lock:
        _container = container


def get_container(request: Request = None) -> ServiceContainer:
    """Resolve the active service container from request state or process state."""
    if request is not None and hasattr(request.app.state, "container"):
        container = request.app.state.container
        if container is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Service container is not available.",
            )
        return container

    global _container
    container = _container
    if container is None:
        with _container_lock:
            if _container is None:
                from di.container import ServiceContainer

                _container = ServiceContainer()
            container = _container
    return container


def _require_initialized(request: Request = None) -> ServiceContainer:
    """Return the active container only after its initialization guard passes."""
    container = get_container(request)
    if not getattr(container, "is_initialized", True):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service container has not been initialized.",
        )
    return container


def get_auth_service(request: Request = None) -> AuthService:
    """Resolve the authentication orchestrator from the active container."""
    return _require_initialized(request).auth_service


def get_user_service(request: Request = None) -> UserService:
    """Resolve the user orchestrator from the active container."""
    return _require_initialized(request).user_service


def get_partition_service(request: Request = None) -> PartitionService:
    """Resolve the partition orchestrator from the active container."""
    return _require_initialized(request).partition_service


def get_workspace_service(request: Request = None) -> WorkspaceService:
    """Resolve the workspace orchestrator from the active container."""
    return _require_initialized(request).workspace_service


def get_retrieval_service(request: Request = None) -> RetrievalService:
    """Resolve the retrieval orchestrator from the active container."""
    return _require_initialized(request).retrieval_service


def get_query_service(request: Request = None) -> QueryService:
    """Resolve the query orchestrator from the active container."""
    return _require_initialized(request).query_service


def get_indexing_service(request: Request = None) -> IndexingService:
    """Resolve the indexing orchestrator from the active container."""
    return _require_initialized(request).indexing_service


def get_job_service(request: Request = None) -> JobService:
    """Resolve the job orchestrator from the active container."""
    return _require_initialized(request).job_service


def get_conversion_service(request: Request = None) -> ConversionService:
    """Resolve the conversion orchestrator from the active container."""
    return _require_initialized(request).conversion_service


def get_mcp_service(request: Request = None) -> MCPService:
    """Resolve the MCP orchestrator from the active container."""
    return _require_initialized(request).mcp_service


def _get_optional_service(container: ServiceContainer, attribute_name: str) -> Any:
    """Resolve a service that can be absent until its phase lands."""
    service = getattr(container, attribute_name, None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{attribute_name} is not available.",
        )
    return service


def get_model_endpoint_service(request: Request = None) -> Any:
    """Resolve the Phase 14 model endpoint orchestrator from the active container."""
    return _get_optional_service(_require_initialized(request), "model_endpoint_service")


def get_preset_service(request: Request = None) -> Any:
    """Resolve the Phase 14 preset orchestrator from the active container."""
    return _get_optional_service(_require_initialized(request), "preset_service")


def get_prompt_service(request: Request = None) -> Any:
    """Resolve the prompt-management orchestrator from the active container."""
    return _get_optional_service(_require_initialized(request), "prompt_service")


def get_config(request: Request = None):
    """Resolve application configuration from the active container."""
    return _require_initialized(request).config


__all__ = [
    "get_auth_service",
    "get_config",
    "get_container",
    "get_conversion_service",
    "get_indexing_service",
    "get_job_service",
    "get_mcp_service",
    "get_model_endpoint_service",
    "get_partition_service",
    "get_preset_service",
    "get_prompt_service",
    "get_query_service",
    "get_retrieval_service",
    "get_user_service",
    "get_workspace_service",
    "set_container",
]
