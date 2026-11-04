"""A token pasted into ``token_env`` never reaches a log line or an error.

``token_env`` only has to look like an env var name, and plenty of tokens do
(``sk_live_...``, hex, base62). Seeding therefore names an entry by its
``external_user_id``, never by its ``token_env``.
"""

from __future__ import annotations

import json

import pytest
from core.config import load_config
from core.config.auth import AuthConfig, SeedUserConfig
from core.utils.logging import json_record
from loguru import logger
from pydantic import ValidationError
from services.persistence.user_repo import PgUserRepository, _resolve_seed_tokens

# Shaped like real API keys, and valid env var names. Assembled at run time so
# secret scanners do not take the fixtures for leaked credentials.
LEAKED = "_".join(("sk", "live", "4eC39HqLyjWDarjtT1zdp7dcQ9xZ"))
OTHER_LEAKED = "_".join(("ghp", "R2d2C3poX9Lk7Mn4Bv6Qw8Er1Ty5Ui0OpAs"))
GOOD_TOKEN = "a-good-token-0123456789abcdef"


@pytest.fixture
def logs():
    """Both renderings of every record: the terminal text and the JSON line."""
    text: list[str] = []
    payloads: list[str] = []
    text_sink = logger.add(text.append, level="DEBUG", format="{message} | {extra}", colorize=False)
    json_sink = logger.add(
        lambda message: payloads.append(json.dumps(json_record(message.record), default=str)), level="DEBUG"
    )
    yield text, payloads
    logger.remove(text_sink)
    logger.remove(json_sink)


def _assert_clean(logs, *secrets: str) -> None:
    text, payloads = logs
    assert text and payloads, "the scenario was expected to log"
    for secret in secrets:
        assert not any(secret in line for line in text)
        assert not any(secret in line for line in payloads)


def _seed(external_user_id: str = "svc-a", token_env: str = LEAKED, **overrides) -> SeedUserConfig:
    return SeedUserConfig(external_user_id=external_user_id, token_env=token_env, **overrides)


class TestValidationErrors:
    def test_a_token_shaped_value_is_a_valid_name(self):
        """Why the name has to stay out of logs: the pattern cannot catch it."""
        assert _seed().token_env == LEAKED

    def test_duplicate_token_env_names_the_entries_not_the_value(self):
        with pytest.raises(ValidationError) as excinfo:
            AuthConfig(seed_users=[_seed("svc-a"), _seed("svc-b")])
        message = excinfo.value.errors()[0]["msg"]
        assert "same token_env" in message
        assert "'svc-a'" in message and "'svc-b'" in message
        assert LEAKED not in message

    def test_duplicate_token_env_stays_out_of_the_boot_error(self, monkeypatch, tmp_path):
        """What the boot log shows: the error ``load_config`` raises, inputs included."""
        (tmp_path / "config.yaml").write_text(
            "retriever:\n  type: single\nreranker:\n  provider: infinity\nwebsearch:\n  provider: staan\n",
            encoding="utf-8",
        )
        entries = [{"external_user_id": name, "token_env": LEAKED} for name in ("svc-a", "svc-b")]
        monkeypatch.setenv("SEED_USERS", json.dumps(entries))
        with pytest.raises(ValidationError) as excinfo:
            load_config(config_path=tmp_path)
        assert "same token_env" in str(excinfo.value)
        assert LEAKED not in str(excinfo.value)
        assert LEAKED not in repr(excinfo.value.errors())
        assert excinfo.value.__cause__ is None and excinfo.value.__context__ is None

    def test_duplicate_external_user_id_does_not_quote_token_env(self):
        with pytest.raises(ValidationError) as excinfo:
            AuthConfig(seed_users=[_seed("svc-a", LEAKED), _seed("svc-a", OTHER_LEAKED)])
        message = excinfo.value.errors()[0]["msg"]
        assert "same external_user_id" in message
        assert LEAKED not in message and OTHER_LEAKED not in message


class TestResolveSeedTokens:
    def test_a_weak_token_is_refused_without_naming_the_env_var(self, logs):
        _, refused = _resolve_seed_tokens([_seed()], {LEAKED: "short"})
        assert refused == {"svc-a"}
        _assert_clean(logs, LEAKED)
        assert any("svc-a" in line for line in logs[1])

    def test_an_allowed_weak_token_warns_without_naming_the_env_var(self, logs):
        tokens, _ = _resolve_seed_tokens([_seed()], {LEAKED: "short", "ALLOW_INSECURE_SECRETS": "true"})
        assert tokens == {"svc-a": "short"}
        _assert_clean(logs, LEAKED)

    def test_the_admin_token_is_refused_without_naming_the_env_var(self, logs):
        _, refused = _resolve_seed_tokens([_seed()], {LEAKED: GOOD_TOKEN, "AUTH_TOKEN": GOOD_TOKEN})
        assert refused == {"svc-a"}
        _assert_clean(logs, LEAKED, GOOD_TOKEN)

    def test_a_shared_token_is_refused_without_naming_the_env_vars(self, logs):
        seeds = [_seed("svc-a", LEAKED), _seed("svc-b", OTHER_LEAKED)]
        _, refused = _resolve_seed_tokens(seeds, {LEAKED: GOOD_TOKEN, OTHER_LEAKED: GOOD_TOKEN})
        assert refused == {"svc-a", "svc-b"}
        _assert_clean(logs, LEAKED, OTHER_LEAKED, GOOD_TOKEN)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """Answers the queries ``_apply_seed_user`` sends, from a small script."""

    def __init__(self, existing=None, token_holder=None, user_id=7, partition_role="editor"):
        self.existing = existing
        self.token_holder = token_holder
        self.user_id = user_id
        self.partition_role = partition_role

    def transaction(self):
        return _Transaction()

    async def fetchrow(self, query, *params):
        return self.existing

    async def fetchval(self, query, *params):
        if "WHERE token = $1" in query:
            return self.token_holder
        if "INSERT INTO partition_memberships" in query:
            return self.partition_role
        return self.user_id


def _repo() -> PgUserRepository:
    return PgUserRepository(lambda: None)


class TestApplySeedUser:
    async def test_an_unmanaged_account_is_skipped_without_naming_the_env_var(self, logs):
        conn = _FakeConn(existing={"id": 5, "managed_by_config": False})
        assert await _repo()._apply_seed_user(conn, _seed(), GOOD_TOKEN, False, set()) == "skipped"
        _assert_clean(logs, LEAKED, GOOD_TOKEN)

    async def test_a_token_held_elsewhere_is_refused_without_naming_the_env_var(self, logs):
        conn = _FakeConn(token_holder=9)
        assert await _repo()._apply_seed_user(conn, _seed(), GOOD_TOKEN, False, set()) == "skipped"
        _assert_clean(logs, LEAKED, GOOD_TOKEN)

    async def test_an_unset_env_var_is_reported_without_naming_it(self, logs):
        assert await _repo()._apply_seed_user(_FakeConn(), _seed(), None, False, set()) == "skipped"
        _assert_clean(logs, LEAKED)
        assert any("unset" in line for line in logs[0])

    async def test_admin_and_membership_warnings_do_not_name_the_env_var(self, logs):
        seed = _seed(is_admin=True, partitions=[{"name": "missing", "role": "editor"}])
        assert await _repo()._apply_seed_user(_FakeConn(), seed, GOOD_TOKEN, False, set()) == "created"
        _assert_clean(logs, LEAKED, GOOD_TOKEN)
        assert any("admin rights" in line for line in logs[0])
        assert any("does not exist" in line for line in logs[0])
