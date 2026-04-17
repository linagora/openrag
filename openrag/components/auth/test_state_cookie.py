"""Unit tests for state_cookie.py."""

import time

import pytest

from components.auth.state_cookie import StateCookiePayload, StateCookieSerializer


SECRET = "test-secret-key-for-state-cookie"


def _serializer() -> StateCookieSerializer:
    return StateCookieSerializer(SECRET)


def _payload() -> StateCookiePayload:
    return StateCookiePayload(
        state="abc123",
        nonce="xyz789",
        code_verifier="verifier_value",
        next_url="/dashboard",
    )


class TestRoundTrip:
    def test_dumps_loads_roundtrip(self):
        ser = _serializer()
        p = _payload()
        token = ser.dumps(p)
        result = ser.loads(token)
        assert result.state == p.state
        assert result.nonce == p.nonce
        assert result.code_verifier == p.code_verifier
        assert result.next_url == p.next_url

    def test_default_next_url(self):
        ser = _serializer()
        p = StateCookiePayload(state="s", nonce="n", code_verifier="v")
        token = ser.dumps(p)
        result = ser.loads(token)
        assert result.next_url == "/"


class TestTampering:
    def test_tampered_cookie_raises_value_error(self):
        ser = _serializer()
        token = ser.dumps(_payload())
        # Flip a character near the end of the token
        tampered = token[:-4] + "XXXX"
        with pytest.raises(ValueError, match="signature invalid"):
            ser.loads(tampered)

    def test_different_secret_raises_value_error(self):
        ser1 = _serializer()
        ser2 = StateCookieSerializer("different-secret")
        token = ser1.dumps(_payload())
        with pytest.raises(ValueError, match="signature invalid"):
            ser2.loads(token)


class TestExpiry:
    def test_expired_cookie_raises_value_error(self):
        ser = _serializer()
        token = ser.dumps(_payload())
        # Use max_age=0: any token older than 0 seconds is expired
        time.sleep(1)
        with pytest.raises(ValueError, match="expired"):
            ser.loads(token, max_age=0)
