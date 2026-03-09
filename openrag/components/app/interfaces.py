from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any


class OpenRAGApiInterface(ABC):
    """Shared application interface for API and MCP operations."""

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    @abstractmethod
    async def search_documents(
        self,
        query: str,
        partitions: list[str] | None = None,
        top_k: int | None = None,
        file_id: str | None = None,
        allowed_partitions: list[str] | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def search_partition(
        self,
        query: str,
        partition: str,
        top_k: int | None = None,
        allowed_partitions: list[str] | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def search_file(
        self,
        query: str,
        partition: str,
        file_id: str,
        top_k: int | None = None,
        allowed_partitions: list[str] | None = None,
    ) -> dict[str, Any]: ...

    # ------------------------------------------------------------------
    # Indexation / catalog  (shared with MCP)
    # ------------------------------------------------------------------

    @abstractmethod
    async def list_partitions(
        self,
        allowed_partitions: list[str] | None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def list_files(
        self,
        partition: str,
        allowed_partitions: list[str] | None,
        limit: int | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def get_file_info(
        self,
        partition: str,
        file_id: str,
        allowed_partitions: list[str] | None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def get_file_chunks(
        self,
        partition: str,
        file_id: str,
        allowed_partitions: list[str] | None,
        offset: int = 0,
        limit: int = 3,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def fuzzy_search_files(
        self,
        query: str,
        allowed_partitions: list[str] | None,
        partition: str | None = None,
        cutoff: float = 0.4,
        limit: int = 20,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def get_task_status(
        self,
        task_id: str,
        user_id: int | None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def list_my_tasks(
        self,
        user_id: int | None,
        task_status: str | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def get_task_logs(
        self,
        task_id: str,
        user_id: int | None,
        log_file: str | Path,
        max_lines: int = 100,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def get_chunk_by_id(
        self,
        chunk_id: str,
        allowed_partitions: list[str] | None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def delete_file(
        self,
        partition: str,
        file_id: str,
        allowed_partitions: list[str] | None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def update_file_metadata(
        self,
        partition: str,
        file_id: str,
        metadata: dict[str, Any],
        allowed_partitions: list[str] | None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def copy_file(
        self,
        source_partition: str,
        source_file_id: str,
        dest_partition: str,
        dest_file_id: str,
        allowed_partitions: list[str] | None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def index_url(
        self,
        url: str,
        partition: str,
        file_id: str,
        allowed_partitions: list[str] | None,
        extra_metadata: dict[str, Any] | None = None,
        task_state_manager_ref: Any = None,
    ) -> dict[str, Any]: ...

    # ------------------------------------------------------------------
    # API-only: file ingestion
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_supported_types(
        self,
        accepted_formats: Any,
        dict_mimetypes: Any,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def add_file(
        self,
        file_path: Path,
        metadata: dict[str, Any],
        partition: str,
        user: dict[str, Any],
        indexer: Any,
        task_state_manager: Any,
    ) -> dict[str, Any]:
        """Queue an already-saved file for indexation.

        Returns ``{ task_id }`` so the router can build the status URL.
        """
        ...

    @abstractmethod
    async def replace_file(
        self,
        file_id: str,
        file_path: Path,
        metadata: dict[str, Any],
        partition: str,
        user: dict[str, Any],
        indexer: Any,
        task_state_manager: Any,
    ) -> dict[str, Any]:
        """Delete the old file then queue the replacement for indexation.

        Returns ``{ task_id }`` so the router can build the status URL.
        """
        ...

    @abstractmethod
    async def get_task_error(
        self,
        task_state_manager: Any,
        task_id: str,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def cancel_task(
        self,
        task_state_manager: Any,
        task_id: str,
    ) -> dict[str, Any]: ...

    # ------------------------------------------------------------------
    # Extract
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_extract(
        self,
        vectordb: Any,
        extract_id: str,
    ) -> dict[str, Any]: ...

    # ------------------------------------------------------------------
    # Partition management
    # ------------------------------------------------------------------

    @abstractmethod
    async def list_partition_chunks(
        self,
        vectordb: Any,
        partition: str,
        include_embedding: bool = True,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def create_partition(
        self,
        vectordb: Any,
        partition: str,
        user_id: int,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def delete_partition(
        self,
        vectordb: Any,
        partition: str,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def list_partition_users(
        self,
        vectordb: Any,
        partition: str,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def add_partition_user(
        self,
        vectordb: Any,
        partition: str,
        user_id: int,
        role: str,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def remove_partition_user(
        self,
        vectordb: Any,
        partition: str,
        user_id: int,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def update_partition_user_role(
        self,
        vectordb: Any,
        partition: str,
        user_id: int,
        role: str,
    ) -> dict[str, Any]: ...

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_queue_info(
        self,
        task_state_manager: Any,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def list_tasks(
        self,
        user_id: int | None,
        task_status: str | None = None,
    ) -> dict[str, Any]: ...

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    @abstractmethod
    async def list_users(
        self,
        vectordb: Any,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def get_current_user(
        self,
        user: dict[str, Any],
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def create_user(
        self,
        vectordb: Any,
        display_name: str | None = None,
        external_user_id: str | None = None,
        is_admin: bool = False,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def get_user(
        self,
        vectordb: Any,
        user_id: int,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def delete_user_account(
        self,
        vectordb: Any,
        user_id: int,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def regenerate_user_token(
        self,
        vectordb: Any,
        user_id: int,
    ) -> dict[str, Any]: ...

    # ------------------------------------------------------------------
    # Actors
    # ------------------------------------------------------------------

    @abstractmethod
    async def list_ray_actors(
        self,
        actors: list[dict[str, Any]],
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def restart_actor(
        self,
        actor_name: str,
        actor_creation_map: dict[str, Any],
    ) -> dict[str, Any]: ...

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @abstractmethod
    async def list_tools(
        self,
        tools: list[Any],
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def execute_tool(
        self,
        tool_name: str,
        file_path: Path,
        metadata: dict[str, Any],
    ) -> dict[str, Any]: ...

    # ------------------------------------------------------------------
    # OpenAI-compatible endpoints
    # ------------------------------------------------------------------

    @abstractmethod
    async def list_models(
        self,
        vectordb: Any,
        user_partitions: list[dict[str, Any]],
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def openai_chat_completion(
        self,
        payload: dict[str, Any],
        partitions: list[str] | None,
    ) -> tuple[AsyncIterator, list[Any]]: ...

    @abstractmethod
    async def openai_completion(
        self,
        payload: dict[str, Any],
        partitions: list[str] | None,
    ) -> tuple[AsyncIterator, list[Any]]: ...
