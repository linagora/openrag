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
from starlette.responses import Response

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
    """Token mode keeps manual login while allowing Admin UI handoff."""
    from chainlit.config import config

    monkeypatch.setattr(config.code, "header_auth_callback", None)
    monkeypatch.setattr(config.code, "password_auth_callback", None)

    _load_app_front(monkeypatch, auth_mode="token", module_name="app_front_token_mode_test")

    assert config.code.header_auth_callback is not None
    assert config.code.password_auth_callback is not None


@pytest.mark.asyncio
async def test_token_mode_header_auth_accepts_chainlit_handoff_cookie(monkeypatch):
    from chainlit.config import config

    monkeypatch.setattr(config.code, "header_auth_callback", None)
    monkeypatch.setattr(config.code, "password_auth_callback", None)
    module = _load_app_front(monkeypatch, auth_mode="token", module_name="app_front_token_handoff_test")

    async def fake_load_user_info(_client, api_key):
        assert api_key == "or-user-token"
        return {
            "display_name": "Token User",
            "email": "token@example.test",
            "is_admin": True,
        }

    async def fake_load_model_ids(_client, api_key):
        assert api_key == "or-user-token"
        return ["openrag-default", "openrag-all"]

    monkeypatch.setattr(module, "_load_user_info", fake_load_user_info)
    monkeypatch.setattr(module, "_load_openrag_model_ids_for_metadata", fake_load_model_ids)

    user = await config.code.header_auth_callback({"cookie": f"{module.CHAINLIT_TOKEN_COOKIE_NAME}=or-user-token"})

    assert user.identifier == "Token User"
    assert user.metadata["provider"] == "credentials"
    assert user.metadata["role"] == "admin"
    assert user.metadata[module.OPENRAG_CHAT_PROFILES_METADATA_KEY] == ["openrag-default", "openrag-all"]
    auth_handle = user.metadata[module.OPENRAG_AUTH_HANDLE_METADATA_KEY]
    assert module._OPENRAG_TOKEN_STORE[auth_handle][0] == "or-user-token"


def test_api_key_falls_back_to_chainlit_cookie_when_auth_handle_is_missing(monkeypatch):
    module = _load_app_front(monkeypatch, auth_mode="oidc", module_name="app_front_cookie_fallback_test")
    monkeypatch.setattr(module, "_openrag_api_key_from_context_cookie", lambda **_kwargs: "or-cookie-token")

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


def test_api_key_requires_reauth_when_user_token_cannot_be_recovered(monkeypatch):
    module = _load_app_front(monkeypatch, auth_mode="token", module_name="app_front_missing_token_test")
    monkeypatch.setattr(module, "_openrag_api_key_from_context_cookie", lambda **_kwargs: None)

    with pytest.raises(module.MissingOpenRAGCredentialError, match="sign in again"):
        module._openrag_api_key_from_user_or_context(
            SimpleNamespace(metadata={module.OPENRAG_AUTH_HANDLE_METADATA_KEY: "missing-handle"})
        )


def test_credentials_user_prefers_handoff_cookie_when_session_cookie_is_stale(monkeypatch):
    module = _load_app_front(monkeypatch, auth_mode="oidc", module_name="app_front_handoff_cookie_priority_test")
    user_session = SimpleNamespace(values={}, get=lambda key: None)
    user_session.set = lambda key, value: user_session.values.update({key: value})
    module.cl = SimpleNamespace(user_session=user_session)
    monkeypatch.setattr(
        module,
        "get_context",
        lambda: SimpleNamespace(
            session=SimpleNamespace(
                environ={
                    "HTTP_COOKIE": (
                        f"openrag_session=stale-session-token; {module.CHAINLIT_TOKEN_COOKIE_NAME}=fresh-handoff-token"
                    )
                }
            )
        ),
    )

    api_key = module._openrag_api_key_from_user_or_context(SimpleNamespace(metadata={"provider": "credentials"}))

    assert api_key == "fresh-handoff-token"


