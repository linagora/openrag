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

import string
from typing import TYPE_CHECKING

from core.models.prompt import Prompt, PromptType
from core.prompts.template_loader import load_template_by_key
from core.utils.exceptions import ConfigError, NotFoundError, ValidationError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.config.root import Settings
    from core.ports.prompt_repo import PromptRepository

logger = get_logger()

_VALID_TYPES = frozenset(t.value for t in PromptType)

# Prompt types whose content is a ``str.format`` template rendered on the hot
# path, mapped to the exact placeholders the pipeline substitutes. Content saved
# for these types MUST use only these ``{placeholders}`` (and escape any literal
# brace as ``{{``/``}}``), or the per-request ``.format(...)`` would raise and
# 500 the chat/retrieval path — globally if it's the type's default. Validated at
# write time (create/update) so an invalid template can never be stored.
#
# Types NOT listed here (chunk_contextualizer, image_captioning, topic_tagger)
# are sent to the LLM verbatim as a system message — never ``.format``-ed — so
# they may contain any literal text, braces included, and need no validation.
_PROMPT_FORMAT_FIELDS: dict[str, frozenset[str]] = {
    PromptType.SYS_PROMPT.value: frozenset({"context", "current_date"}),
    PromptType.QUERY_CONTEXTUALIZER.value: frozenset({"query_language", "current_date"}),
    PromptType.HYDE.value: frozenset({"question"}),
    PromptType.MULTI_QUERY.value: frozenset({"query", "k_queries"}),
}


