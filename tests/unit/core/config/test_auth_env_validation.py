"""Tests for :meth:`core.config.auth.OIDCConfig.from_env`.

The validator is what the legacy ``openrag/main.py`` did inline at
module load — the move keeps the same fail-fast rules but exercises
them through :class:`ServiceContainer.__init__`. These tests pin the
rules so a regression in the parser surfaces at unit-test time, not on
an operator's box.
"""

from __future__ import annotations

import pytest
from core.config.auth import OIDCConfig

_REQUIRED_ENV = {
    "AUTH_MODE": "oidc",
    "OIDC_ENDPOINT": "https://idp.example.com/realms/openrag",
    "OIDC_CLIENT_ID": "openrag",
    "OIDC_CLIENT_SECRET": "shh",
    "OIDC_REDIRECT_URI": "https://openrag.example.com/auth/callback",
    "OIDC_TOKEN_ENCRYPTION_KEY": "fernet-key-base64-32-bytes",
}


@pytest.fixture(autouse=True)
def _clear_oidc_env(monkeypatch):
    """Strip the test env of any AUTH_*/OIDC_* leakage from the
    surrounding shell so each test gets a clean slate."""
    for name in list(_REQUIRED_ENV) + [
        "OIDC_CLAIM_SOURCE",
        "OIDC_CLAIM_MAPPING",
        "OIDC_SCOPES",
        "OIDC_POST_LOGOUT_REDIRECT_URI",
        "OIDC_AUTO_PROVISION_LOGIN",
    ]:
        monkeypatch.delenv(name, raising=False)


def _set_env(monkeypatch, **overrides) -> None:
    """Set every required var, then apply overrides. Pass a value of
    ``None`` to unset a key."""
    full = {**_REQUIRED_ENV, **overrides}
    for name, value in full.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_token_mode_returns_disabled_config(monkeypatch):
    """AUTH_MODE=token: enabled=False; no required vars consulted."""
    monkeypatch.setenv("AUTH_MODE", "token")
    cfg = OIDCConfig.from_env()
    assert cfg.enabled is False
    assert cfg.issuer_url == ""
    assert cfg.claim_source == "id_token"


def test_default_mode_is_token(monkeypatch):
    """Unset AUTH_MODE defaults to token — the legacy behaviour."""
    cfg = OIDCConfig.from_env()
    assert cfg.enabled is False


def test_oidc_mode_with_all_required_env_set(monkeypatch):
    _set_env(monkeypatch)
    cfg = OIDCConfig.from_env()
    assert cfg.enabled is True
    assert cfg.issuer_url == _REQUIRED_ENV["OIDC_ENDPOINT"]
    assert cfg.client_id == _REQUIRED_ENV["OIDC_CLIENT_ID"]
    assert cfg.claim_source == "id_token"
    assert cfg.claim_mapping == ""


def test_optional_post_logout_redirect_uri_round_trips(monkeypatch):
    _set_env(monkeypatch, OIDC_POST_LOGOUT_REDIRECT_URI="https://example.com/bye")
    cfg = OIDCConfig.from_env()
    assert cfg.post_logout_redirect_uri == "https://example.com/bye"


def test_auto_provision_login_is_truthy_only_for_literal_true(monkeypatch):
    _set_env(monkeypatch, OIDC_AUTO_PROVISION_LOGIN="True")
    assert OIDCConfig.from_env().auto_provision_login is True
    _set_env(monkeypatch, OIDC_AUTO_PROVISION_LOGIN="yes")
    assert OIDCConfig.from_env().auto_provision_login is False


# ---------------------------------------------------------------------------
# AUTH_MODE gate
# ---------------------------------------------------------------------------


def test_invalid_auth_mode_raises(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "saml")
    with pytest.raises(RuntimeError, match="Invalid AUTH_MODE"):
        OIDCConfig.from_env()


