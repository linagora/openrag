"""Unit tests for PromptService.

Uses an in-memory fake repository so the resolution precedence, seeding, and
validation logic are tested without a database. Seeding runs against the *real*
bundled templates, which also verifies the prompt_type → config-key map lines
up with the on-disk filenames for all managed types.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from core.config.infrastructure import PathsConfig, PromptsConfig
from core.models.prompt import Prompt, PromptType
from core.utils.exceptions import ConfigError, NotFoundError, ValidationError
from loguru import logger
from services.orchestrators.prompt_service import PROMPT_TYPE_KEYS, PromptService


class FakePromptRepo:
    """Minimal in-memory PromptRepository honouring the same invariants."""

    def __init__(self) -> None:
        self.prompts: dict[str, Prompt] = {}

    async def create(self, prompt: Prompt) -> Prompt:
        if prompt.is_default:
            for p in self.prompts.values():
                if p.prompt_type == prompt.prompt_type:
                    p.is_default = False
        self.prompts[prompt.id] = prompt
        return prompt

    async def get(self, prompt_id: str) -> Prompt | None:
        return self.prompts.get(prompt_id)

    async def list(self, *, prompt_type=None, offset=0, limit=100) -> list[Prompt]:
        rows = [p for p in self.prompts.values() if prompt_type is None or p.prompt_type == prompt_type]
        rows.sort(key=lambda p: (p.prompt_type, p.name, p.created_at))
        return rows[offset : offset + limit]

    async def count(self, *, prompt_type=None) -> int:
        return len([p for p in self.prompts.values() if prompt_type is None or p.prompt_type == prompt_type])

    async def update(self, prompt_id: str, **fields) -> Prompt | None:
        p = self.prompts.get(prompt_id)
        if p is None:
            return None
        for k in ("name", "content"):
            if k in fields:
                setattr(p, k, fields[k])
        return p

    async def delete(self, prompt_id: str) -> bool:
        return self.prompts.pop(prompt_id, None) is not None

    async def get_by_name(self, prompt_type: str, name: str) -> Prompt | None:
        return next(
            (p for p in self.prompts.values() if p.prompt_type == prompt_type and p.name == name),
            None,
        )

    async def reference_counts(self) -> dict[tuple[str, str], int]:
        return getattr(self, "_ref_counts", {})

    async def get_default(self, prompt_type: str) -> Prompt | None:
        return next((p for p in self.prompts.values() if p.prompt_type == prompt_type and p.is_default), None)

    async def set_default(self, prompt_id: str) -> Prompt | None:
        target = self.prompts.get(prompt_id)
        if target is None:
            return None
        for p in self.prompts.values():
            if p.prompt_type == target.prompt_type:
                p.is_default = False
        target.is_default = True
        return target


def _service(repo: FakePromptRepo | None = None) -> PromptService:
    config = SimpleNamespace(paths=PathsConfig(), prompts=PromptsConfig())
    return PromptService(prompt_repo=repo or FakePromptRepo(), config=config)


class TestSeeding:
    async def test_seeds_all_prompt_types_from_disk(self):
        repo = FakePromptRepo()
        await _service(repo).seed_defaults()
        seeded_types = {p.prompt_type for p in repo.prompts.values()}
        assert seeded_types == set(PROMPT_TYPE_KEYS)
        assert len(PROMPT_TYPE_KEYS) == 9
        for p in repo.prompts.values():
            assert p.is_default is True
            assert p.content.strip()

        asr_prompt = await repo.get_default(PromptType.ASR_TRANSCRIPTION.value)
        assert asr_prompt is not None
        assert "[S01]" in asr_prompt.content
        assert "Do not include timestamps" in asr_prompt.content

    async def test_seeding_is_idempotent(self):
        repo = FakePromptRepo()
        svc = _service(repo)
        await svc.seed_defaults()
        sysp = await repo.get_default("sys_prompt")
        sysp.content = "OPERATOR EDIT"
        await svc.seed_defaults()
        assert (await repo.get_default("sys_prompt")).content == "OPERATOR EDIT"
        assert len(repo.prompts) == 9

    async def test_seeding_skips_blank_non_asr_template(self, monkeypatch):
        repo = FakePromptRepo()
        svc = _service(repo)
        disk_seed = svc._disk_seed

        monkeypatch.setattr(
            svc,
            "_disk_seed",
            lambda prompt_type: "" if prompt_type == PromptType.SYS_PROMPT.value else disk_seed(prompt_type),
        )

        await svc.seed_defaults()

        assert await repo.get_default(PromptType.SYS_PROMPT.value) is None
        assert await repo.get_default(PromptType.ASR_TRANSCRIPTION.value) is not None
        with pytest.raises(ConfigError) as exc:
            await svc.resolve_prompt(PromptType.SYS_PROMPT.value)
        assert exc.value.code == "PROMPT_UNAVAILABLE"

    async def test_seeding_normalizes_whitespace_only_asr_template(self, monkeypatch):
        repo = FakePromptRepo()
        svc = _service(repo)
        disk_seed = svc._disk_seed

        monkeypatch.setattr(
            svc,
            "_disk_seed",
            lambda prompt_type: " \n\t "
            if prompt_type == PromptType.ASR_TRANSCRIPTION.value
            else disk_seed(prompt_type),
        )

        await svc.seed_defaults()

        assert (await repo.get_default(PromptType.ASR_TRANSCRIPTION.value)).content == ""

    async def test_type_set_matches_enum(self):
        assert set(PROMPT_TYPE_KEYS) == {t.value for t in PromptType}


class TestResolution:
    async def test_precedence_named_then_default_then_disk(self):
        repo = FakePromptRepo()
        svc = _service(repo)

        # Nothing in DB → disk seed fallback (never empty).
        disk = await svc.resolve_prompt("sys_prompt")
        assert disk.strip()

        # Global default → wins over disk.
        await repo.create(Prompt(prompt_type="sys_prompt", name="default_sys", content="DEFAULT", is_default=True))
        assert await svc.resolve_prompt("sys_prompt") == "DEFAULT"

        # A named prompt → wins over default when named.
        await repo.create(Prompt(prompt_type="sys_prompt", name="legal", content="LEGAL"))
        assert await svc.resolve_prompt("sys_prompt", names=["legal"]) == "LEGAL"
        # Unknown / None names are skipped, falling through to the default.
        assert await svc.resolve_prompt("sys_prompt", names=["missing"]) == "DEFAULT"
        assert await svc.resolve_prompt("sys_prompt", names=[None]) == "DEFAULT"

    async def test_first_resolvable_name_wins(self):
        repo = FakePromptRepo()
        svc = _service(repo)
        await repo.create(Prompt(prompt_type="hyde", name="b", content="B"))
        # Ordered candidates: first that resolves wins (extension point for a
        # future per-user tier prepended ahead of the partition/preset name).
        assert await svc.resolve_prompt("hyde", names=["a", "b"]) == "B"

    async def test_asr_falls_back_to_native_prompt_when_custom_dir_has_no_template(self, tmp_path):
        config = SimpleNamespace(paths=PathsConfig(prompts_dir=tmp_path), prompts=PromptsConfig())
        svc = PromptService(prompt_repo=FakePromptRepo(), config=config)

        assert await svc.resolve_prompt(PromptType.ASR_TRANSCRIPTION.value) == ""


class TestCrud:
    async def test_create_validates_type(self):
        with pytest.raises(ValidationError):
            await _service().create_prompt(prompt_type="not_a_type", name="x", content="y")

    async def test_asr_allows_blank_content_to_select_the_model_prompt(self):
        svc = _service()

        created = await svc.create_prompt(
            prompt_type=PromptType.ASR_TRANSCRIPTION.value,
            name="native-model-prompt",
            content="",
            is_default=True,
        )

        assert created.content == ""
        assert await svc.resolve_prompt(PromptType.ASR_TRANSCRIPTION.value) == ""

    async def test_asr_whitespace_content_is_stored_as_the_native_prompt_choice(self):
        repo = FakePromptRepo()
        svc = _service(repo)

        created = await svc.create_prompt(
            prompt_type=PromptType.ASR_TRANSCRIPTION.value,
            name="native-model-prompt",
            content=" \n\t ",
            is_default=True,
        )

        assert created.content == ""
        assert (await repo.get(created.id)).content == ""

    async def test_updating_asr_to_whitespace_selects_the_native_prompt(self):
        repo = FakePromptRepo()
        svc = _service(repo)
        prompt = await svc.create_prompt(
            prompt_type=PromptType.ASR_TRANSCRIPTION.value,
            name="custom-instruction",
            content="Keep speaker labels.",
        )

        updated = await svc.update_prompt(prompt.id, content=" \n\t ")

        assert updated.content == ""
        assert (await repo.get(prompt.id)).content == ""

    async def test_non_asr_rejects_blank_content(self):
        with pytest.raises(ValidationError) as exc:
            await _service().create_prompt(prompt_type="sys_prompt", name="blank", content="   ")
        assert exc.value.status_code == 422

    async def test_get_missing_raises(self):
        with pytest.raises(NotFoundError):
            await _service().get_prompt("nope")

    async def test_create_duplicate_name_per_type_is_rejected(self):
        repo = FakePromptRepo()
        svc = _service(repo)
        await svc.create_prompt(prompt_type="sys_prompt", name="formal", content="a")
        with pytest.raises(ValidationError):
            await svc.create_prompt(prompt_type="sys_prompt", name="formal", content="b")
        # Same name under a different type is fine.
        await svc.create_prompt(prompt_type="hyde", name="formal", content="c")

    async def test_rename_collision_is_rejected(self):
        repo = FakePromptRepo()
        svc = _service(repo)
        await svc.create_prompt(prompt_type="sys_prompt", name="a", content="a")
        b = await svc.create_prompt(prompt_type="sys_prompt", name="b", content="b")
        with pytest.raises(ValidationError):
            await svc.update_prompt(b.id, name="a")

    async def test_create_accepts_valid_template_placeholders(self):
        svc = _service()
        # sys_prompt allows {context} and {current_date}; escaped braces are literal.
        p = await svc.create_prompt(
            prompt_type="sys_prompt", name="ok", content="Use {context} on {current_date}. Literal {{brace}}."
        )
        assert p.id

    async def test_create_rejects_unknown_placeholder(self):
        svc = _service()
        with pytest.raises(ValidationError) as exc:
            await svc.create_prompt(prompt_type="sys_prompt", name="bad", content="Answer about {topic}")
        assert exc.value.status_code == 422

    async def test_create_rejects_malformed_braces(self):
        svc = _service()
        # A stray single brace (e.g. a JSON/code example) would crash str.format at runtime.
        with pytest.raises(ValidationError):
            await svc.create_prompt(prompt_type="sys_prompt", name="bad", content='return {"a": 1}')

    @pytest.mark.parametrize(
        "content",
        [
            "{context!x} on {current_date}",  # ValueError: unknown conversion
            "{context.missing} on {current_date}",  # AttributeError at render time
            "{context[0]} on {current_date}",  # renders, but not a supported form
            "{context:>10} on {current_date}",  # format spec
        ],
    )
    async def test_create_rejects_placeholders_str_format_cannot_render(self, content):
        """A field reduced to its root name looked valid while `.format()` still
        raised — and as a type's global default that fails every request that
        falls back to it. Only plain placeholders are accepted.
        """
        svc = _service()
        with pytest.raises(ValidationError) as exc:
            await svc.create_prompt(prompt_type="sys_prompt", name="bad", content=content)
        assert exc.value.status_code == 422

    async def test_bundled_templates_all_pass_validation(self):
        """Guards the stricter rule against the seed path: a bundled template that
        failed validation would be skipped at boot, leaving the type with no
        default at all.
        """
        from core.prompts.template_loader import load_template_by_key
        from services.orchestrators.prompt_service import _TYPE_TO_CONFIG_KEY, _validate_template

        svc = _service()
        for prompt_type, config_key in _TYPE_TO_CONFIG_KEY.items():
            content = load_template_by_key(svc._config.paths.prompts_dir, svc._config.prompts, config_key)
            _validate_template(prompt_type, content)

    async def test_verbatim_type_allows_any_braces(self):
        svc = _service()
        # chunk_contextualizer is sent as-is (never str.format-ed), so literal braces are fine.
        p = await svc.create_prompt(
            prompt_type="chunk_contextualizer", name="ok", content='Emit JSON like {"topic": "x"} with {anything}'
        )
        assert p.id

    async def test_update_rejects_bad_template(self):
        repo = FakePromptRepo()
        svc = _service(repo)
        p = await svc.create_prompt(prompt_type="hyde", name="h", content="Hypothetical doc for {question}")
        with pytest.raises(ValidationError):
            await svc.update_prompt(p.id, content="now with {unknown_var}")

    async def test_update_promotes_default_via_set_default(self):
        repo = FakePromptRepo()
        svc = _service(repo)
        a = await repo.create(Prompt(prompt_type="sys_prompt", name="a", content="a", is_default=True))
        b = await repo.create(Prompt(prompt_type="sys_prompt", name="b", content="b"))
        updated = await svc.update_prompt(b.id, content="b2", is_default=True)
        assert updated.is_default is True and updated.content == "b2"
        assert (await repo.get(a.id)).is_default is False  # single-default invariant

    async def test_delete_default_is_rejected(self):
        repo = FakePromptRepo()
        svc = _service(repo)
        d = await repo.create(Prompt(prompt_type="sys_prompt", content="c", is_default=True))
        with pytest.raises(ValidationError):
            await svc.delete_prompt(d.id)

    async def test_delete_non_default_ok(self):
        repo = FakePromptRepo()
        svc = _service(repo)
        p = await repo.create(Prompt(prompt_type="sys_prompt", content="c"))
        await svc.delete_prompt(p.id)
        assert await repo.get(p.id) is None

    async def test_set_default_missing_raises(self):
        with pytest.raises(NotFoundError):
            await _service().set_default("nope")

    async def test_list_filters_and_annotates_used_by(self):
        repo = FakePromptRepo()
        svc = _service(repo)
        p = await repo.create(Prompt(prompt_type="hyde", name="b", content="c"))
        repo._ref_counts = {("hyde", "b"): 3}
        listed = await svc.list_prompts(prompt_type="hyde")
        assert [row["prompt_type"] for row in listed] == ["hyde"]
        assert listed[0]["used_by"] == 3
        assert p.id == listed[0]["id"]


class TestLoggingIsBraceSafe:
    def test_a_brace_in_a_prompt_name_does_not_raise(self):
        """Prompt names are free text. Interpolating one into the log format
        string made a brace a format field, so a partition pointed at a prompt
        named `my{tmpl}` raised KeyError on *every* request that resolved it.
        """
        from services.orchestrators.prompt_service import PromptService

        records: list[str] = []
        sink_id = logger.add(records.append, level="DEBUG", format="{message}")
        try:
            PromptService._log_resolution("sys_prompt", ["my{tmpl}"], "named", "my{tmpl}", "body {with} braces")
        finally:
            logger.remove(sink_id)
        assert "my{tmpl}" in "".join(records)


class TestResolveSurvivesRepositoryFailure:
    async def test_a_repo_error_degrades_to_the_disk_seed(self):
        """Resolution moved onto the request path, so chat and search now depend
        on Postgres per request where they used to read prompts once at boot. A
        transient pool error must degrade to the bundled template, not 500.
        """

        class ExplodingRepo(FakePromptRepo):
            async def get_by_name(self, prompt_type, name):
                raise RuntimeError("connection pool exhausted")

            async def get_default(self, prompt_type):
                raise RuntimeError("connection pool exhausted")

        svc = _service(ExplodingRepo())
        content = await svc.resolve_prompt("sys_prompt", names=["whatever"])
        assert "{context}" in content  # the bundled sys_prompt template


class TestSeedingSurvivesAConcurrentReplica:
    async def test_a_lost_seed_race_does_not_fail_boot(self):
        """Losing the race is a no-op, but the unique violation maps to a
        ValidationError that _initialize_step re-raises — so an unhandled one
        turns a concurrent boot into a crash-loop instead of a skipped insert.
        """

        class RacingRepo(FakePromptRepo):
            async def create(self, prompt):
                raise ValidationError("already exists", status_code=409, code="PROMPT_EXISTS")

        await _service(RacingRepo()).seed_defaults()  # must not raise


class TestUnavailablePromptRaisesTheTypedError:
    async def test_missing_default_and_missing_template_raises_configerror(self, tmp_path):
        """Exercises the raise itself. ConfigError hard-coded its own code, so
        passing code= collided with the forwarded kwargs and the statement threw
        TypeError instead — the typed error could never be constructed.
        """
        # Point the loader at an empty directory — no bundled template, and the
        # fake repo has no default, which is the only path reaching the raise.
        svc = _service()
        svc._config = SimpleNamespace(
            paths=PathsConfig(prompts_dir=tmp_path),
            prompts=PromptsConfig(),
        )

        with pytest.raises(ConfigError) as exc:
            await svc.resolve_prompt("hyde")

        assert exc.value.code == "PROMPT_UNAVAILABLE"
        assert exc.value.status_code == 500
        assert "hyde" in str(exc.value)


class TestErrorPathsAreExercised:
    """Every raise in the service reached at least once.

    These paths were reasoned about rather than run, which is how a raise that
    itself threw TypeError survived review — coverage over the error branches is
    the check that catches that class of defect.
    """

    async def test_malformed_braces_raise_at_write_time(self):
        svc = _service()
        with pytest.raises(ValidationError) as exc:
            await svc.create_prompt(prompt_type="hyde", name="bad", content="unbalanced {question")
        assert exc.value.status_code == 422
        assert "brace" in str(exc.value).lower()

    @pytest.mark.parametrize("op", ["get", "update", "set_default", "delete"])
    async def test_unknown_id_raises_not_found(self, op):
        svc = _service()
        with pytest.raises(NotFoundError):
            if op == "get":
                await svc.get_prompt("nope")
            elif op == "update":
                await svc.update_prompt("nope", name="x")
            elif op == "set_default":
                await svc.set_default("nope")
            else:
                await svc.delete_prompt("nope")

    async def test_deleting_a_types_default_is_refused(self):
        repo = FakePromptRepo()
        svc = _service(repo)
        p = await svc.create_prompt(prompt_type="hyde", name="d", content="{question}", is_default=True)
        with pytest.raises(ValidationError):
            await svc.delete_prompt(p.id)

    async def test_seeding_keeps_the_native_asr_default_when_custom_templates_are_missing(self, tmp_path):
        repo = FakePromptRepo()
        svc = _service(repo)
        svc._config = SimpleNamespace(paths=PathsConfig(prompts_dir=tmp_path), prompts=PromptsConfig())
        await svc.seed_defaults()  # warns per type, never raises
        assert [(p.prompt_type, p.content, p.is_default) for p in repo.prompts.values()] == [
            (PromptType.ASR_TRANSCRIPTION.value, "", True)
        ]