def _validate_template(prompt_type: str, content: str) -> None:
    """Reject a format-templated prompt whose ``{placeholders}`` are malformed or
    unknown for its type. No-op for verbatim (non-formatted) prompt types.

    Raises ``ValidationError`` (422) so the admin sees a precise message instead
    of a later 500 on the chat path.
    """
    allowed = _PROMPT_FORMAT_FIELDS.get(prompt_type)
    if allowed is None:
        return
    try:
        # Formatter.parse yields (literal, field_name, format_spec, conversion);
        # field_name is None for literal text and for escaped {{/}}. It raises
        # ValueError on an unbalanced single brace.
        fields = {fname for _, fname, _, _ in string.Formatter().parse(content) if fname is not None}
    except ValueError as exc:
        raise ValidationError(
            f"Prompt template has malformed braces ({exc}). Escape a literal brace as '{{{{' or '}}}}'.",
            status_code=422,
            code="PROMPT_TEMPLATE_INVALID",
        ) from exc
    # Reduce "context.attr" / "context[0]" / "" (auto-numbering) to their root name.
    roots = {f.split(".")[0].split("[")[0] for f in fields}
    unknown = {r for r in roots if r not in allowed}
    if unknown:
        raise ValidationError(
            f"Prompt template uses unknown placeholder(s) {sorted(unknown)} for type "
            f"'{prompt_type}'. Allowed: {sorted(allowed)} (escape a literal brace as '{{{{'/'}}}}').",
            status_code=422,
            code="PROMPT_TEMPLATE_INVALID",
        )


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
            try:
                # Seeding writes straight to the repo, so it would otherwise be
                # the one path that stores content the CRUD API would reject. A
                # bundled template with a bad placeholder must not become a
                # type's global default: every request falling back to it would
                # raise inside .format() at the point of use.
                _validate_template(prompt_type, content)
            except ValidationError as exc:
                logger.warning(f"Bundled template for '{prompt_type}' is not a valid template; not seeding: {exc}")
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
        library prompt), then the global default, then the on-disk seed.
        ``names`` entries may be ``None``/empty (skipped) so callers can pass
        optional config values directly.

        Returns a string in every reachable case: boot seeds a default per type
        and deleting a type's default is refused, so reaching the disk seed is
        already an anomaly. If even that is unreadable there is no prompt to
        return, and inventing one would silently degrade generation — so this
        raises a typed :class:`ConfigError` naming the type instead of letting a
        bare ``FileNotFoundError`` surface as an opaque 500. Callers that must
        never fail (the ingest path) catch it and fall back to their own
        disk-loaded prompt.
        """
        candidates = [n for n in (names or ()) if n]
        for name in candidates:
            prompt = await self._repo.get_by_name(prompt_type, name)
            if prompt is not None:
                self._log_resolution(prompt_type, candidates, "named", name, prompt.content)
                return prompt.content
        default = await self._repo.get_default(prompt_type)
        if default is not None:
            self._log_resolution(prompt_type, candidates, "default", default.name, default.content)
            return default.content
        try:
            content = self._disk_seed(prompt_type)
        except (FileNotFoundError, ValueError, KeyError) as exc:
            raise ConfigError(
                f"No prompt available for type '{prompt_type}': no library default and "
                f"no readable bundled template ({exc}).",
                code="PROMPT_UNAVAILABLE",
            ) from exc
        self._log_resolution(prompt_type, candidates, "disk-seed", None, content)
        return content

    @staticmethod
    def _log_resolution(prompt_type: str, candidates: list[str], source: str, name: str | None, content: str) -> None:
        """Emit one line per resolution so operators can confirm, in the logs,
        exactly which library prompt each pipeline stage (indexation /
        retrieval / chat) actually used, and preview its text.

        ``source`` is how it resolved: ``named`` (a partition/preset selection),
        ``default`` (the type's global default), or ``disk-seed`` (bundled
        fallback). ``candidates`` are the names the caller offered, in order.

        DEBUG, not INFO: this fires on every chat request and every indexing
        job, and it carries prompt text. It pairs with the ``llm.call`` line
        from the inference clients, which sits at the same level — turn on
        ``LOG_LEVEL=DEBUG`` to see which prompt a stage picked *and* what
        actually went to the model. The preview is built lazily so an INFO
        deployment pays nothing for it.
        """
        logger.bind(
            prompt_type=prompt_type,
            candidates=candidates,
            source=source,
            resolved_name=name,
            length=len(content),
        ).opt(lazy=True).debug(
            f"prompt.resolve {prompt_type} <- {source}{f':{name}' if name else ''} | " + "{}",
            lambda: repr(" ".join(content.split())[:80]),
        )

    # ------------------------------------------------------------------
    # Library CRUD
    # ------------------------------------------------------------------

    async def create_prompt(self, *, prompt_type: str, name: str, content: str, is_default: bool = False) -> Prompt:
        self._validate_type(prompt_type)
        _validate_template(prompt_type, content)
        if await self._repo.get_by_name(prompt_type, name) is not None:
            raise ValidationError(
                f"A '{prompt_type}' prompt named '{name}' already exists.",
                status_code=409,
                code="PROMPT_EXISTS",
            )
        return await self._repo.create(
            Prompt(prompt_type=prompt_type, name=name, content=content, is_default=is_default)
        )

    async def get_prompt(self, prompt_id: str) -> Prompt:
        prompt = await self._repo.get(prompt_id)
        if prompt is None:
            raise NotFoundError(f"Prompt '{prompt_id}' not found.")
        return prompt

    async def list_prompts(self, *, prompt_type: str | None = None, offset: int = 0, limit: int = 100) -> list[dict]:
        """List prompts, each annotated with ``used_by`` — the number of
        partitions/presets that reference it by name (one bulk aggregate)."""
        if prompt_type is not None:
            self._validate_type(prompt_type)
        prompts = await self._repo.list(prompt_type=prompt_type, offset=offset, limit=limit)
        counts = await self._repo.reference_counts()
        return [{**p.model_dump(), "used_by": counts.get((p.prompt_type, p.name), 0)} for p in prompts]

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

        new_content = fields.get("content")
        if new_content is not None:
            _validate_template(existing.prompt_type, str(new_content))

        new_name = fields.get("name")
        if new_name is not None and new_name != existing.name:
            clash = await self._repo.get_by_name(existing.prompt_type, new_name)
            if clash is not None and clash.id != prompt_id:
                raise ValidationError(
                    f"A '{existing.prompt_type}' prompt named '{new_name}' already exists.",
                    status_code=409,
                    code="PROMPT_EXISTS",
                )

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
