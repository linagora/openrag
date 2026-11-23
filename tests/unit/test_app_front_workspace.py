"""Workspace scope regression tests for the Chainlit app."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

_APP_FRONT = Path(__file__).resolve().parents[2] / "openrag" / "app_front.py"


def _load_app_front(monkeypatch, *, module_name: str, persistency: bool = False):
    monkeypatch.setenv("AUTH_TOKEN", "test-token")
    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "x" * 32)
    monkeypatch.setenv("CHAINLIT_DATALAYER_COMPOSE", "postgres" if persistency else "")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.syspath_prepend(str(_APP_FRONT.parent))

    # Importing the app executes Chainlit decorators that overwrite the global
    # ``config.code`` callback registrations. Snapshot and restore them so one
    # test's module load can't leak callbacks into the next.
    from chainlit.config import CodeSettings
    from chainlit.config import config as chainlit_config

    saved_code = chainlit_config.code
    chainlit_config.code = CodeSettings(action_callbacks={})
    try:
        spec = importlib.util.spec_from_file_location(module_name, _APP_FRONT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        chainlit_config.code = saved_code
    return module


class _UserSession:
    def __init__(self, values: dict | None = None):
        self.values = values or {}

    def get(self, key: str, default=None):
        return self.values.get(key, default)

    def set(self, key: str, value):
        self.values[key] = value


def _bind_user_session(monkeypatch, module, session, *, socket_id=None):
    runtime_session = SimpleNamespace(id="workspace-test", socket_id=socket_id)
    monkeypatch.setattr(module, "get_context", lambda: SimpleNamespace(session=runtime_session))
    monkeypatch.setattr(module.cl, "user_session", session)
    monkeypatch.setitem(module.user_sessions, runtime_session.id, session.values)
    return runtime_session


class _Message:
    sent: list[str] = []

    def __init__(self, content: str = "", **_kwargs):
        self.content = content

    async def send(self):
        self.sent.append(self.content)


@pytest.mark.asyncio
async def test_workspace_catalogue_keeps_successful_partitions_when_another_fails(monkeypatch):
    module = _load_app_front(monkeypatch, module_name="app_front_workspace_catalogue_test")
    monkeypatch.setattr(
        module,
        "_load_openrag_model_ids",
        lambda *_args: _resolved(["openrag-all", "openrag-finance", "openrag-legal"]),
    )
    monkeypatch.setattr(module, "get_headers", lambda _api_key: {})
    monkeypatch.setattr(module, "t", lambda key: key)

    class Client:
        async def get(self, url: str, **_kwargs):
            if "/finance/" in url:
                return httpx.Response(
                    200,
                    request=httpx.Request("GET", url),
                    json={"workspaces": [{"workspace_id": "budget", "display_name": "Budget"}]},
                )
            return httpx.Response(503, request=httpx.Request("GET", url))

    items, failed_partitions, loaded_partitions = await module._load_workspace_setting_items(
        Client(), "token", "openrag-all"
    )

    assert any(value == '["finance","budget"]' for value in items.values())
    assert failed_partitions == ["legal"]
    assert loaded_partitions == 1


@pytest.mark.asyncio
async def test_workspace_catalogue_order_is_deterministic_for_casefold_ties(monkeypatch):
    module = _load_app_front(monkeypatch, module_name="app_front_workspace_catalogue_order_test")
    monkeypatch.setattr(
        module,
        "_load_openrag_model_ids",
        lambda *_args: _resolved(["openrag-a", "openrag-A", "openrag-all"]),
    )
    monkeypatch.setattr(module, "get_headers", lambda _api_key: {})
    monkeypatch.setattr(module, "t", lambda key: key)

    class Client:
        async def get(self, url: str, **_kwargs):
            if "/A/" in url:
                workspaces = [{"workspace_id": "alpha"}, {"workspace_id": "Alpha"}]
            else:
                workspaces = [{"workspace_id": "beta"}]
            return httpx.Response(200, request=httpx.Request("GET", url), json={"workspaces": workspaces})

    items, failed_partitions, loaded_partitions = await module._load_workspace_setting_items(
        Client(), "token", "openrag-all"
    )

    assert failed_partitions == []
    assert loaded_partitions == 2
    assert list(items.values()) == [
        module.WORKSPACE_NONE_VALUE,
        '["A","Alpha"]',
        '["A","alpha"]',
        '["a","beta"]',
    ]


async def _resolved(value):
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("submitted_value", "extra_allowed_values", "verifying", "expected_message"),
    [
        ("untrusted-value", set(), False, "workspace_scope_invalid"),
        (
            '["finance","replacement"]',
            {'["finance","replacement"]'},
            True,
            "workspace_scope_resume_error",
        ),
    ],
)
async def test_rejected_workspace_setting_restores_the_persisted_thread_value(
    monkeypatch,
    submitted_value,
    extra_allowed_values,
    verifying,
    expected_message,
):
    module = _load_app_front(monkeypatch, module_name="app_front_workspace_settings_test")
    committed_value = '["finance","current"]'
    persisted_settings = {module.WORKSPACE_SETTING_ID: submitted_value}
    session = _UserSession(
        {
            module.WORKSPACE_SCOPE_VALUE_SESSION_KEY: committed_value,
            module.WORKSPACE_VALUES_SESSION_KEY: {
                module.WORKSPACE_NONE_VALUE,
                committed_value,
                *extra_allowed_values,
            },
            module.WORKSPACE_VERIFYING_SESSION_KEY: verifying,
            module.WORKSPACE_INVALID_SESSION_KEY: verifying,
            "chat_settings": persisted_settings,
        }
    )
    context = SimpleNamespace(session=SimpleNamespace(chat_settings=persisted_settings))
    _Message.sent = []
    monkeypatch.setattr(module.cl, "user_session", session)
    monkeypatch.setattr(module.cl, "Message", _Message)
    monkeypatch.setattr(module, "get_context", lambda: context)
    monkeypatch.setattr(module, "t", lambda key: key)

    await module.on_chat_settings_update({module.WORKSPACE_SETTING_ID: submitted_value})

    assert context.session.chat_settings[module.WORKSPACE_SETTING_ID] == committed_value
    assert session.get("chat_settings")[module.WORKSPACE_SETTING_ID] == committed_value
    assert session.get(module.WORKSPACE_INVALID_SESSION_KEY) is True
    assert _Message.sent == [expected_message]


@pytest.mark.asyncio
async def test_locked_workspace_scope_rejects_a_different_valid_setting(monkeypatch):
    module = _load_app_front(monkeypatch, module_name="app_front_workspace_lock_test")
    committed_value = '["finance","current"]'
    submitted_value = '["finance","replacement"]'
    persisted_settings = {module.WORKSPACE_SETTING_ID: submitted_value}
    session = _UserSession(
        {
            module.WORKSPACE_SCOPE_VALUE_SESSION_KEY: committed_value,
            module.WORKSPACE_VALUES_SESSION_KEY: {
                module.WORKSPACE_NONE_VALUE,
                committed_value,
                submitted_value,
            },
            module.WORKSPACE_LOCKED_SESSION_KEY: True,
            "chat_settings": persisted_settings,
        }
    )
    context = SimpleNamespace(session=SimpleNamespace(chat_settings=persisted_settings))
    _Message.sent = []
    monkeypatch.setattr(module.cl, "user_session", session)
    monkeypatch.setattr(module.cl, "Message", _Message)
    monkeypatch.setattr(module, "get_context", lambda: context)
    monkeypatch.setattr(module, "t", lambda key: key)

    async def restore_settings(value, *, disabled=False):
        assert disabled is True
        context.session.chat_settings[module.WORKSPACE_SETTING_ID] = value
        return {module.WORKSPACE_SETTING_ID: value}

    monkeypatch.setattr(module, "_send_workspace_settings", restore_settings)

    await module.on_chat_settings_update({module.WORKSPACE_SETTING_ID: submitted_value})

    assert context.session.chat_settings[module.WORKSPACE_SETTING_ID] == committed_value
    assert _Message.sent == ["workspace_scope_locked"]


@pytest.mark.asyncio
async def test_resume_preserves_history_and_blocks_messages_until_workspace_verification_finishes(monkeypatch):
    module = _load_app_front(
        monkeypatch,
        module_name="app_front_workspace_resume_test",
        persistency=True,
    )
    raw_scope = json.dumps(["finance", "budget"], separators=(",", ":"))
    saved_messages = [
        {"role": "user", "content": "Earlier question"},
        {"role": "assistant", "content": "Earlier answer"},
    ]
    session = _UserSession(
        {
            "chat_settings": {module.WORKSPACE_SETTING_ID: raw_scope},
            # Deep copy: the resume hook must not be able to satisfy the
            # history assertion by mutating the session's own list in place.
            "messages": copy.deepcopy(saved_messages),
        }
    )
    _Message.sent = []
    runtime_session = _bind_user_session(monkeypatch, module, session, socket_id="first-socket")
    runtime_session.chat_settings = session.get("chat_settings")
    monkeypatch.setattr(module.cl, "Message", _Message)

    class ChatSettings:
        def __init__(self, *_args, **_kwargs):
            pass

        async def send(self):
            return {module.WORKSPACE_SETTING_ID: raw_scope}

    monkeypatch.setattr(module.cl, "ChatSettings", ChatSettings)
    monkeypatch.setattr(module, "_current_openrag_api_key", lambda: "token")
    monkeypatch.setattr(module, "get_headers", lambda _api_key: {})
    monkeypatch.setattr(module, "t", lambda key: key)

    request_started = asyncio.Event()
    finish_request = asyncio.Event()

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"workspace_id": "budget", "display_name": "Budget"}

    class AsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, **_kwargs):
            request_started.set()
            await finish_request.wait()
            return Response()

    monkeypatch.setattr(module.httpx, "AsyncClient", AsyncClient)
    monkeypatch.setattr(module, "INTERNAL_BASE_URL", "http://openrag.test")

    resume_task = asyncio.create_task(module.on_chat_resume({}))
    request_started_task = asyncio.create_task(request_started.wait())
    try:
        # Fail rather than hang forever if the hook never starts its
        # verification request (or raises before it).
        done, _pending = await asyncio.wait(
            {request_started_task, resume_task},
            timeout=5,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if resume_task in done:
            await resume_task
            pytest.fail("resume finished before verifying the workspace")
        assert request_started_task in done, "verification request never started"
        assert session.get(module.WORKSPACE_INVALID_SESSION_KEY) is True

        await module.on_message(SimpleNamespace(content="question"))
        assert _Message.sent == ["workspace_scope_invalid"]

        # Keep the saved scope if a rejected settings update arrives while
        # the resumed workspace is still being verified.
        runtime_session.chat_settings[module.WORKSPACE_SETTING_ID] = module.WORKSPACE_NONE_VALUE
        await module.on_chat_settings_update(runtime_session.chat_settings)
        assert runtime_session.chat_settings[module.WORKSPACE_SETTING_ID] == raw_scope

        finish_request.set()
        await asyncio.wait_for(resume_task, timeout=5)
    finally:
        finish_request.set()
        for task in (request_started_task, resume_task):
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    assert session.get(module.WORKSPACE_INVALID_SESSION_KEY) is False
    assert session.get(module.WORKSPACE_SCOPE_VALUE_SESSION_KEY) == raw_scope
    assert session.get("messages") == saved_messages
    assert module._workspace_runtime_ready()

    # Chainlit reloads persisted thread data onto the same websocket session.
    # Turns must wait both before and after the user-session dictionary changes.
    runtime_session.socket_id = "next-socket"
    await module.on_message(SimpleNamespace(content="before reload"))
    session.values = {"chat_settings": {module.WORKSPACE_SETTING_ID: raw_scope}}
    monkeypatch.setitem(module.user_sessions, runtime_session.id, session.values)
    await module.on_message(SimpleNamespace(content="before resume hook"))
    assert _Message.sent[-2:] == ["workspace_scope_initializing"] * 2
    assert session.get("messages") is None

    await asyncio.wait_for(module.on_chat_resume({}), timeout=5)
    assert module._workspace_runtime_ready()
    assert session.get(module.WORKSPACE_SCOPE_SESSION_KEY) == ("finance", "budget")

    # Even another metadata reload on the same socket invalidates readiness.
    session.values = {"message": "Metadata size exceeds the limit"}
    monkeypatch.setitem(module.user_sessions, runtime_session.id, session.values)
    await module.on_message(SimpleNamespace(content="before redacted resume hook"))
    assert _Message.sent[-1] == "workspace_scope_initializing"
    assert not module._workspace_runtime_ready()


@pytest.mark.asyncio
async def test_message_before_workspace_initialization_is_blocked(monkeypatch):
    module = _load_app_front(monkeypatch, module_name="app_front_workspace_not_ready_test")
    session = _UserSession({})
    runtime_session = SimpleNamespace()
    _Message.sent = []
    monkeypatch.setattr(module.cl, "user_session", session)
    monkeypatch.setattr(module.cl, "Message", _Message)
    monkeypatch.setattr(module, "get_context", lambda: SimpleNamespace(session=runtime_session))
    monkeypatch.setattr(module, "t", lambda key: key)

    await module.on_message(SimpleNamespace(content="question"))

    assert _Message.sent == ["workspace_scope_initializing"]
    assert session.get(module.WORKSPACE_LOCKED_SESSION_KEY, False) is False
    assert session.get("messages", []) == []


@pytest.mark.asyncio
async def test_message_before_resume_hook_blocks_persisted_scope(monkeypatch):
    module = _load_app_front(monkeypatch, module_name="app_front_workspace_resume_race_test")
    saved_scope = '["finance","budget"]'
    session = _UserSession(
        {
            module.WORKSPACE_SCOPE_VALUE_SESSION_KEY: saved_scope,
            module.WORKSPACE_SCOPE_SESSION_KEY: ("finance", "budget"),
            module.WORKSPACE_VALUES_SESSION_KEY: {module.WORKSPACE_NONE_VALUE, saved_scope},
            module.WORKSPACE_LOCKED_SESSION_KEY: True,
            module.WORKSPACE_INVALID_SESSION_KEY: False,
            "messages": [{"role": "user", "content": "Earlier question"}],
        }
    )
    runtime_session = SimpleNamespace()
    _Message.sent = []
    monkeypatch.setattr(module.cl, "user_session", session)
    monkeypatch.setattr(module.cl, "Message", _Message)
    monkeypatch.setattr(module, "get_context", lambda: SimpleNamespace(session=runtime_session))
    monkeypatch.setattr(module, "t", lambda key: key)

    await module.on_message(SimpleNamespace(content="question"))

    assert _Message.sent == ["workspace_scope_initializing"]
    assert session.get("messages") == [{"role": "user", "content": "Earlier question"}]


@pytest.mark.asyncio
async def test_message_during_chat_start_catalogue_load_does_not_lock_unscoped(monkeypatch):
    module = _load_app_front(monkeypatch, module_name="app_front_workspace_start_race_test")
    session = _UserSession({"chat_profile": "openrag-all"})
    runtime_session = _bind_user_session(monkeypatch, module, session, socket_id="first-socket")
    setattr(runtime_session, module._WORKSPACE_RUNTIME_READY_ATTRIBUTE, module._workspace_runtime_state())
    _Message.sent = []
    monkeypatch.setattr(module.cl, "Message", _Message)
    monkeypatch.setattr(module, "_current_openrag_api_key", lambda: "token")
    monkeypatch.setattr(module, "get_headers", lambda _api_key: {})
    monkeypatch.setattr(module, "t", lambda key: key)

    class HealthClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url: str, **_kwargs):
            return httpx.Response(200, request=httpx.Request("GET", url), text="ok")

    monkeypatch.setattr(module.httpx, "AsyncClient", HealthClient)
    monkeypatch.setattr(
        module.cl,
        "context",
        SimpleNamespace(emitter=SimpleNamespace(set_commands=lambda _commands: _resolved(None))),
    )

    catalogue_started = asyncio.Event()
    finish_catalogue = asyncio.Event()

    async def load_items(*_args):
        catalogue_started.set()
        await finish_catalogue.wait()
        return {module.WORKSPACE_NONE_VALUE: module.WORKSPACE_NONE_VALUE}, [], 1

    monkeypatch.setattr(module, "_load_workspace_setting_items", load_items)

    async def send_settings(value=module.WORKSPACE_NONE_VALUE, *, disabled=False):
        return {module.WORKSPACE_SETTING_ID: value}

    monkeypatch.setattr(module, "_send_workspace_settings", send_settings)

    start_task = asyncio.create_task(module.on_chat_start())
    try:
        await asyncio.wait_for(catalogue_started.wait(), timeout=5)
        await module.on_message(SimpleNamespace(content="question"))
        assert _Message.sent == ["workspace_scope_initializing"]
        assert session.get(module.WORKSPACE_LOCKED_SESSION_KEY) is False
        assert session.get("messages", []) == []
    finally:
        finish_catalogue.set()
        await asyncio.wait_for(start_task, timeout=5)

    assert module._workspace_runtime_ready()
    runtime_session.socket_id = "next-socket"
    assert module._workspace_runtime_ready()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_workspace_setting", [False, True], ids=["legacy", "explicit"])
async def test_unscoped_resume_remains_valid(monkeypatch, has_workspace_setting):
    module = _load_app_front(
        monkeypatch,
        module_name="app_front_workspace_unscoped_resume_test",
        persistency=True,
    )
    thread_metadata = {}
    if has_workspace_setting:
        thread_metadata["chat_settings"] = {module.WORKSPACE_SETTING_ID: module.WORKSPACE_NONE_VALUE}
    session = _UserSession(dict(thread_metadata))
    _bind_user_session(monkeypatch, module, session)
    _Message.sent = []
    monkeypatch.setattr(module.cl, "Message", _Message)

    sent_settings = []

    class ChatSettings:
        def __init__(self, inputs, **_kwargs):
            self._inputs = inputs

        async def send(self):
            select = self._inputs[0]
            sent_settings.append((select.initial_value, select.disabled))
            return {module.WORKSPACE_SETTING_ID: select.initial_value}

    monkeypatch.setattr(module.cl, "ChatSettings", ChatSettings)
    monkeypatch.setattr(module, "t", lambda key: key)

    await asyncio.wait_for(
        module.on_chat_resume({"metadata": thread_metadata}),
        timeout=5,
    )

    assert session.get(module.WORKSPACE_INVALID_SESSION_KEY) is False
    assert session.get(module.WORKSPACE_VERIFYING_SESSION_KEY) is False
    assert session.get(module.WORKSPACE_SCOPE_VALUE_SESSION_KEY) == module.WORKSPACE_NONE_VALUE
    assert session.get(module.WORKSPACE_SCOPE_UNRECOVERABLE_SESSION_KEY, False) is False
    assert module._workspace_runtime_ready()
    assert sent_settings == [(module.WORKSPACE_NONE_VALUE, True)]
    assert _Message.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("serialize_metadata", [False, True], ids=["dict", "json"])
async def test_redacted_resume_stays_blocked_after_metadata_is_rewritten(monkeypatch, serialize_metadata):
    from chainlit.session import clean_metadata

    module = _load_app_front(
        monkeypatch,
        module_name="app_front_workspace_redacted_blocked_test",
        persistency=True,
    )
    scope = json.dumps(["finance", "budget"], separators=(",", ":"))
    redacted_metadata = clean_metadata(
        {
            "chat_settings": {module.WORKSPACE_SETTING_ID: scope},
            module.WORKSPACE_PERSISTED_SCOPE_SESSION_KEY: scope,
            "messages": [{"role": "user", "content": "x" * 1_048_576}],
        }
    )
    thread_metadata = json.dumps(redacted_metadata) if serialize_metadata else redacted_metadata
    session = _UserSession(dict(redacted_metadata))
    _bind_user_session(monkeypatch, module, session)
    _Message.sent = []
    monkeypatch.setattr(module.cl, "Message", _Message)
    monkeypatch.setattr(module, "t", lambda key: key)

    async def never_called(*_args, **_kwargs):
        raise AssertionError("no verification request should be issued")

    monkeypatch.setattr(module, "_current_openrag_api_key", never_called)

    await asyncio.wait_for(
        module.on_chat_resume({"metadata": thread_metadata}),
        timeout=5,
    )

    assert session.get(module.WORKSPACE_INVALID_SESSION_KEY) is True
    assert session.get(module.WORKSPACE_VERIFYING_SESSION_KEY) is False
    assert session.get(module.WORKSPACE_VERIFY_FAILED_SESSION_KEY) is True
    assert session.get(module.WORKSPACE_SCOPE_UNRECOVERABLE_SESSION_KEY) is True
    assert session.get(module.WORKSPACE_SCOPE_VALUE_SESSION_KEY) == module.WORKSPACE_NONE_VALUE
    assert not module._workspace_runtime_ready()
    assert _Message.sent == ["workspace_scope_resume_error"]

    # Chainlit may persist the small blocked session without the original
    # redaction marker. The sticky flag must prevent a later resume from
    # interpreting the now-missing setting as an unscoped conversation.
    persisted_metadata = {
        module.WORKSPACE_SCOPE_UNRECOVERABLE_SESSION_KEY: True,
        module.WORKSPACE_INVALID_SESSION_KEY: True,
        module.WORKSPACE_VERIFY_FAILED_SESSION_KEY: True,
    }
    resumed_session = _UserSession(dict(persisted_metadata))
    _bind_user_session(monkeypatch, module, resumed_session)
    await asyncio.wait_for(module.on_chat_resume({"metadata": persisted_metadata}), timeout=5)
    assert resumed_session.get(module.WORKSPACE_INVALID_SESSION_KEY) is True
    assert resumed_session.get(module.WORKSPACE_SCOPE_UNRECOVERABLE_SESSION_KEY) is True
    assert not module._workspace_runtime_ready()
    assert _Message.sent == ["workspace_scope_resume_error", "workspace_scope_resume_error"]

    # A later turn is still blocked instead of going to the model unscoped.
    await module.on_message(SimpleNamespace(content="question"))
    assert _Message.sent == [
        "workspace_scope_resume_error",
        "workspace_scope_resume_error",
        "workspace_scope_invalid",
    ]


@pytest.mark.asyncio
async def test_failed_verification_is_not_cleared_by_a_no_workspace_update(monkeypatch):
    """After a failed resume verification, a "No workspace" settings update must
    not silently clear the invalid flag and drop the chat back to unscoped."""
    module = _load_app_front(
        monkeypatch,
        module_name="app_front_workspace_verify_failed_test",
        persistency=True,
    )
    saved_scope = json.dumps(["finance", "budget"], separators=(",", ":"))
    session = _UserSession(
        {
            "chat_settings": {module.WORKSPACE_SETTING_ID: saved_scope},
            module.WORKSPACE_VERIFY_FAILED_SESSION_KEY: True,
            module.WORKSPACE_INVALID_SESSION_KEY: True,
            module.WORKSPACE_VERIFYING_SESSION_KEY: False,
            module.WORKSPACE_LOCKED_SESSION_KEY: True,
            module.WORKSPACE_SCOPE_VALUE_SESSION_KEY: module.WORKSPACE_NONE_VALUE,
            module.WORKSPACE_VALUES_SESSION_KEY: {module.WORKSPACE_NONE_VALUE},
            module.WORKSPACE_PERSISTED_SCOPE_SESSION_KEY: saved_scope,
        }
    )
    context = SimpleNamespace(
        session=SimpleNamespace(chat_settings={module.WORKSPACE_SETTING_ID: module.WORKSPACE_NONE_VALUE})
    )
    _Message.sent = []
    monkeypatch.setattr(module.cl, "user_session", session)
    monkeypatch.setattr(module.cl, "Message", _Message)
    monkeypatch.setattr(module, "get_context", lambda: context)
    monkeypatch.setattr(module, "t", lambda key: key)

    await module.on_chat_settings_update({module.WORKSPACE_SETTING_ID: module.WORKSPACE_NONE_VALUE})

    assert session.get(module.WORKSPACE_INVALID_SESSION_KEY) is True
    assert session.get(module.WORKSPACE_VERIFY_FAILED_SESSION_KEY) is True
    assert _Message.sent == ["workspace_scope_resume_error"]


@pytest.mark.asyncio
async def test_unreadable_saved_scope_stays_blocked_after_rejected_update(monkeypatch):
    module = _load_app_front(monkeypatch, module_name="app_front_unreadable_scope_test", persistency=True)
    chat_settings = {module.WORKSPACE_SETTING_ID: ["finance", "budget"]}
    session = _UserSession({"chat_settings": chat_settings})
    runtime_session = _bind_user_session(monkeypatch, module, session)
    runtime_session.chat_settings = chat_settings
    _Message.sent = []
    monkeypatch.setattr(module.cl, "Message", _Message)
    monkeypatch.setattr(module, "t", lambda key: key)

    await module.on_chat_resume({"metadata": session.values.copy()})
    assert not module._workspace_runtime_ready()

    # Chainlit writes the setting before invoking the settings callback.
    chat_settings[module.WORKSPACE_SETTING_ID] = module.WORKSPACE_NONE_VALUE
    await module.on_chat_settings_update(chat_settings)

    thread_metadata = copy.deepcopy(session.values)
    resumed_session = _UserSession(thread_metadata.copy())
    _bind_user_session(monkeypatch, module, resumed_session)
    await module.on_chat_resume({"metadata": thread_metadata})

    assert _Message.sent == ["workspace_scope_resume_error"] * 3
    assert resumed_session.get(module.WORKSPACE_SCOPE_UNRECOVERABLE_SESSION_KEY) is True
    assert not module._workspace_runtime_ready()


@pytest.mark.asyncio
async def test_workspace_catalogue_loads_partitions_concurrently(monkeypatch):
    """Several partitions must be fetched under a bounded concurrency, not one
    at a time, so a slow partition can't serialize the whole selector load."""
    module = _load_app_front(monkeypatch, module_name="app_front_workspace_concurrency_test")
    monkeypatch.setattr(
        module,
        "_load_openrag_model_ids",
        lambda *_args: _resolved([f"openrag-{name}" for name in ["a", "b", "c", "d", "e", "f", "g", "h"]]),
    )
    monkeypatch.setattr(module, "get_headers", lambda _api_key: {})
    monkeypatch.setattr(module, "t", lambda key: key)

    in_flight = 0
    peak_in_flight = 0

    class Client:
        async def get(self, url: str, **_kwargs):
            nonlocal in_flight, peak_in_flight
            in_flight += 1
            peak_in_flight = max(peak_in_flight, in_flight)
            try:
                await asyncio.sleep(0.01)
            finally:
                in_flight -= 1
            return httpx.Response(200, request=httpx.Request("GET", url), json={"workspaces": []})

    items, failed_partitions, loaded_partitions = await module._load_workspace_setting_items(
        Client(), "token", "openrag-all"
    )

    assert failed_partitions == []
    assert loaded_partitions == 8
    assert peak_in_flight > 1  # requests overlapped rather than serializing
    assert peak_in_flight <= 4  # bounded by the semaphore