def test_credentials_user_does_not_recover_from_oidc_session_cookie(monkeypatch):
    module = _load_app_front(
        monkeypatch, auth_mode="oidc", module_name="app_front_credentials_no_session_fallback_test"
    )
    user_session = SimpleNamespace(values={}, get=lambda key: None)
    user_session.set = lambda key, value: user_session.values.update({key: value})
    module.cl = SimpleNamespace(user_session=user_session)
    monkeypatch.setattr(
        module,
        "get_context",
        lambda: SimpleNamespace(session=SimpleNamespace(environ={"HTTP_COOKIE": "openrag_session=oidc-session-token"})),
    )

    with pytest.raises(module.MissingOpenRAGCredentialError, match="sign in again"):
        module._openrag_api_key_from_user_or_context(SimpleNamespace(metadata={"provider": "credentials"}))

    assert module.OPENRAG_API_KEY_SESSION_KEY not in user_session.values


@pytest.mark.asyncio
async def test_chainlit_logout_clears_handoff_and_openrag_session_cookies(monkeypatch):
    module = _load_app_front(monkeypatch, auth_mode="token", module_name="app_front_logout_cookie_cleanup_test")
    request = SimpleNamespace(
        cookies={
            module.CHAINLIT_AUTH_COOKIE_NAME: "stale-chainlit-jwt",
            f"{module.CHAINLIT_AUTH_COOKIE_NAME}_0": "stale-chainlit-jwt-chunk",
            module.CHAINLIT_TOKEN_COOKIE_NAME: "or-user-token",
            module.OPENRAG_SESSION_COOKIE_NAME: "oidc-session-token",
        }
    )
    response = Response()

    returned = await module.on_logout(request, response)

    cookies = [value.decode() for key, value in response.raw_headers if key.lower() == b"set-cookie"]
    assert returned == {"success": True}
    assert any(f"{module.CHAINLIT_AUTH_COOKIE_NAME}=" in cookie and "Max-Age=0" in cookie for cookie in cookies)
    assert any(f"{module.CHAINLIT_AUTH_COOKIE_NAME}_0=" in cookie and "Max-Age=0" in cookie for cookie in cookies)
    assert any(f"{module.CHAINLIT_TOKEN_COOKIE_NAME}=" in cookie and "Max-Age=0" in cookie for cookie in cookies)
    assert any(f"{module.OPENRAG_SESSION_COOKIE_NAME}=" in cookie and "Max-Age=0" in cookie for cookie in cookies)


@pytest.mark.asyncio
async def test_chat_start_handles_expired_handoff_without_exception_log(monkeypatch):
    module = _load_app_front(monkeypatch, auth_mode="oidc", module_name="app_front_chat_start_expired_handoff_test")
    sent_messages = []
    log_calls = []

    class UserSession:
        def __init__(self):
            self.values = {}

        def get(self, key):
            return self.values.get(key)

        def set(self, key, value):
            self.values[key] = value

    class Message:
        def __init__(self, content):
            self.content = content

        async def send(self):
            sent_messages.append(self.content)

    logger = SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: log_calls.append(("warning", args, kwargs)),
        exception=lambda *args, **kwargs: log_calls.append(("exception", args, kwargs)),
    )
    module.logger = logger
    module.cl = SimpleNamespace(user_session=UserSession(), Message=Message)
    monkeypatch.setattr(
        module,
        "_current_openrag_api_key",
        lambda: (_ for _ in ()).throw(module.MissingOpenRAGCredentialError("expired handoff")),
    )

    await module.on_chat_start()

    assert sent_messages == ["expired handoff"]
    assert [call[0] for call in log_calls] == ["warning"]


def test_chainlit_user_metadata_keeps_chat_profiles_but_not_bearer(monkeypatch):
    module = _load_app_front(monkeypatch, auth_mode="token", module_name="app_front_profile_metadata_test")

    user = module._chainlit_user_from_info(
        {"display_name": "Token User", "is_admin": False},
        provider="credentials",
        api_key="or-user-token",
        model_ids=["openrag-default", "openrag-all"],
    )

    assert user.metadata[module.OPENRAG_CHAT_PROFILES_METADATA_KEY] == ["openrag-default", "openrag-all"]
    assert user.metadata["provider"] == "credentials"
    assert "api_key" not in user.metadata
    assert "or-user-token" not in str(user.metadata)


