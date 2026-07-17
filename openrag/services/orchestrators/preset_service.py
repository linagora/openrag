"""PresetService — CRUD, default seeding, validation, and live re-resolution.

Orchestrates PresetRepository to maintain the named pipeline preset registry
in the DB and in the in-memory config (Settings.presets). On first boot it
seeds 6 default presets (3 indexation + 3 retrieval) so partitions resolve
without any admin intervention.

When a preset is updated the service calls partition_service.load_partitions()
so all affected partitions pick up the change without a restart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.config.indexation_pipeline import IndexationPipelineConfig
from core.config.retrieval_pipeline import RetrievalPipelineConfig
from core.utils.exceptions import NotFoundError, ValidationError
from core.utils.logging import get_logger
from services.orchestrators.partition_service import PartitionService

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.ports.preset_repo import PresetRepository

logger = get_logger()

_VALID_PRESET_TYPES = frozenset({"indexation", "retrieval"})

# The 'default' preset is the system fallback: partitions.{indexation,retrieval}_preset
# default to this name (schema server_default + create_partition), and
# resolve_partition_row raises if it is missing. It must always exist under this
# name, so renaming it away or deleting it is rejected. Editing its config is fine.
_RESERVED_PRESET_NAME = "default"

_DEFAULT_SEEDS: dict[str, dict[str, dict[str, Any]]] = {
    # ``enable_contextualization``, ``enable_image_captioning``, and
    # ``parsing_strategy`` are intentionally omitted from the ``default``
    # indexation preset below. The first two are injected at seed time from
    # the global ``chunker.contextual_retrieval`` (``CONTEXTUAL_RETRIEVAL``)
    # and ``loader.image_captioning`` (``IMAGE_CAPTIONING``) toggles in
    # _finalize_seed; the last stays unset so the default preset inherits the
    # global ``file_loaders.pdf`` (``PDFLOADER``) backend at parse time.
    # Named presets (legal/finance) keep their explicit choice.
    "indexation": {
        "default": {
            "chunking": {"name": "recursive_splitter", "chunk_size": 512, "chunk_overlap_rate": 0.2},
            # ``parsing_strategy`` is intentionally omitted so the default preset
            # inherits the deployment's global PDFLOADER (``file_loaders.pdf``)
            # rather than forcing one PDF backend on every partition. A hardcoded
            # value here would override the operator's global choice and lazily
            # spin up that backend's Ray pool — e.g. forcing marker on a
            # pymupdf-configured deployment. Named presets below opt in explicitly.
            "enable_entity_extraction": True,
            "enable_topic_tagging": True,
        },
        "legal": {
            "chunking": {"name": "recursive_splitter", "chunk_size": 1024, "chunk_overlap_rate": 0.25},
            "parsing_strategy": "marker",
            "enable_image_captioning": True,
            "enable_contextualization": True,
            "contextualization_mode": "structured",
        },
        "finance": {
            "chunking": {"name": "recursive_splitter", "chunk_size": 768, "chunk_overlap_rate": 0.2},
            "parsing_strategy": "marker",
            "enable_image_captioning": True,
            "enable_contextualization": False,
        },
    },
    # ``enable_reranker`` is intentionally omitted here — it is injected at seed
    # time from the global ``reranker.enabled`` kill-switch (see _finalize_seed)
    # so a deployment without a reranker (CPU-only, CI) does not force reranking
    # on every partition and then fail against an unreachable reranker endpoint.
    "retrieval": {
        "default": {
            "type": "single",
            "top_k": 50,
            "top_n": 10,
            "similarity_threshold": 0.6,
        },
        "multiquery": {
            "type": "multiQuery",
            "top_k": 50,
            "top_n": 10,
        },
        "hyde": {
            "type": "hyde",
            "top_k": 50,
            "top_n": 10,
        },
    },
}


class PresetService:
    """CRUD and lifecycle management for named pipeline presets."""

    def __init__(
        self,
        *,
        preset_repo: PresetRepository,
        config: Settings,
        partition_service: PartitionService | None = None,
    ) -> None:
        self._repo = preset_repo
        self._config = config
        self._partition_service = partition_service

    # ------------------------------------------------------------------
    # Startup lifecycle
    # ------------------------------------------------------------------

    async def seed_defaults(self) -> None:
        """Insert default presets if the DB has no rows for that type.

        Each preset type is seeded independently — if indexation presets exist
        but retrieval is empty, only retrieval gets seeded.
        """
        for preset_type, presets in _DEFAULT_SEEDS.items():
            existing = await self._repo.list_all(preset_type=preset_type)
            if existing:
                continue
            for name, config in presets.items():
                await self._repo.upsert(name, preset_type, self._finalize_seed(name, preset_type, config))
                logger.info(f"Seeded default {preset_type} preset '{name}'.")

    def _finalize_seed(self, name: str, preset_type: str, config: dict[str, Any]) -> dict[str, Any]:
        """Overlay env-derived global toggles onto a static default seed.

        Top-level kill-switches are folded into the seeded presets so a
        fresh deployment honours the env flags without admin action:

        * every retrieval preset inherits ``reranker.enabled`` (``enable_reranker``)
          so a deployment without a reranker never builds one against a
          missing/unreachable endpoint;
        * the ``default`` indexation preset inherits ``chunker.contextual_retrieval``
          (``CONTEXTUAL_RETRIEVAL``) as ``enable_contextualization`` and
          ``loader.image_captioning`` (``IMAGE_CAPTIONING``) as
          ``enable_image_captioning``, so the global toggles still drive
          these enrichments on a fresh deployment. Named presets keep the
          explicit value carried in the seed.

        This is a one-time seed-time default, not a live runtime gate: once a
        preset row exists, it is the sole source of truth (an admin can flip
        ``enable_image_captioning`` per partition regardless of the current
        ``IMAGE_CAPTIONING`` env value) — see ``IndexingPipeline._should_caption``.
        """
        if preset_type == "retrieval":
            return {**config, "enable_reranker": self._config.reranker.enabled}
        if preset_type == "indexation" and name == "default":
            return {
                **config,
                "enable_contextualization": self._config.chunker.contextual_retrieval,
                "enable_image_captioning": self._config.loader.image_captioning,
            }
        return config

    async def load_all(self) -> None:
        """Fetch all presets from DB, rebuild config.presets dicts atomically.

        Uses clear()+update() so any existing references to the dict objects
        (e.g. held by PartitionService) stay valid after the swap.
        """
        rows = await self._repo.list_all()
        idx_bucket: dict[str, dict[str, Any]] = {}
        ret_bucket: dict[str, dict[str, Any]] = {}

        for row in rows:
            if row["preset_type"] == "indexation":
                idx_bucket[row["name"]] = row["config"]
            elif row["preset_type"] == "retrieval":
                ret_bucket[row["name"]] = row["config"]

        presets = self._config.presets
        presets.indexation.clear()
        presets.indexation.update(idx_bucket)
        presets.retrieval.clear()
        presets.retrieval.update(ret_bucket)

        logger.info(
            "Loaded pipeline presets.",
            n_indexation=len(idx_bucket),
            n_retrieval=len(ret_bucket),
        )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create_preset(self, name: str, preset_type: str, config: dict[str, Any]) -> dict:
        """Register a new preset; raises 409 if (name, preset_type) already exists."""
        if preset_type not in _VALID_PRESET_TYPES:
            raise ValidationError(f"Invalid preset_type '{preset_type}'. Must be 'indexation' or 'retrieval'.")
        existing = await self._repo.get(name, preset_type)
        if existing is not None:
            raise ValidationError(
                f"Preset '{name}' of type '{preset_type}' already exists.",
                status_code=409,
                code="PRESET_EXISTS",
            )
        self._validate_config(preset_type, config)
        result = await self._repo.upsert(name, preset_type, config)
        await self.load_all()
        return result

    async def get_preset(self, name: str, preset_type: str) -> dict:
        """Fetch one preset row; raises 404 if not found."""
        row = await self._repo.get(name, preset_type)
        if row is None:
            raise NotFoundError(f"Preset '{name}' of type '{preset_type}' not found.")
        return row

    async def list_presets(self, preset_type: str | None = None) -> list[dict]:
        """List presets, each annotated with ``used_by_partitions``.

        The count comes from a single bulk aggregate query rather than one
        count query per preset row.
        """
        rows = await self._repo.list_all(preset_type=preset_type)
        counts = await self._repo.usage_counts()
        return [{**row, "used_by_partitions": counts.get((row["name"], row["preset_type"]), 0)} for row in rows]

    async def update_preset(self, name: str, preset_type: str, **fields: object) -> dict:
        """Update preset config and/or rename it.

        Pass ``new_name=`` to rename. If the config changes, all partitions
        referencing this preset are re-resolved immediately.
        """
        existing = await self._repo.get(name, preset_type)
        if existing is None:
            raise NotFoundError(f"Preset '{name}' of type '{preset_type}' not found.")

        new_name: str | None = fields.pop("new_name", None)  # type: ignore[assignment]
        new_config: dict | None = fields.pop("config", None)  # type: ignore[assignment]

        if new_config is not None:
            self._validate_config(preset_type, new_config)

        effective_config = new_config if new_config is not None else existing["config"]
        effective_name = name

        if new_name and new_name != name:
            if name == _RESERVED_PRESET_NAME:
                raise ValidationError(
                    f"Cannot rename the reserved '{_RESERVED_PRESET_NAME}' {preset_type} preset: "
                    "it is the system fallback every partition resolves to."
                )
            if await self._repo.get(new_name, preset_type) is not None:
                raise ValidationError(
                    f"Cannot rename preset to '{new_name}': a {preset_type} preset with that name already exists."
                )
            effective_name = new_name
            result = await self._repo.rename(name, effective_name, preset_type, effective_config)
        else:
            result = await self._repo.upsert(effective_name, preset_type, effective_config)
        await self.load_all()

        if self._partition_service is not None:
            await self._partition_service.load_partitions()

        return result

    async def delete_preset(self, name: str, preset_type: str) -> None:
        """Delete a preset.

        Raises 404 if not found, 422 if it's the reserved 'default' preset,
        409 (ConflictError) if any partition references it — that in-use
        check is enforced atomically by the repo so a concurrent partition
        assignment can't race past it.
        """
        existing = await self._repo.get(name, preset_type)
        if existing is None:
            raise NotFoundError(f"Preset '{name}' of type '{preset_type}' not found.")

        if name == _RESERVED_PRESET_NAME:
            raise ValidationError(
                f"Cannot delete the reserved '{_RESERVED_PRESET_NAME}' {preset_type} preset: "
                "it is the system fallback every partition resolves to."
            )

        await self._repo.delete(name, preset_type)
        await self.load_all()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_config(self, preset_type: str, config: dict[str, Any]) -> None:
        """Instantiate the Pydantic pipeline model to validate the config dict."""
        try:
            match preset_type:
                case "indexation":
                    IndexationPipelineConfig(**config)
                case "retrieval":
                    RetrievalPipelineConfig(**config)
        except Exception as exc:
            raise ValidationError(f"Invalid {preset_type} preset config: {exc}") from exc


__all__ = ["PresetService"]
