from __future__ import annotations

from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import ray
from components.mcp.indexation_service import IndexationService
from components.mcp.service import SearchToolService

from .interfaces import OpenRAGApiInterface


class OpenRAGApplicationService(OpenRAGApiInterface):
    """Shared app-layer service used by both API and MCP."""

    def __init__(
        self,
        *,
        default_top_k: int = 5,
        max_top_k: int = 50,
        similarity_threshold: float = 0.8,
    ):
        # Lazy import to avoid triggering Ray initialisation at import time.
        from components.mcp.adapters import RayIndexerSearchGateway  # noqa: PLC0415

        self.search_service = SearchToolService(
            gateway=RayIndexerSearchGateway(),
            default_top_k=default_top_k,
            max_top_k=max_top_k,
            similarity_threshold=similarity_threshold,
        )
        self.indexation_service = IndexationService()
        self._ragpipe = None

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search_documents(
        self,
        query: str,
        partitions: list[str] | None = None,
        top_k: int | None = None,
        file_id: str | None = None,
        allowed_partitions: list[str] | None = None,
    ) -> dict[str, Any]:
        return await self.search_service.search_documents(
            query=query,
            partitions=partitions,
            top_k=top_k,
            file_id=file_id,
            allowed_partitions=allowed_partitions,
        )

    async def search_partition(
        self,
        query: str,
        partition: str,
        top_k: int | None = None,
        allowed_partitions: list[str] | None = None,
    ) -> dict[str, Any]:
        return await self.search_service.search_documents(
            query=query,
            partitions=[partition],
            top_k=top_k,
            allowed_partitions=allowed_partitions,
        )

    async def search_file(
        self,
        query: str,
        partition: str,
        file_id: str,
        top_k: int | None = None,
        allowed_partitions: list[str] | None = None,
    ) -> dict[str, Any]:
        return await self.search_service.search_documents(
            query=query,
            partitions=[partition],
            top_k=top_k,
            file_id=file_id,
            allowed_partitions=allowed_partitions,
        )

    # ------------------------------------------------------------------
    # Indexation / catalog (shared with MCP)
    # ------------------------------------------------------------------

    async def list_partitions(
        self,
        allowed_partitions: list[str] | None,
    ) -> dict[str, Any]:
        return await self.indexation_service.list_partitions(allowed_partitions=allowed_partitions)

    async def list_files(
        self,
        partition: str,
        allowed_partitions: list[str] | None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return await self.indexation_service.list_files(
            partition=partition,
            allowed_partitions=allowed_partitions,
            limit=limit,
        )

    async def get_file_info(
        self,
        partition: str,
        file_id: str,
        allowed_partitions: list[str] | None,
    ) -> dict[str, Any]:
        return await self.indexation_service.get_file_info(
            partition=partition,
            file_id=file_id,
            allowed_partitions=allowed_partitions,
        )

    async def get_file_chunks(
        self,
        partition: str,
        file_id: str,
        allowed_partitions: list[str] | None,
    ) -> dict[str, Any]:
        return await self.indexation_service.get_file_chunks(
            partition=partition,
            file_id=file_id,
            allowed_partitions=allowed_partitions,
        )

    async def fuzzy_search_files(
        self,
        query: str,
        allowed_partitions: list[str] | None,
        partition: str | None = None,
        cutoff: float = 0.4,
        limit: int = 20,
    ) -> dict[str, Any]:
        return await self.indexation_service.fuzzy_search_files(
            query=query,
            allowed_partitions=allowed_partitions,
            partition=partition,
            cutoff=cutoff,
            limit=limit,
        )

    async def get_task_status(
        self,
        task_id: str,
        user_id: int | None,
    ) -> dict[str, Any]:
        return await self.indexation_service.get_task_status(task_id=task_id, user_id=user_id)

    async def list_my_tasks(
        self,
        user_id: int | None,
        task_status: str | None = None,
    ) -> dict[str, Any]:
        return await self.indexation_service.list_my_tasks(user_id=user_id, task_status=task_status)

    async def get_task_logs(
        self,
        task_id: str,
        user_id: int | None,
        log_file: str | Path,
        max_lines: int = 100,
    ) -> dict[str, Any]:
        return await self.indexation_service.get_task_logs(
            task_id=task_id,
            user_id=user_id,
            log_file=log_file,
            max_lines=max_lines,
        )

    async def get_chunk_by_id(
        self,
        chunk_id: str,
        allowed_partitions: list[str] | None,
    ) -> dict[str, Any]:
        return await self.indexation_service.get_chunk_by_id(
            chunk_id=chunk_id,
            allowed_partitions=allowed_partitions,
        )

    async def delete_file(
        self,
        partition: str,
        file_id: str,
        allowed_partitions: list[str] | None,
    ) -> dict[str, Any]:
        return await self.indexation_service.delete_file(
            partition=partition,
            file_id=file_id,
            allowed_partitions=allowed_partitions,
        )

    async def update_file_metadata(
        self,
        partition: str,
        file_id: str,
        metadata: dict[str, Any],
        allowed_partitions: list[str] | None,
    ) -> dict[str, Any]:
        return await self.indexation_service.update_file_metadata(
            partition=partition,
            file_id=file_id,
            metadata=metadata,
            allowed_partitions=allowed_partitions,
        )

    async def copy_file(
        self,
        source_partition: str,
        source_file_id: str,
        dest_partition: str,
        dest_file_id: str,
        allowed_partitions: list[str] | None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.indexation_service.copy_file(
            source_partition=source_partition,
            source_file_id=source_file_id,
            dest_partition=dest_partition,
            dest_file_id=dest_file_id,
            allowed_partitions=allowed_partitions,
            extra_metadata=extra_metadata,
        )

    async def index_url(
        self,
        url: str,
        partition: str,
        file_id: str,
        allowed_partitions: list[str] | None,
        extra_metadata: dict[str, Any] | None = None,
        task_state_manager_ref: Any = None,
    ) -> dict[str, Any]:
        return await self.indexation_service.index_url(
            url=url,
            partition=partition,
            file_id=file_id,
            allowed_partitions=allowed_partitions,
            extra_metadata=extra_metadata,
            task_state_manager_ref=task_state_manager_ref,
        )

    # ------------------------------------------------------------------
    # File ingestion
    # ------------------------------------------------------------------

    async def get_supported_types(
        self,
        accepted_formats: Any,
        dict_mimetypes: Any,
    ) -> dict[str, Any]:
        return {"extensions": list(accepted_formats), "mimetypes": list(dict_mimetypes)}

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
        task = indexer.add_file.remote(path=file_path, metadata=metadata, partition=partition, user=user)
        task_id: str = task.task_id().hex()
        await task_state_manager.set_state.remote(task_id, "QUEUED")
        await task_state_manager.set_object_ref.remote(task_id, {"ref": task})
        return {"task_id": task_id}

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
        await indexer.delete_file.remote(file_id, partition)
        task = indexer.add_file.remote(path=file_path, metadata=metadata, partition=partition, user=user)
        task_id: str = task.task_id().hex()
        await task_state_manager.set_state.remote(task_id, "QUEUED")
        await task_state_manager.set_object_ref.remote(task_id, {"ref": task})
        return {"task_id": task_id}

    async def get_task_error(
        self,
        task_state_manager: Any,
        task_id: str,
    ) -> dict[str, Any]:
        error = await task_state_manager.get_error.remote(task_id)
        return {"task_id": task_id, "traceback": error.splitlines() if error else []}

    async def cancel_task(
        self,
        task_state_manager: Any,
        task_id: str,
    ) -> dict[str, Any]:
        obj_ref = await task_state_manager.get_object_ref.remote(task_id)
        ray.cancel(obj_ref["ref"], recursive=True)
        return {"message": f"Cancellation signal sent for task {task_id}"}

    # ------------------------------------------------------------------
    # Extract
    # ------------------------------------------------------------------

    async def get_extract(
        self,
        vectordb: Any,
        extract_id: str,
    ) -> dict[str, Any]:
        chunk = await vectordb.get_chunk_by_id.remote(extract_id)
        if chunk is None:
            return {}
        return {"page_content": chunk.page_content, "metadata": chunk.metadata}

    # ------------------------------------------------------------------
    # Partition management
    # ------------------------------------------------------------------

    async def list_partition_chunks(
        self,
        vectordb: Any,
        partition: str,
        include_embedding: bool = True,
    ) -> dict[str, Any]:
        chunks = await vectordb.list_all_chunk.remote(partition=partition, include_embedding=include_embedding)
        return {"chunks": chunks}

    async def create_partition(
        self,
        vectordb: Any,
        partition: str,
        user_id: int,
    ) -> dict[str, Any]:
        await vectordb.create_partition.remote(partition=partition, user_id=user_id)
        return {"partition": partition, "created": True}

    async def delete_partition(
        self,
        vectordb: Any,
        partition: str,
    ) -> dict[str, Any]:
        await vectordb.delete_partition.remote(partition)
        return {"partition": partition, "deleted": True}

    async def list_partition_users(
        self,
        vectordb: Any,
        partition: str,
    ) -> dict[str, Any]:
        members = await vectordb.list_partition_members.remote(partition=partition)
        return {"members": members}

    async def add_partition_user(
        self,
        vectordb: Any,
        partition: str,
        user_id: int,
        role: str,
    ) -> dict[str, Any]:
        await vectordb.add_partition_member.remote(partition=partition, user_id=user_id, role=role)
        return {"added": True}

    async def remove_partition_user(
        self,
        vectordb: Any,
        partition: str,
        user_id: int,
    ) -> dict[str, Any]:
        await vectordb.remove_partition_member.remote(partition=partition, user_id=user_id)
        return {"removed": True}

    async def update_partition_user_role(
        self,
        vectordb: Any,
        partition: str,
        user_id: int,
        role: str,
    ) -> dict[str, Any]:
        await vectordb.update_partition_member_role.remote(partition=partition, user_id=user_id, new_role=role)
        return {"updated": True}

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------

    async def get_queue_info(
        self,
        task_state_manager: Any,
    ) -> dict[str, Any]:
        all_states: dict = await task_state_manager.get_all_states.remote()
        status_counts = Counter(all_states.values())
        active_statuses = ["QUEUED", "SERIALIZING", "CHUNKING", "INSERTING"]
        active = {s: status_counts.get(s, 0) for s in active_statuses}
        task_summary = {
            "active": sum(active.values()),
            "active_statuses": active,
            "total_completed": status_counts.get("COMPLETED", 0),
            "total_failed": status_counts.get("FAILED", 0),
        }
        worker_info = await task_state_manager.get_pool_info.remote()
        workers = {
            "total_slots": worker_info["total_capacity"],
            "pool_size": worker_info["pool_size"],
            "max_per_actor": worker_info["max_tasks_per_worker"],
        }
        return {"workers": workers, "tasks": task_summary}

    async def list_tasks(
        self,
        user_id: int | None,
        task_status: str | None = None,
    ) -> dict[str, Any]:
        return await self.list_my_tasks(user_id=user_id, task_status=task_status)

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    async def list_users(
        self,
        vectordb: Any,
    ) -> dict[str, Any]:
        users = await vectordb.list_users.remote()
        return {"users": users}

    async def get_current_user(
        self,
        user: dict[str, Any],
    ) -> dict[str, Any]:
        return user

    async def create_user(
        self,
        vectordb: Any,
        display_name: str | None = None,
        external_user_id: str | None = None,
        is_admin: bool = False,
    ) -> dict[str, Any]:
        return await vectordb.create_user.remote(
            display_name=display_name,
            external_user_id=external_user_id,
            is_admin=is_admin,
        )

    async def get_user(
        self,
        vectordb: Any,
        user_id: int,
    ) -> dict[str, Any]:
        return await vectordb.get_user.remote(user_id)

    async def delete_user_account(
        self,
        vectordb: Any,
        user_id: int,
    ) -> dict[str, Any]:
        await vectordb.delete_user.remote(user_id)
        return {"deleted": True}

    async def regenerate_user_token(
        self,
        vectordb: Any,
        user_id: int,
    ) -> dict[str, Any]:
        return await vectordb.regenerate_user_token.remote(user_id)

    # ------------------------------------------------------------------
    # Actors
    # ------------------------------------------------------------------

    async def list_ray_actors(
        self,
        actors: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {"actors": actors}

    async def restart_actor(
        self,
        actor_name: str,
        actor_creation_map: dict[str, Any],
    ) -> dict[str, Any]:
        """Kill and recreate a named Ray actor.

        Returns ``{ message, actor_name, actor_id }``.
        Raises ``KeyError`` if *actor_name* is not in *actor_creation_map*.
        Raises ``RuntimeError`` if kill or recreation fails.
        """
        if actor_name not in actor_creation_map:
            raise KeyError(f"Unknown actor: {actor_name}")

        # Kill existing actor (if alive)
        try:
            actor = ray.get_actor(actor_name, namespace="openrag")
            ray.kill(actor, no_restart=True)
        except ValueError:
            pass  # Actor not found; will be created fresh

        # Recreate
        new_actor = actor_creation_map[actor_name]()
        if "Semaphore" in actor_name:
            new_actor = new_actor._actor
        return {
            "message": f"Actor {actor_name} restarted successfully.",
            "actor_name": actor_name,
            "actor_id": new_actor._actor_id.hex(),
        }

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    async def list_tools(
        self,
        tools: list[Any],
    ) -> dict[str, Any]:
        return {"tools": tools}

    async def execute_tool(
        self,
        tool_name: str,
        file_path: Path,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a named tool against an already-saved file.

        Currently supports ``"extractText"`` only.
        Returns ``{ message: <extracted text> }``.
        Raises ``ValueError`` for unknown tool names.
        """
        from components.indexer.utils.files import serialize_file
        from components.indexer.utils.text_sanitizer import sanitize_extracted_text

        if tool_name == "extractText":
            task_id = ray.get_runtime_context().get_task_id()
            doc = await serialize_file(task_id, path=file_path, metadata=metadata)
            sanitized = sanitize_extracted_text(doc.page_content)
            return {"message": sanitized}

        raise ValueError(f"Unknown tool: {tool_name}")

    # ------------------------------------------------------------------
    # OpenAI-compatible endpoints
    # ------------------------------------------------------------------

    async def list_models(
        self,
        vectordb: Any,
        user_partitions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return an OpenAI-compatible model list from the available partitions."""
        import consts

        if [p["partition"] for p in user_partitions] == ["all"]:
            user_partitions = await vectordb.list_partitions.remote()

        models = [
            {
                "id": f"{consts.PARTITION_PREFIX}{p['partition']}",
                "object": "model",
                "created": p["created_at"],
                "owned_by": "OpenRAG",
            }
            for p in user_partitions
        ]
        models.append(
            {
                "id": f"{consts.PARTITION_PREFIX}all",
                "object": "model",
                "created": 0,
                "owned_by": "OpenRAG",
            }
        )
        return {"object": "list", "data": models}

    def _get_ragpipe(self):
        if self._ragpipe is None:
            from components.pipeline import RagPipeline  # noqa: PLC0415
            from config import load_config  # noqa: PLC0415

            self._ragpipe = RagPipeline(config=load_config())
        return self._ragpipe

    async def openai_chat_completion(
        self,
        payload: dict[str, Any],
        partitions: list[str] | None,
    ) -> tuple[AsyncIterator, list[Any]]:
        """Run the RAG chat-completion pipeline.

        Returns ``(llm_output_iterator, source_docs)``.
        """
        llm_output, docs = await self._get_ragpipe().chat_completion(partition=partitions, payload=payload)
        return llm_output, docs

    async def openai_completion(
        self,
        payload: dict[str, Any],
        partitions: list[str] | None,
    ) -> tuple[AsyncIterator, list[Any]]:
        """Run the RAG completion pipeline.

        Returns ``(llm_output_iterator, source_docs)``.
        """
        llm_output, docs = await self._get_ragpipe().completions(partition=partitions, payload=payload)
        return llm_output, docs