@pytest.mark.asyncio
async def test_chat_profiles_use_cached_model_ids_when_handoff_token_is_unavailable(monkeypatch):
    module = _load_app_front(monkeypatch, auth_mode="oidc", module_name="app_front_cached_profiles_test")
    sent_messages = []

    class Message:
        def __init__(self, content):
            self.content = content

        async def send(self):
            sent_messages.append(self.content)

    monkeypatch.setattr(module, "t", lambda key: "{name} ({partition})" if key.startswith("profile_") else key)
    module.cl = SimpleNamespace(Message=Message, ChatProfile=module.cl.ChatProfile)
    monkeypatch.setattr(
        module,
        "_openrag_api_key_from_user_or_context",
        lambda _user: (_ for _ in ()).throw(module.MissingOpenRAGCredentialError("expired handoff")),
    )

    profiles = await module.chat_profile(
        SimpleNamespace(
            metadata={
                "provider": "credentials",
                module.OPENRAG_CHAT_PROFILES_METADATA_KEY: ["openrag-default", "openrag-all"],
            }
        )
    )

    assert [profile.name for profile in profiles] == ["openrag-default", "openrag-all"]
    assert profiles[-1].default is True
    assert sent_messages == []


@pytest.mark.asyncio
async def test_chat_profiles_handle_expired_handoff_without_exception_log(monkeypatch):
    module = _load_app_front(monkeypatch, auth_mode="oidc", module_name="app_front_profiles_expired_handoff_test")
    sent_messages = []
    log_calls = []

    class Message:
        def __init__(self, content):
            self.content = content

        async def send(self):
            sent_messages.append(self.content)

    logger = SimpleNamespace(
        warning=lambda *args, **kwargs: log_calls.append(("warning", args, kwargs)),
        exception=lambda *args, **kwargs: log_calls.append(("exception", args, kwargs)),
    )
    module.logger = logger
    module.cl = SimpleNamespace(Message=Message)
    monkeypatch.setattr(
        module,
        "_openrag_api_key_from_user_or_context",
        lambda _user: (_ for _ in ()).throw(module.MissingOpenRAGCredentialError("expired handoff")),
    )

    profiles = await module.chat_profile(SimpleNamespace(metadata={"provider": "credentials"}))

    assert profiles == []
    assert sent_messages == ["expired handoff"]
    assert [call[0] for call in log_calls] == ["warning"]


def test_oidc_user_prefers_session_cookie_when_handoff_cookie_is_left_over(monkeypatch):
    module = _load_app_front(monkeypatch, auth_mode="oidc", module_name="app_front_oidc_cookie_priority_test")
    user_session = SimpleNamespace(values={}, get=lambda key: None)
    user_session.set = lambda key, value: user_session.values.update({key: value})
    module.cl = SimpleNamespace(user_session=user_session)
    monkeypatch.setattr(
        module,
        "get_context",
        lambda: SimpleNamespace(
            session=SimpleNamespace(
                environ={
                    "HTTP_COOKIE": (
                        f"openrag_session=current-session-token; "
                        f"{module.CHAINLIT_TOKEN_COOKIE_NAME}=left-over-handoff-token"
                    )
                }
            )
        ),
    )

    api_key = module._openrag_api_key_from_user_or_context(SimpleNamespace(metadata={"provider": "oidc"}))

    assert api_key == "current-session-token"


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

    async def fake_load_model_ids(_client, api_key):
        assert api_key == "handoff-token"
        return ["openrag-handoff", "openrag-all"]

    monkeypatch.setattr(module, "_load_user_info", fake_load_user_info)
    monkeypatch.setattr(module, "_load_openrag_model_ids_for_metadata", fake_load_model_ids)

    user = await module._chainlit_user_from_browser_cookies(
        {"cookie": (f"openrag_session=stale-session-token; {module.CHAINLIT_TOKEN_COOKIE_NAME}=handoff-token")}
    )

    assert attempts == ["stale-session-token", "handoff-token"]
    assert user.identifier == "Handoff User"
    assert user.metadata["provider"] == "credentials"
    assert user.metadata[module.OPENRAG_CHAT_PROFILES_METADATA_KEY] == ["openrag-handoff", "openrag-all"]
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
