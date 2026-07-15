"""Regression test for #380 — app_front.py must raise on startup when
CHAINLIT_AUTH_SECRET is unset, instead of falling back to a hardcoded
default secret.
"""

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

_FIX_SOURCE = Path(__file__).resolve().parents[2] / "openrag" / "app_front.py"
_OPENRAG_RUNTIME_PATH = _FIX_SOURCE.parent


def _load_app_front(monkeypatch, *, auth_mode: str, module_name: str):
    monkeypatch.setenv("AUTH_TOKEN", "test-token")
    monkeypatch.setenv("AUTH_MODE", auth_mode)
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "x" * 32)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: None)
    monkeypatch.syspath_prepend(str(_OPENRAG_RUNTIME_PATH))

    spec = importlib.util.spec_from_file_location(module_name, _FIX_SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_hardcoded_default_secret_assignment_in_source():
    """The fall-through to a literal default secret must be gone.

    The bug was an assignment that substituted a hardcoded value when the
    env var was unset. We assert that assignment is not present in source.
    """
    with open(_FIX_SOURCE) as f:
        content = f.read()
    assert 'os.environ["CHAINLIT_AUTH_SECRET"] = "default_secret_for_openrag_ui"' not in content, (
        "The hardcoded chainlit-auth secret assignment must not be reintroduced"
    )


def test_openrag_bearer_token_is_not_stored_in_chainlit_user_metadata():
    with open(_FIX_SOURCE) as f:
        content = f.read()

    assert '"api_key": api_key' not in content
    assert '"api_key": password' not in content


def test_token_mode_keeps_chainlit_password_login(monkeypatch):
    """Token mode must keep Chainlit's manual token login form available."""
    from chainlit.config import config

    monkeypatch.setattr(config.code, "header_auth_callback", None)
    monkeypatch.setattr(config.code, "password_auth_callback", None)

    _load_app_front(monkeypatch, auth_mode="token", module_name="app_front_token_mode_test")

    assert config.code.header_auth_callback is None
    assert config.code.password_auth_callback is not None


def test_api_key_falls_back_to_chainlit_cookie_when_auth_handle_is_missing(monkeypatch):
    module = _load_app_front(monkeypatch, auth_mode="oidc", module_name="app_front_cookie_fallback_test")
    monkeypatch.setattr(module, "_openrag_api_key_from_context_cookie", lambda: "or-cookie-token")

    class UserSession:
        def __init__(self):
            self.values = {}

        def get(self, key):
            return self.values.get(key)

        def set(self, key, value):
            self.values[key] = value

    user_session = UserSession()
    module.cl = SimpleNamespace(user_session=user_session)

    api_key = module._openrag_api_key_from_user_or_context(SimpleNamespace(metadata={}))

    assert api_key == "or-cookie-token"
    assert user_session.values[module.OPENRAG_API_KEY_SESSION_KEY] == "or-cookie-token"


@pytest.mark.parametrize("stale_status", [401, 403])
@pytest.mark.asyncio
async def test_chainlit_cookie_auth_retries_handoff_after_stale_oidc_session(monkeypatch, stale_status):
    module = _load_app_front(monkeypatch, auth_mode="oidc", module_name="app_front_cookie_retry_test")
    attempts = []

    async def fake_load_user_info(_client, api_key):
        attempts.append(api_key)
        if api_key == "stale-session-token":
            request = httpx.Request("GET", "http://internal/users/info")
            response = httpx.Response(stale_status, request=request)
            raise httpx.HTTPStatusError("Unauthorized", request=request, response=response)
        return {
            "display_name": "Handoff User",
            "email": "handoff@example.test",
            "is_admin": False,
        }

    monkeypatch.setattr(module, "_load_user_info", fake_load_user_info)

    user = await module._chainlit_user_from_browser_cookies(
        {"cookie": (f"openrag_session=stale-session-token; {module.CHAINLIT_TOKEN_COOKIE_NAME}=handoff-token")}
    )

    assert attempts == ["stale-session-token", "handoff-token"]
    assert user.identifier == "Handoff User"
    assert user.metadata["provider"] == "credentials"
    auth_handle = user.metadata[module.OPENRAG_AUTH_HANDLE_METADATA_KEY]
    assert module._OPENRAG_TOKEN_STORE[auth_handle][0] == "handoff-token"


@pytest.mark.asyncio
async def test_oidc_token_handoff_keeps_bearer_on_static_source_urls(monkeypatch):
    module = _load_app_front(monkeypatch, auth_mode="oidc", module_name="app_front_source_token_test")
    module.INTERNAL_BASE_URL = "http://internal:8080"
    monkeypatch.setattr(module, "get_external_url", lambda: "https://openrag.example")

    class UserSession:
        def get(self, key):
            if key == "user":
                return SimpleNamespace(metadata={"provider": "credentials"})
            return None

    module.cl = SimpleNamespace(
        user_session=UserSession(),
        Pdf=lambda **kwargs: SimpleNamespace(**kwargs),
        Text=lambda **kwargs: SimpleNamespace(**kwargs),
        Image=lambda **kwargs: SimpleNamespace(**kwargs),
        Video=lambda **kwargs: SimpleNamespace(**kwargs),
        Audio=lambda **kwargs: SimpleNamespace(**kwargs),
    )

    elements, _ = await module._format_sources(
        [
            {
                "filename": "document.pdf",
                "file_url": "http://internal:8080/static/source-id",
                "page": "1",
            }
        ],
        api_key="or-user-token",
    )

    assert elements[0].url == "https://openrag.example/static/source-id?token=or-user-token"


@pytest.mark.asyncio
async def test_oidc_session_does_not_put_bearer_on_static_source_urls(monkeypatch):
    module = _load_app_front(monkeypatch, auth_mode="oidc", module_name="app_front_source_oidc_test")
    module.INTERNAL_BASE_URL = "http://internal:8080"
    monkeypatch.setattr(module, "get_external_url", lambda: "https://openrag.example")

    class UserSession:
        def get(self, key):
            if key == "user":
                return SimpleNamespace(metadata={"provider": "oidc"})
            return None

    module.cl = SimpleNamespace(
        user_session=UserSession(),
        Pdf=lambda **kwargs: SimpleNamespace(**kwargs),
        Text=lambda **kwargs: SimpleNamespace(**kwargs),
        Image=lambda **kwargs: SimpleNamespace(**kwargs),
        Video=lambda **kwargs: SimpleNamespace(**kwargs),
        Audio=lambda **kwargs: SimpleNamespace(**kwargs),
    )

    elements, _ = await module._format_sources(
        [
            {
                "filename": "document.pdf",
                "file_url": "http://internal:8080/static/source-id",
                "page": "1",
            }
        ],
        api_key="opaque-session-token",
    )

    assert elements[0].url == "https://openrag.example/static/source-id"


def test_app_front_raises_when_secret_missing(monkeypatch):
    """Importing app_front.py without CHAINLIT_AUTH_SECRET must raise."""
    monkeypatch.setenv("AUTH_TOKEN", "test-token")
    monkeypatch.delenv("CHAINLIT_AUTH_SECRET", raising=False)
    monkeypatch.setenv("AUTH_MODE", "token")
    # Prevent load_dotenv() inside app_front.py from re-loading CHAINLIT_AUTH_SECRET
    # from a .env file that may have it set (e.g. local dev environment).
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: None)

    # Drop the previously-imported app_front module so the import body re-runs.
    for mod in [m for m in sys.modules if m == "app_front" or m.endswith(".app_front")]:
        sys.modules.pop(mod, None)

    spec = importlib.util.spec_from_file_location("app_front_test", _FIX_SOURCE)
    module = importlib.util.module_from_spec(spec)
    with pytest.raises(RuntimeError, match="CHAINLIT_AUTH_SECRET"):
        spec.loader.exec_module(module)
