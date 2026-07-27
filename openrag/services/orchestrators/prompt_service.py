"""PromptService — seeding, resolution, and CRUD for the DB prompt library.

Orchestrates :class:`PromptRepository` to expose the prompt library to the
admin API and to answer the one question the rest of the system asks:

    resolve_prompt(prompt_type, names=[...]) -> str

Selection is by name: a preset (indexation/retrieval) or a partition
(generation) names a library prompt per type. The caller passes the
precedence-ordered candidate names for a request; the first that resolves
wins, else the global default, else the on-disk seed template. Passing an
ordered list is the extension point — e.g. per-user personalization prepends a
user's prompt name ahead of the partition's without changing this signature.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.models.prompt import Prompt, PromptType
from core.prompts.template_loader import load_template_by_key
from core.utils.exceptions import NotFoundError, ValidationError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.config.root import Settings
    from core.ports.prompt_repo import PromptRepository

logger = get_logger()

_VALID_TYPES = frozenset(t.value for t in PromptType)

# Canonical ``prompt_type`` (a ``PromptType`` value, and the DB key) → the
# ``PromptsConfig`` attribute the on-disk template loader looks up. Identity for
# every type except image captioning, whose config attribute is historically
# ``image_describer`` while its prompt type is ``image_captioning``. This map is
# the single reconciliation point between the DB type namespace and the disk
# filename namespace; keep it exhaustive over PromptType.
_TYPE_TO_CONFIG_KEY: dict[str, str] = {
    PromptType.SYS_PROMPT.value: "sys_prompt",
    PromptType.QUERY_CONTEXTUALIZER.value: "query_contextualizer",
    PromptType.CHUNK_CONTEXTUALIZER.value: "chunk_contextualizer",
    PromptType.IMAGE_CAPTIONING.value: "image_describer",
    PromptType.HYDE.value: "hyde",
    PromptType.MULTI_QUERY.value: "multi_query",
    PromptType.SPOKEN_STYLE_ANSWER.value: "spoken_style_answer",
    PromptType.TOPIC_TAGGER.value: "topic_tagger",
}


class PromptService:
    """CRUD, resolution, and lifecycle for DB-backed prompts."""

    def __init__(self, *, prompt_repo: PromptRepository, config: Settings) -> None:
        self._repo = prompt_repo
        self._config = config

    # ------------------------------------------------------------------
    # Startup lifecycle
    # ------------------------------------------------------------------

    async def seed_defaults(self) -> None:
        """Create one default library prompt per type from its disk template.

        Idempotent per type: if a default already exists for a type it is left
        untouched, so an admin's edits survive restarts. A type whose disk
        template is missing is skipped with a warning rather than aborting boot.
        """
        for prompt_type in _TYPE_TO_CONFIG_KEY:
            if await self._repo.get_default(prompt_type) is not None:
                continue
            try:
                content = self._disk_seed(prompt_type)
            except (FileNotFoundError, ValueError) as exc:
                logger.warning(f"No disk template to seed prompt type '{prompt_type}': {exc}")
                continue
            await self._repo.create(
                Prompt(
                    prompt_type=prompt_type,
                    name=f"default_{prompt_type}",
                    content=content,
                    is_default=True,
                )
            )
            logger.info(f"Seeded default prompt for '{prompt_type}'.")

    def _disk_seed(self, prompt_type: str) -> str:
        """Read a prompt type's bundled template from disk (honours PROMPTS_DIR)."""
        config_key = _TYPE_TO_CONFIG_KEY[prompt_type]
        return load_template_by_key(self._config.paths.prompts_dir, self._config.prompts, config_key)

    # ------------------------------------------------------------------
    # Resolution — the single seam
    # ------------------------------------------------------------------

    async def resolve_prompt(self, prompt_type: str, names: Sequence[str | None] | None = None) -> str:
        """Resolve the effective prompt text for ``prompt_type``.

        Tries each candidate ``name`` in order (a preset- or partition-named
        library prompt), then the global default, then the on-disk seed. Always
        returns a string. ``names`` entries may be ``None``/empty (skipped) so
        callers can pass optional config values directly.
        """
        for name in names or ():
            if name:
                prompt = await self._repo.get_by_name(prompt_type, name)
                if prompt is not None:
                    return prompt.content
        default = await self._repo.get_default(prompt_type)
        if default is not None:
            return default.content
        return self._disk_seed(prompt_type)

    # ------------------------------------------------------------------
    # Library CRUD
    # ------------------------------------------------------------------

    async def create_prompt(self, *, prompt_type: str, name: str, content: str, is_default: bool = False) -> Prompt:
        self._validate_type(prompt_type)
        return await self._repo.create(
            Prompt(prompt_type=prompt_type, name=name, content=content, is_default=is_default)
        )

    async def get_prompt(self, prompt_id: str) -> Prompt:
        prompt = await self._repo.get(prompt_id)
        if prompt is None:
            raise NotFoundError(f"Prompt '{prompt_id}' not found.")
        return prompt

    async def list_prompts(self, *, prompt_type: str | None = None, offset: int = 0, limit: int = 100) -> list[Prompt]:
        if prompt_type is not None:
            self._validate_type(prompt_type)
        return await self._repo.list(prompt_type=prompt_type, offset=offset, limit=limit)

    async def update_prompt(self, prompt_id: str, **fields: object) -> Prompt:
        """Update ``name``/``content`` and/or promote to default.

        ``is_default=True`` is routed through the repo's atomic set_default
        (clear-then-set) rather than a plain column write; a falsey value is a
        no-op (you switch the default by promoting another prompt, never by
        leaving the type with none).
        """
        existing = await self._repo.get(prompt_id)
        if existing is None:
            raise NotFoundError(f"Prompt '{prompt_id}' not found.")

        promote_to_default = bool(fields.pop("is_default", None))
        updated = existing
        if fields:
            updated = await self._repo.update(prompt_id, **fields) or existing
        if promote_to_default:
            updated = await self._repo.set_default(prompt_id) or updated
        return updated

    async def set_default(self, prompt_id: str) -> Prompt:
        result = await self._repo.set_default(prompt_id)
        if result is None:
            raise NotFoundError(f"Prompt '{prompt_id}' not found.")
        return result

    async def delete_prompt(self, prompt_id: str) -> None:
        """Delete a library prompt.

        Refuses to delete a type's current default — removing it would strand
        every prompt that resolves to the default on the disk seed and leave the
        library with no default for that type. Promote another prompt first.
        """
        existing = await self._repo.get(prompt_id)
        if existing is None:
            raise NotFoundError(f"Prompt '{prompt_id}' not found.")
        if existing.is_default:
            raise ValidationError(
                f"Cannot delete the default '{existing.prompt_type}' prompt. Set another default first.",
            )
        await self._repo.delete(prompt_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _validate_type(self, prompt_type: str) -> None:
        if prompt_type not in _VALID_TYPES:
            raise ValidationError(
                f"Invalid prompt_type '{prompt_type}'. Must be one of: {sorted(_VALID_TYPES)}",
            )


__all__ = ["PromptService", "PROMPT_TYPE_KEYS"]

# Public view of the canonical type set, for callers that enumerate managed
# prompt types without reaching into the private map.
PROMPT_TYPE_KEYS = tuple(_TYPE_TO_CONFIG_KEY)
