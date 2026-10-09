"""``auth.seed_users``: the operator-managed accounts provisioned at startup."""

from __future__ import annotations

import pytest
from core.config import load_config
from core.config.auth import AuthConfig, SeedUserConfig
from core.config.root import Settings
from core.models.user import PartitionRole
from pydantic import ValidationError


def _entry(**overrides) -> dict:
    entry = {
        "external_user_id": "svc-cozy-stack",
        "display_name": "cozy-stack",
        "token_env": "COZY_STACK_TOKEN",
        "partitions": [{"name": "twake", "role": "editor"}],
    }
    entry.update(overrides)
    return entry


def _write_minimal_config(tmp_path, extra: str = ""):
    # The discriminated unions need their tag even when a developer .env sets
    # RERANKER_* / WEBSEARCH_* without a provider (load_config reads .env).
    (tmp_path / "config.yaml").write_text(
        "retriever:\n  type: single\nreranker:\n  provider: infinity\nwebsearch:\n  provider: staan\n" + extra,
        encoding="utf-8",
    )


class TestSeedUserConfig:
    def test_defaults(self):
        seed = SeedUserConfig(external_user_id="svc-a", token_env="SVC_A_TOKEN")
        assert seed.is_admin is False
        assert seed.partitions == []
        # No display name: the stable key doubles as one.
        assert seed.display_name == "svc-a"

    def test_role_is_parsed_into_partition_role(self):
        seed = SeedUserConfig(**_entry())
        assert seed.partitions[0].role is PartitionRole.EDITOR

    def test_unknown_role_is_rejected(self):
        with pytest.raises(ValidationError):
            SeedUserConfig(**_entry(partitions=[{"name": "twake", "role": "admin"}]))

    def test_same_partition_twice_is_rejected(self):
        with pytest.raises(ValidationError, match="twake"):
            SeedUserConfig(
                **_entry(partitions=[{"name": "twake", "role": "editor"}, {"name": "twake", "role": "viewer"}])
            )

    def test_unknown_key_is_rejected(self):
        """A plaintext ``token:`` key is the likeliest typo, and it must never be
        silently ignored while the secret sits in a versioned file."""
        with pytest.raises(ValidationError):
            SeedUserConfig(**_entry(token="or-plaintext-in-the-config"))

    @pytest.mark.parametrize("name", ["", "1TOKEN", "COZY-TOKEN", "A B"])
    def test_token_env_must_be_an_env_var_name(self, name):
        with pytest.raises(ValidationError):
            SeedUserConfig(**_entry(token_env=name))

    def test_token_env_cannot_be_the_admin_token(self):
        with pytest.raises(ValidationError, match="AUTH_TOKEN"):
            SeedUserConfig(**_entry(token_env="AUTH_TOKEN"))

    def test_external_user_id_is_required_and_non_blank(self):
        with pytest.raises(ValidationError):
            SeedUserConfig(**_entry(external_user_id="  "))


class TestAuthConfig:
    def test_defaults_to_no_seed_users(self):
        assert AuthConfig().seed_users == []
        assert Settings().auth.seed_users == []

    def test_duplicate_external_user_id_is_rejected(self):
        with pytest.raises(ValidationError, match="svc-cozy-stack"):
            AuthConfig(seed_users=[_entry(), _entry(token_env="OTHER_TOKEN")])

    def test_duplicate_token_env_is_rejected(self):
        with pytest.raises(ValidationError, match="COZY_STACK_TOKEN"):
            AuthConfig(seed_users=[_entry(), _entry(external_user_id="svc-other")])


class TestLoading:
    def test_read_from_config_yaml(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SEED_USERS", raising=False)
        _write_minimal_config(
            tmp_path,
            "auth:\n  seed_users:\n    - external_user_id: svc-a\n      token_env: SVC_A_TOKEN\n",
        )
        settings = load_config(config_path=tmp_path)
        assert [s.external_user_id for s in settings.auth.seed_users] == ["svc-a"]

    def test_seed_users_env_replaces_the_config_list(self, monkeypatch, tmp_path):
        _write_minimal_config(
            tmp_path,
            "auth:\n  seed_users:\n    - external_user_id: svc-a\n      token_env: SVC_A_TOKEN\n",
        )
        monkeypatch.setenv(
            "SEED_USERS",
            '[{"external_user_id": "svc-b", "token_env": "SVC_B_TOKEN",'
            ' "partitions": [{"name": "twake", "role": "viewer"}]}]',
        )
        settings = load_config(config_path=tmp_path)
        assert [s.external_user_id for s in settings.auth.seed_users] == ["svc-b"]
        assert settings.auth.seed_users[0].partitions[0].role is PartitionRole.VIEWER

    def test_seed_users_env_accepts_yaml(self, monkeypatch, tmp_path):
        _write_minimal_config(tmp_path)
        monkeypatch.setenv("SEED_USERS", "- external_user_id: svc-c\n  token_env: SVC_C_TOKEN\n")
        settings = load_config(config_path=tmp_path)
        assert [s.external_user_id for s in settings.auth.seed_users] == ["svc-c"]

    def test_blank_seed_users_env_means_unset(self, monkeypatch, tmp_path):
        _write_minimal_config(
            tmp_path,
            "auth:\n  seed_users:\n    - external_user_id: svc-a\n      token_env: SVC_A_TOKEN\n",
        )
        monkeypatch.setenv("SEED_USERS", "  ")
        settings = load_config(config_path=tmp_path)
        assert [s.external_user_id for s in settings.auth.seed_users] == ["svc-a"]

    @pytest.mark.parametrize("raw", ['{"external_user_id": "svc"}', "[unclosed", '"text"'])
    def test_malformed_seed_users_env_fails_loudly(self, monkeypatch, tmp_path, raw):
        _write_minimal_config(tmp_path)
        monkeypatch.setenv("SEED_USERS", raw)
        with pytest.raises(ValueError, match="SEED_USERS"):
            load_config(config_path=tmp_path)
