"""Unit tests for session_tokens.py."""

import pytest
from cryptography.fernet import Fernet

from components.auth.session_tokens import (
    decrypt_token,
    encrypt_token,
    hash_session_token,
    issue_session_token,
)


def _valid_key() -> str:
    return Fernet.generate_key().decode()


class TestIssueSessionToken:
    def test_returns_tuple_of_two_strings(self):
        plain, hashed = issue_session_token()
        assert isinstance(plain, str)
        assert isinstance(hashed, str)

    def test_hash_is_64_hex_chars(self):
        _, hashed = issue_session_token()
        assert len(hashed) == 64
        assert all(c in "0123456789abcdef" for c in hashed)

    def test_tokens_are_unique(self):
        tokens = {issue_session_token()[0] for _ in range(20)}
        assert len(tokens) == 20

    def test_hash_matches_plain(self):
        plain, hashed = issue_session_token()
        assert hash_session_token(plain) == hashed


class TestHashSessionToken:
    def test_deterministic(self):
        assert hash_session_token("abc") == hash_session_token("abc")

    def test_different_inputs_differ(self):
        assert hash_session_token("abc") != hash_session_token("def")


class TestEncryptDecryptRoundTrip:
    def test_round_trip(self):
        key = _valid_key()
        plaintext = "super-secret-access-token"
        ciphertext = encrypt_token(plaintext, key)
        assert ciphertext is not None
        assert decrypt_token(ciphertext, key) == plaintext

    def test_none_plaintext_returns_none(self):
        assert encrypt_token(None, _valid_key()) is None

    def test_none_ciphertext_returns_none(self):
        assert decrypt_token(None, _valid_key()) is None

    def test_wrong_key_raises_value_error(self):
        key1 = _valid_key()
        key2 = _valid_key()
        ciphertext = encrypt_token("secret", key1)
        with pytest.raises(ValueError, match="decrypt"):
            decrypt_token(ciphertext, key2)

    def test_invalid_key_raises_value_error(self):
        with pytest.raises(ValueError, match="valid Fernet"):
            encrypt_token("data", "not-a-fernet-key")