def test_auth_mode_is_case_and_whitespace_tolerant(monkeypatch):
    """Operators sometimes leave a trailing space in .env files."""
    _set_env(monkeypatch, AUTH_MODE="  OIDC  ")
    cfg = OIDCConfig.from_env()
    assert cfg.enabled is True


# ---------------------------------------------------------------------------
# Required-vars-when-enabled
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", list(_REQUIRED_ENV)[1:])  # skip AUTH_MODE
def test_oidc_mode_with_any_required_var_missing_raises(monkeypatch, missing):
    _set_env(monkeypatch, **{missing: None})
    with pytest.raises(RuntimeError) as excinfo:
        OIDCConfig.from_env()
    assert missing in str(excinfo.value)


def test_missing_vars_listed_together_in_single_message(monkeypatch):
    """All missing keys appear in one error so an operator does not have
    to fix one, re-run, fix the next."""
    _set_env(monkeypatch, OIDC_CLIENT_ID=None, OIDC_CLIENT_SECRET=None)
    with pytest.raises(RuntimeError) as excinfo:
        OIDCConfig.from_env()
    message = str(excinfo.value)
    assert "OIDC_CLIENT_ID" in message
    assert "OIDC_CLIENT_SECRET" in message


def test_empty_string_counts_as_missing(monkeypatch):
    """Operators sometimes set ``OIDC_ENDPOINT=`` with no value; the
    validator treats that the same as unset."""
    _set_env(monkeypatch, OIDC_ENDPOINT="")
    with pytest.raises(RuntimeError, match="OIDC_ENDPOINT"):
        OIDCConfig.from_env()


# ---------------------------------------------------------------------------
# claim_source
# ---------------------------------------------------------------------------


def test_invalid_claim_source_raises(monkeypatch):
    _set_env(monkeypatch, OIDC_CLAIM_SOURCE="cookies")
    with pytest.raises(RuntimeError, match="OIDC_CLAIM_SOURCE"):
        OIDCConfig.from_env()


def test_userinfo_claim_source_accepted(monkeypatch):
    _set_env(monkeypatch, OIDC_CLAIM_SOURCE="userinfo")
    cfg = OIDCConfig.from_env()
    assert cfg.claim_source == "userinfo"


# ---------------------------------------------------------------------------
# claim_mapping
# ---------------------------------------------------------------------------


def test_well_formed_claim_mapping_is_accepted(monkeypatch):
    _set_env(monkeypatch, OIDC_CLAIM_MAPPING="display_name:name, email:mail")
    cfg = OIDCConfig.from_env()
    assert cfg.claim_mapping == "display_name:name, email:mail"


def test_claim_mapping_without_colon_raises(monkeypatch):
    _set_env(monkeypatch, OIDC_CLAIM_MAPPING="display_name")
    with pytest.raises(RuntimeError, match="expected 'db_field:claim'"):
        OIDCConfig.from_env()


def test_claim_mapping_non_whitelisted_field_raises(monkeypatch):
    """``is_admin`` is the canonical privilege-escalation target — the
    validator must reject any attempt to populate it via claims."""
    _set_env(monkeypatch, OIDC_CLAIM_MAPPING="is_admin:role")
    with pytest.raises(RuntimeError, match="is not writable"):
        OIDCConfig.from_env()


def test_claim_mapping_empty_claim_name_raises(monkeypatch):
    _set_env(monkeypatch, OIDC_CLAIM_MAPPING="email:")
    with pytest.raises(RuntimeError, match="empty claim name"):
        OIDCConfig.from_env()


def test_claim_mapping_validated_even_in_token_mode(monkeypatch):
    """Misconfigured mapping on an operator's box must fail at startup
    even when AUTH_MODE is currently ``token`` — otherwise flipping the
    mode flag would silently break logins."""
    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("OIDC_CLAIM_MAPPING", "is_admin:role")
    with pytest.raises(RuntimeError, match="is not writable"):
        OIDCConfig.from_env()
