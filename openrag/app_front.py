import json
import os
import re
import secrets
import string
import time
import unicodedata
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlparse

import chainlit as cl
import httpx
from chainlit.input_widget import Select
from chainlit.config import config as cl_config
from chainlit.context import get_context
from core.auth.chainlit import (
    CHAINLIT_AUTH_COOKIE_NAME,
    CHAINLIT_LOGOUT_COOKIE_MAX_AGE_SECONDS,
    CHAINLIT_LOGOUT_COOKIE_NAME,
    CHAINLIT_TOKEN_COOKIE_NAME,
    CHAINLIT_TOKEN_COOKIE_PATH,
)
from core.utils.consts import PARTITION_PREFIX
from core.utils.logging import get_logger, mask_email
from core.utils.web_url import normalize_web_url
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()
logger = get_logger()

PERSISTENCY = os.environ.get("CHAINLIT_DATALAYER_COMPOSE", "") != ""
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")
AUTH_MODE = os.environ.get("AUTH_MODE", "token").strip().lower()

# Chainlit authentication
CHAINLIT_AUTH_SECRET = os.environ.get("CHAINLIT_AUTH_SECRET")

# Application internal URL (used to call the API from Chainlit)
port = os.environ.get("APP_iPORT", "8080")
INTERNAL_BASE_URL = f"http://localhost:{port}"  # Default fallback URL

DEFAULT_LANGUAGE = os.environ.get("DEFAULT_LANGUAGE")
OPENRAG_API_KEY_SESSION_KEY = "openrag_api_key"
OPENRAG_AUTH_HANDLE_METADATA_KEY = "openrag_auth_handle"
OPENRAG_CHAT_PROFILES_METADATA_KEY = "openrag_chat_profiles"
OPENRAG_SESSION_COOKIE_NAME = "openrag_session"
WORKSPACE_SETTING_ID = "Workspace"
WORKSPACE_NONE_VALUE = "__openrag_no_workspace__"
WORKSPACE_SCOPE_SESSION_KEY = "openrag_workspace_scope"
WORKSPACE_SCOPE_VALUE_SESSION_KEY = "openrag_workspace_scope_value"
WORKSPACE_ITEMS_SESSION_KEY = "openrag_workspace_items"
WORKSPACE_VALUES_SESSION_KEY = "openrag_workspace_values"
WORKSPACE_LOCKED_SESSION_KEY = "openrag_workspace_locked"
WORKSPACE_INVALID_SESSION_KEY = "openrag_workspace_invalid"
_OPENRAG_TOKEN_STORE: dict[str, tuple[str, float]] = {}
_MARKDOWN_ESCAPE_TABLE = str.maketrans({char: f"\\{char}" for char in string.punctuation})
_MARKDOWN_URL_SAFE_CHARS = ":/?#[]@!$&'+,;=%"
# Chainlit inserts source names into Markdown link labels. Strip only characters
# that can break or restyle that label so ordinary filenames stay recognizable.
_MARKDOWN_UNSAFE_SOURCE_NAME_CHARS = str.maketrans(dict.fromkeys("[]*`\\<>", " "))
_MARKDOWN_BLOCK_PREFIX_RE = re.compile(r"^(?:(?:#{1,6}|[-+]|\d{1,9}[.)])(?:\s+|$))+")
_MARKDOWN_THEMATIC_BREAK_RE = re.compile(r"^(?:(?:-\s*){3,}|(?:_\s*){3,}|(?:\*\s*){3,})$")
_MARKDOWN_SETEXT_UNDERLINE_RE = re.compile(r"^(?:=+|-+)$")


class MissingOpenRAGCredentialError(RuntimeError):
    pass


def _escape_markdown_text(value: str) -> str:
    """Render untrusted source metadata as literal Markdown text."""
    return value.translate(_MARKDOWN_ESCAPE_TABLE)


def _neutralize_source_name_delimiters(value: str) -> str:
    """Remove active Markdown delimiters while preserving safe filename punctuation."""
    characters = list(value)
    underscore_openers: list[tuple[int, int]] = []
    index = 0

    while index < len(value):
        if value[index] != "_":
            index += 1
            continue

        end = index + 1
        while end < len(value) and value[end] == "_":
            end += 1

        previous = value[index - 1] if index else None
        following = value[end] if end < len(value) else None
        previous_whitespace = previous is None or previous.isspace()
        following_whitespace = following is None or following.isspace()
        previous_punctuation = previous is not None and unicodedata.category(previous)[0] in {"P", "S"}
        following_punctuation = following is not None and unicodedata.category(following)[0] in {"P", "S"}
        left_flanking = not following_whitespace and (
            not following_punctuation or previous_whitespace or previous_punctuation
        )
        right_flanking = not previous_whitespace and (
            not previous_punctuation or following_whitespace or following_punctuation
        )
        can_open = left_flanking and (not right_flanking or previous_punctuation)
        can_close = right_flanking and (not left_flanking or following_punctuation)

        if can_close and underscore_openers:
            opener_start, opener_end = underscore_openers.pop()
            characters[opener_start:opener_end] = " " * (opener_end - opener_start)
            characters[index:end] = " " * (end - index)
        elif can_open:
            underscore_openers.append((index, end))
        index = end

    index = 0
    while index < len(value):
        if value[index] != "~":
            index += 1
            continue
        end = index + 1
        while end < len(value) and value[end] == "~":
            end += 1
        if end - index >= 2:
            characters[index:end] = " " * (end - index)
        index = end

    return "".join(characters)


def _safe_source_name(value: str, existing: dict) -> str:
    """Build a Markdown-inert name that Chainlit can match to its element."""
    value = _neutralize_source_name_delimiters(value)
    value = value.lstrip()
    while True:
        value = " ".join(value.translate(_MARKDOWN_UNSAFE_SOURCE_NAME_CHARS).split())
        if _MARKDOWN_THEMATIC_BREAK_RE.fullmatch(value) or _MARKDOWN_SETEXT_UNDERLINE_RE.fullmatch(value):
            value = ""
            break

        stripped = _MARKDOWN_BLOCK_PREFIX_RE.sub("", value)
        if stripped == value:
            break
        value = stripped

    base = value or "source"
    candidate = base
    suffix = 2
    while candidate in existing:
        candidate = f"{base} {suffix}"
        suffix += 1
    return candidate


def get_user_language() -> str:
    """Return the active language: env override if set, otherwise browser's Accept-Language."""
    if DEFAULT_LANGUAGE:
        return DEFAULT_LANGUAGE
    try:
        context = get_context()
        accept_language = context.session.environ.get("HTTP_ACCEPT_LANGUAGE", "")
        if accept_language:
            # "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7" → "fr-FR"
            return accept_language.split(",")[0].split(";")[0].strip()
    except Exception:
        pass
    return "en-US"


def _language_candidates(lang: str) -> list[str]:
    """Return locales to try in order: full code, base language, then en-US fallback."""
    candidates = [lang]
    base = lang.split("-")[0]
    if base and base != lang:
        candidates.append(base)
    candidates.append("en-US")
    return list(dict.fromkeys(candidates))


@lru_cache(maxsize=32)
def _load_app_strings(lang: str) -> dict:
    # Cached to avoid re-reading translation JSON from disk on every t() call.
    return cl_config.load_translation(lang).get("app", {})


def t(key: str) -> str:
    """Get a translated app string for the current user's language, falling back if missing."""
    for lang in _language_candidates(get_user_language()):
        app_strings = _load_app_strings(lang)
        if key in app_strings:
            return app_strings[key]
    return key


def get_headers(api_key):
    headers = {
        "Content-Type": "application/json",
        "accept": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _workspace_scope_from_value(value: object) -> tuple[str, str] | None:
    """Decode a Chainlit option value into its partition/workspace pair."""
    if not isinstance(value, str) or not value:
        return None
    try:
        scope = json.loads(value)
    except (TypeError, ValueError):
        return None
    if (
        isinstance(scope, list)
        and len(scope) == 2
        and all(isinstance(part, str) and part for part in scope)
    ):
        return scope[0], scope[1]
    return None


def _workspace_option_label(display_name: object, partition: str, workspace_id: str) -> str:
    """Keep user-controlled labels on one line and make same-named choices distinct."""
    raw_label = display_name if isinstance(display_name, str) else ""
    label = " ".join(raw_label.split())
    label = "".join(char if not unicodedata.category(char).startswith("C") else " " for char in label)
    label = " ".join(label.split())[:80] or workspace_id
    return f"{label} — {partition}/{workspace_id}"


async def _load_workspace_setting_items(
    client: httpx.AsyncClient,
    api_key: str,
    chat_profile: str | None,
) -> dict[str, str]:
    """Load workspace options visible through the selected partition profile."""
    if not isinstance(chat_profile, str) or not chat_profile.startswith(PARTITION_PREFIX):
        return {}

    profile_partition = chat_profile.removeprefix(PARTITION_PREFIX)
    model_ids = await _load_openrag_model_ids(client, api_key)
    available_partitions = sorted(
        {
            model_id.removeprefix(PARTITION_PREFIX)
            for model_id in model_ids
            if model_id.startswith(PARTITION_PREFIX) and model_id != f"{PARTITION_PREFIX}all"
        },
        key=str.casefold,
    )

    if profile_partition == "all":
        partitions = available_partitions
    elif chat_profile in model_ids:
        partitions = [profile_partition]
    else:
        return {}

    items = {t("workspace_scope_none"): WORKSPACE_NONE_VALUE}
    for partition in partitions:
        response = await client.get(
            url=f"{INTERNAL_BASE_URL}/partition/{quote(partition, safe='')}/workspaces",
            headers=get_headers(api_key),
        )
        response.raise_for_status()
        workspaces = response.json().get("workspaces", [])
        if not isinstance(workspaces, list):
            continue
        for workspace in sorted(
            (item for item in workspaces if isinstance(item, dict)),
            key=lambda item: str(item.get("workspace_id", "")).casefold(),
        ):
            workspace_id = workspace.get("workspace_id")
            if not isinstance(workspace_id, str) or not workspace_id:
                continue
            label = _workspace_option_label(workspace.get("display_name"), partition, workspace_id)
            value = json.dumps([partition, workspace_id], separators=(",", ":"))
            items[label] = value
    return items


def _set_workspace_scope_value(value: str) -> None:
    cl.user_session.set(WORKSPACE_SCOPE_VALUE_SESSION_KEY, value)
    cl.user_session.set(WORKSPACE_SCOPE_SESSION_KEY, _workspace_scope_from_value(value))


async def _send_workspace_settings(
    value: str = WORKSPACE_NONE_VALUE,
    *,
    disabled: bool = False,
) -> dict | None:
    items = cl.user_session.get(WORKSPACE_ITEMS_SESSION_KEY, {})
    if not isinstance(items, dict) or not items:
        return None
    allowed_values = set(items.values())
    initial_value = value if value in allowed_values else WORKSPACE_NONE_VALUE
    settings = await cl.ChatSettings(
        [
            Select(
                id=WORKSPACE_SETTING_ID,
                label=t("workspace_scope_label"),
                items=items,
                initial_value=initial_value,
                description=t("workspace_scope_description"),
                disabled=disabled,
            )
        ]
    ).send()
    return settings


def _extract_cookie(cookie_header: str, name: str) -> str | None:
    """Parse a single cookie value from a Cookie header. No dependency on http.cookies for simplicity."""
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            if k.strip() == name:
                return v.strip()
    return None


def _delete_cookie_and_chunks(request, response, name: str, *, paths: tuple[str, ...]) -> None:
    cookie_names = {name, *(cookie_name for cookie_name in request.cookies if cookie_name.startswith(f"{name}_"))}
    for cookie_name in cookie_names:
        for path in paths:
            response.delete_cookie(key=cookie_name, path=path)


def _is_request_secure(request) -> bool:
    if os.environ.get("PREFERRED_URL_SCHEME", "").lower() == "https":
        return True
    headers = getattr(request, "headers", {}) or {}
    xfp = headers.get("x-forwarded-proto", "")
    if xfp.split(",", 1)[0].strip().lower() == "https":
        return True
    url = getattr(request, "url", None)
    return getattr(url, "scheme", "") == "https"


def _clear_chat_logout_cookies(request, response) -> None:
    _delete_cookie_and_chunks(request, response, CHAINLIT_AUTH_COOKIE_NAME, paths=("/", CHAINLIT_TOKEN_COOKIE_PATH))
    _delete_cookie_and_chunks(request, response, CHAINLIT_TOKEN_COOKIE_NAME, paths=(CHAINLIT_TOKEN_COOKIE_PATH,))
    response.delete_cookie(key=OPENRAG_SESSION_COOKIE_NAME, path="/")
    secure_logout_signal = _is_request_secure(request)
    response.set_cookie(
        key=CHAINLIT_LOGOUT_COOKIE_NAME,
        value="1",
        max_age=CHAINLIT_LOGOUT_COOKIE_MAX_AGE_SECONDS,
        path="/",
        secure=secure_logout_signal,
        samesite="none" if secure_logout_signal else "lax",
    )


def _token_store_ttl_seconds() -> int:
    return int(getattr(cl_config.project, "user_session_timeout", 3600) or 3600)


def _cleanup_token_store(now: float | None = None) -> None:
    now = now if now is not None else time.monotonic()
    expired = [handle for handle, (_, expires_at) in _OPENRAG_TOKEN_STORE.items() if expires_at <= now]
    for handle in expired:
        _OPENRAG_TOKEN_STORE.pop(handle, None)


def _remember_openrag_api_key(api_key: str | None) -> str | None:
    if not api_key:
        return None
    _cleanup_token_store()
    handle = secrets.token_urlsafe(32)
    _OPENRAG_TOKEN_STORE[handle] = (api_key, time.monotonic() + _token_store_ttl_seconds())
    return handle


def _openrag_api_key_from_user(user) -> str | None:
    metadata = getattr(user, "metadata", {}) or {}
    handle = metadata.get(OPENRAG_AUTH_HANDLE_METADATA_KEY)
    if not handle:
        return None
    item = _OPENRAG_TOKEN_STORE.get(handle)
    if not item:
        return None
    api_key, expires_at = item
    now = time.monotonic()
    if expires_at <= now:
        _OPENRAG_TOKEN_STORE.pop(handle, None)
        return None
    _OPENRAG_TOKEN_STORE[handle] = (api_key, now + _token_store_ttl_seconds())
    return api_key


def _openrag_auth_provider_from_user(user) -> str | None:
    metadata = getattr(user, "metadata", {}) or {}
    provider = metadata.get("provider")
    return provider if isinstance(provider, str) else None


def _openrag_api_key_from_context_cookie(*, prefer_handoff: bool = False) -> str | None:
    try:
        context = get_context()
        cookie_header = context.session.environ.get("HTTP_COOKIE", "")
    except Exception:
        return None
    session_token = _extract_cookie(cookie_header, "openrag_session")
    handoff_token = _extract_cookie(cookie_header, CHAINLIT_TOKEN_COOKIE_NAME)
    if prefer_handoff:
        return handoff_token
    return session_token or handoff_token


def _current_openrag_api_key(default: str = "sk-1234") -> str:
    api_key = cl.user_session.get(OPENRAG_API_KEY_SESSION_KEY)
    if api_key:
        return api_key

    user = cl.user_session.get("user")
    return _openrag_api_key_from_user_or_context(user, default=default)


def _openrag_api_key_from_user_or_context(user, default: str = "sk-1234") -> str:
    api_key = _openrag_api_key_from_user(user)
    if api_key:
        cl.user_session.set(OPENRAG_API_KEY_SESSION_KEY, api_key)
        return api_key
    api_key = _openrag_api_key_from_context_cookie(
        prefer_handoff=_openrag_auth_provider_from_user(user) == "credentials"
    )
    if api_key:
        cl.user_session.set(OPENRAG_API_KEY_SESSION_KEY, api_key)
        return api_key
    if user is not None:
        raise MissingOpenRAGCredentialError(
            "Your OpenRAG Chat session expired. Please sign out of Chat and sign in again."
        )
    return default


async def _handle_missing_openrag_credential(error: MissingOpenRAGCredentialError) -> None:
    logger.warning("OpenRAG Chat session expired", error=str(error))
    await cl.Message(content=str(error)).send()


def _current_openrag_auth_provider() -> str | None:
    user = cl.user_session.get("user")
    return _openrag_auth_provider_from_user(user)


def _chainlit_url_prefix() -> str:
    """Browser-facing prefix Chainlit is served under, or "" when it owns the origin.

    ``mount_chainlit`` exports the submount path as ``CHAINLIT_ROOT_PATH`` (plus
    ``CHAINLIT_PARENT_ROOT_PATH`` when the parent app itself has a root_path)
    before it loads this module, and Chainlit's own HTML template builds its
    asset URLs from exactly this pair.
    """
    return os.getenv("CHAINLIT_PARENT_ROOT_PATH", "") + os.getenv("CHAINLIT_ROOT_PATH", "")


def _chat_profile_from_model_id(model_id: str) -> cl.ChatProfile | None:
    if not model_id.startswith(PARTITION_PREFIX):
        return None
    partition = model_id.removeprefix(PARTITION_PREFIX)
    description_key = "profile_description_all" if partition == "all" else "profile_description_partition"
    description_template = t(description_key)
    return cl.ChatProfile(
        name=model_id,
        markdown_description=description_template.format(name=model_id, partition=partition),
        # Chainlit's frontend treats an icon containing "/public" as relative to
        # its own API base and rewrites it (buildEndpoint), but renders the
        # assistant avatar from the same field *verbatim*. So "/public/favicon.svg"
        # resolves correctly in the profile picker and root-absolute in the avatar
        # — a 403 against the OpenRAG origin on every message, outside the
        # /chainlit auth bypass. Prefixing it by hand only moves the breakage to
        # the picker, which then doubles the prefix. Chainlit's own /favicon route
        # serves the same file from a path with no "/public" in it, so both
        # consumers use it unmodified.
        icon=f"{_chainlit_url_prefix()}/favicon",
        default=model_id == f"{PARTITION_PREFIX}all",
    )


def _chat_profiles_from_model_ids(model_ids: list[str]) -> list[cl.ChatProfile]:
    profiles = []
    for model_id in model_ids:
        profile = _chat_profile_from_model_id(model_id)
        if profile:
            profiles.append(profile)
    return profiles


def _cached_chat_profiles_from_user(user) -> list[cl.ChatProfile]:
    metadata = getattr(user, "metadata", {}) or {}
    model_ids = metadata.get(OPENRAG_CHAT_PROFILES_METADATA_KEY)
    if not isinstance(model_ids, list):
        return []
    return _chat_profiles_from_model_ids([model_id for model_id in model_ids if isinstance(model_id, str)])


async def _load_openrag_model_ids(client: httpx.AsyncClient, api_key: str) -> list[str]:
    response = await client.get(
        url=f"{INTERNAL_BASE_URL}/v1/models",
        headers=get_headers(api_key),
    )
    response.raise_for_status()
    data = response.json()
    models = data.get("data", [])
    if not isinstance(models, list):
        return []
    return [
        model["id"]
        for model in models
        if isinstance(model, dict) and isinstance(model.get("id"), str) and model["id"].startswith(PARTITION_PREFIX)
    ]


async def _load_openrag_model_ids_for_metadata(client: httpx.AsyncClient, api_key: str) -> list[str]:
    try:
        return await _load_openrag_model_ids(client, api_key)
    except Exception as e:
        logger.warning("Could not preload OpenRAG chat profiles for Chainlit", error=str(e))
        return []


def _chainlit_user_from_info(data: dict, *, provider: str, api_key: str, model_ids: list[str] | None = None) -> cl.User:
    metadata = {
        "role": "admin" if data.pop("is_admin", False) else "user",
        "provider": provider,
        "extra": data,
    }
    if model_ids:
        metadata[OPENRAG_CHAT_PROFILES_METADATA_KEY] = model_ids
    auth_handle = _remember_openrag_api_key(api_key)
    if auth_handle:
        metadata[OPENRAG_AUTH_HANDLE_METADATA_KEY] = auth_handle

    identifier = data.get("display_name") or data.get("email") or f"User #{data.get('id')}"
    return cl.User(identifier=identifier, metadata=metadata)


async def _load_user_info(client: httpx.AsyncClient, api_key: str) -> dict:
    response = await client.get(
        url=f"{INTERNAL_BASE_URL}/users/info",
        headers=get_headers(api_key),
    )
    response.raise_for_status()
    return response.json()


async def _chainlit_user_from_browser_cookies(headers: dict) -> cl.User | None:
    """Authenticate Chainlit from the browser cookies OpenRAG owns."""
    cookie_header = headers.get("cookie") or headers.get("Cookie") or ""
    session_token = _extract_cookie(cookie_header, "openrag_session")
    chainlit_token = _extract_cookie(cookie_header, CHAINLIT_TOKEN_COOKIE_NAME)
    api_key = session_token or chainlit_token
    if not api_key:
        logger.info("No OpenRAG auth cookie in Chainlit request")
        return None

    provider = "oidc" if session_token else "credentials"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            try:
                data = await _load_user_info(client, api_key)
            except httpx.HTTPStatusError as e:
                if e.response.status_code not in (401, 403) or not session_token or not chainlit_token:
                    raise
                api_key = chainlit_token
                provider = "credentials"
                data = await _load_user_info(client, api_key)
            model_ids = await _load_openrag_model_ids_for_metadata(client, api_key)
    except httpx.HTTPStatusError as e:
        logger.info("Session cookie rejected by /users/info", status=e.response.status_code)
        return None
    except Exception as e:
        logger.exception("Chainlit header_auth_callback failure", error=str(e))
        return None

    return _chainlit_user_from_info(data, provider=provider, api_key=api_key, model_ids=model_ids)


if PERSISTENCY:

    @cl.on_chat_resume
    async def on_chat_resume(thread):
        # Chainlit restores the committed ChatSettings from the persisted
        # thread's metadata. Keep the workspace scope for resumed turns, and
        # keep it fixed so the same conversation never mixes retrieval scopes.
        cl.user_session.set("messages", [])
        cl.user_session.set(WORKSPACE_LOCKED_SESSION_KEY, True)
        saved_settings = cl.user_session.get("chat_settings", {}) or {}
        if not isinstance(saved_settings, dict):
            saved_settings = {}
        raw_value = saved_settings.get(WORKSPACE_SETTING_ID, WORKSPACE_NONE_VALUE)
        if raw_value is None or raw_value == "":
            raw_value = WORKSPACE_NONE_VALUE
            invalid_scope = False
        elif not isinstance(raw_value, str):
            raw_value = WORKSPACE_NONE_VALUE
            invalid_scope = True
        else:
            invalid_scope = False
        scope = _workspace_scope_from_value(raw_value)
        invalid_scope = invalid_scope or bool(raw_value != WORKSPACE_NONE_VALUE and scope is None)
        items = {t("workspace_scope_none"): WORKSPACE_NONE_VALUE}
        if scope:
            partition, workspace_id = scope
            try:
                api_key = _current_openrag_api_key()
                async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                    response = await client.get(
                        url=(
                            f"{INTERNAL_BASE_URL}/partition/{quote(partition, safe='')}"
                            f"/workspaces/{quote(workspace_id, safe='')}"
                        ),
                        headers=get_headers(api_key),
                    )
                    response.raise_for_status()
                workspace = response.json()
                label = _workspace_option_label(
                    workspace.get("display_name") if isinstance(workspace, dict) else None,
                    partition,
                    workspace_id,
                )
                items[label] = raw_value
            except httpx.HTTPStatusError as e:
                logger.warning(
                    "Workspace scope is no longer available when resuming chat",
                    partition=partition,
                    status_code=e.response.status_code,
                )
                invalid_scope = True
            except Exception as e:
                logger.warning("Could not verify workspace scope when resuming chat", error=str(e))
                invalid_scope = True
        cl.user_session.set(WORKSPACE_ITEMS_SESSION_KEY, items)
        cl.user_session.set(WORKSPACE_VALUES_SESSION_KEY, set(items.values()))
        _set_workspace_scope_value(raw_value if scope and not invalid_scope else WORKSPACE_NONE_VALUE)
        cl.user_session.set(WORKSPACE_INVALID_SESSION_KEY, invalid_scope)
        if invalid_scope:
            await cl.Message(content=t("workspace_scope_resume_error")).send()
            return
        await _send_workspace_settings(raw_value, disabled=True)


if AUTH_TOKEN and AUTH_MODE != "oidc":
    if not CHAINLIT_AUTH_SECRET:
        raise RuntimeError(
            "CHAINLIT_AUTH_SECRET is required when authentication is enabled. "
            'Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"` '
            "and set it in your environment."
        )

    @cl.header_auth_callback
    async def header_auth_callback(headers: dict) -> cl.User | None:
        """Authenticate Chat from the short-lived Admin UI handoff cookie."""
        return await _chainlit_user_from_browser_cookies(headers)

    @cl.password_auth_callback
    async def auth_callback(username: str, password: str):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(4 * 60.0)) as client:
                data = await _load_user_info(client, password)
                model_ids = await _load_openrag_model_ids_for_metadata(client, password)

            return _chainlit_user_from_info(data, provider="credentials", api_key=password, model_ids=model_ids)

        except httpx.HTTPStatusError:
            logger.info("Authentication failed", username=mask_email(username))
            return None
        except Exception as e:
            logger.exception("Unexpected error during authentication", error=str(e))
            return None

elif AUTH_MODE == "oidc":
    if not CHAINLIT_AUTH_SECRET:
        raise RuntimeError(
            "CHAINLIT_AUTH_SECRET is required when AUTH_MODE=oidc. "
            'Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"` '
            "and set it in your environment."
        )

    @cl.header_auth_callback
    async def header_auth_callback(headers: dict) -> cl.User | None:
        """Authenticate Chainlit users via an OpenRAG browser auth cookie."""
        return await _chainlit_user_from_browser_cookies(headers)


@cl.on_logout
async def on_logout(request, response):
    _clear_chat_logout_cookies(request, response)
    return {"success": True}


def get_external_url():
    context = get_context()
    referer = context.session.environ.get("HTTP_REFERER", "")
    parsed_url = urlparse(referer)  # Parse the referer URL
    external_base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
    return external_base_url


@cl.set_chat_profiles
async def chat_profile(current_user: cl.User):
    try:
        api_key = _openrag_api_key_from_user_or_context(current_user)
        async with httpx.AsyncClient(timeout=httpx.Timeout(4 * 60.0)) as client:
            model_ids = await _load_openrag_model_ids(client, api_key)
        return _chat_profiles_from_model_ids(model_ids)
    except MissingOpenRAGCredentialError as e:
        cached_profiles = _cached_chat_profiles_from_user(current_user)
        if cached_profiles:
            return cached_profiles
        await _handle_missing_openrag_credential(e)
        return []
    except Exception as e:
        cached_profiles = _cached_chat_profiles_from_user(current_user)
        if cached_profiles:
            return cached_profiles
        await cl.Message(content=t("error_profiles").format(e)).send()
        return []


@cl.on_chat_start
async def on_chat_start():
    cl.user_session.set("messages", [])
    cl.user_session.set(WORKSPACE_SCOPE_SESSION_KEY, None)
    cl.user_session.set(WORKSPACE_SCOPE_VALUE_SESSION_KEY, WORKSPACE_NONE_VALUE)
    cl.user_session.set(WORKSPACE_ITEMS_SESSION_KEY, {})
    cl.user_session.set(WORKSPACE_VALUES_SESSION_KEY, {WORKSPACE_NONE_VALUE})
    cl.user_session.set(WORKSPACE_LOCKED_SESSION_KEY, False)
    cl.user_session.set(WORKSPACE_INVALID_SESSION_KEY, False)
    logger.debug("New Chat Started", internal_base_url=INTERNAL_BASE_URL)
    try:
        api_key = _current_openrag_api_key()
        async with httpx.AsyncClient(timeout=httpx.Timeout(4 * 60.0)) as client:
            response = await client.get(
                url=f"{INTERNAL_BASE_URL}/health_check",
                headers=get_headers(api_key),
            )
            print(response.text)
        commands = t("commands")
        await cl.context.emitter.set_commands(commands if isinstance(commands, list) else [])
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                items = await _load_workspace_setting_items(
                    client,
                    api_key,
                    cl.user_session.get("chat_profile"),
                )
            cl.user_session.set(WORKSPACE_ITEMS_SESSION_KEY, items)
            allowed_values = set(items.values())
            cl.user_session.set(WORKSPACE_VALUES_SESSION_KEY, allowed_values or {WORKSPACE_NONE_VALUE})
            if items:
                settings = await _send_workspace_settings()
                value = settings.get(WORKSPACE_SETTING_ID, WORKSPACE_NONE_VALUE) if settings else WORKSPACE_NONE_VALUE
                if value not in allowed_values:
                    value = WORKSPACE_NONE_VALUE
                _set_workspace_scope_value(value)
        except Exception as e:
            logger.warning("Could not load workspace choices for Chainlit", error=str(e))
            await cl.Message(content=t("workspace_scope_load_error")).send()
    except MissingOpenRAGCredentialError as e:
        await _handle_missing_openrag_credential(e)
    except Exception as e:
        logger.exception("An error occured while checking the API health", error=str(e))
        await cl.Message(content=t("error_health").format(e)).send()


@cl.on_settings_update
async def on_chat_settings_update(settings: dict):
    value = settings.get(WORKSPACE_SETTING_ID, WORKSPACE_NONE_VALUE)
    allowed_values = cl.user_session.get(WORKSPACE_VALUES_SESSION_KEY, {WORKSPACE_NONE_VALUE})
    if not isinstance(value, str) or value not in allowed_values:
        cl.user_session.set(WORKSPACE_INVALID_SESSION_KEY, True)
        await cl.Message(content=t("workspace_scope_invalid")).send()
        return

    current_value = cl.user_session.get(WORKSPACE_SCOPE_VALUE_SESSION_KEY, WORKSPACE_NONE_VALUE)
    if cl.user_session.get(WORKSPACE_LOCKED_SESSION_KEY, False) and value != current_value:
        await _send_workspace_settings(current_value, disabled=True)
        await cl.Message(content=t("workspace_scope_locked")).send()
        return

    cl.user_session.set(WORKSPACE_INVALID_SESSION_KEY, False)
    _set_workspace_scope_value(value)


async def __fetch_page_content(chunk_url, headers=None):
    async with httpx.AsyncClient() as client:
        response = await client.get(chunk_url, headers=headers)
        response.raise_for_status()  # raises exception for 4xx/5xx responses
        data = response.json()
        return data.get("page_content", "")


def _chunk_field(source: dict, key: str):
    """Read one chunk field from a source entry, old shape or new.

    The chunk's metadata is nested under ``chunk``; a server from before that
    change spreads it across the entry instead. Accept both so the UI survives a
    rolling deploy in either direction. Server-computed fields (``file_url``,
    ``chunk_url``, ``source_type``) are read from the top level in both shapes
    and must not come through here — they are authoritative there, and
    deliberately scrubbed from the nested dict.
    """
    chunk = source.get("chunk")
    if isinstance(chunk, dict) and key in chunk:
        return chunk[key]
    return source.get(key)


async def _format_sources(metadata_sources, only_txt=False, api_key=None):
    """Format retrieved web and file sources as Chainlit elements."""
    if not isinstance(metadata_sources, list) or not metadata_sources:
        return [], []

    d = {}
    headers = get_headers(api_key)
    external_url = get_external_url()  # used to override the base URL when the front-end requests a file resource
    for i, s in enumerate(metadata_sources):
        if not isinstance(s, dict):
            continue

        if s.get("source_type") == "web":
            title = s.get("title", "")
            snippet = s.get("snippet", "")
            title = title.strip() if isinstance(title, str) else ""
            snippet = snippet.strip() if isinstance(snippet, str) else ""
            url = normalize_web_url(s.get("url"))
            if url is None:
                continue
            markdown_url = quote(url, safe=_MARKDOWN_URL_SAFE_CHARS)

            source_label = title or url
            source_label = " ".join(source_label.split())
            source_name = _safe_source_name(source_label, d)
            content = f"**[{_escape_markdown_text(source_label)}]({markdown_url})**"
            if snippet:
                content += f"\n\n{_escape_markdown_text(snippet)}"
            d[source_name] = cl.Text(content=content, name=source_name, display="side")
            continue

        filename_value = _chunk_field(s, "filename")
        file_url = s.get("file_url")
        page = _chunk_field(s, "page")
        if (
            not isinstance(filename_value, str)
            or not filename_value.strip()
            or not isinstance(file_url, str)
            or not file_url.strip()
        ):
            continue

        filename = Path(filename_value.strip())
        suffix = filename.suffix.lower()
        file_url = file_url.strip()
        file_url = file_url.replace(INTERNAL_BASE_URL, external_url)  # put the correct base url
        # Avoid leaking the credential in the URL (browser history, proxy logs,
        # Referer headers). In OIDC mode the browser already sends the
        # openrag_session cookie on same-origin file fetches, which the auth
        # middleware accepts — so the token query param is unnecessary. In token
        # mode, or for a token handoff inside an OIDC deployment, there is no
        # cookie on /static, so it remains the only way for the browser to
        # authenticate the fetch.
        if api_key and (AUTH_MODE != "oidc" or _current_openrag_auth_provider() == "credentials"):
            file_url = f"{file_url}?token={api_key}"
        page_label = str(page).strip() if page is not None else ""
        source_label = f"{filename}" + (
            f" (page: {page_label})" if suffix in [".pdf", ".pptx", ".docx", ".doc"] and page_label else ""
        )
        source_name = _safe_source_name(source_label, d)

        try:
            if only_txt:
                chunk_url = s.get("chunk_url")
                if not isinstance(chunk_url, str) or not chunk_url.strip():
                    continue
                chunk_content = await __fetch_page_content(chunk_url=chunk_url, headers=headers)
                if not isinstance(chunk_content, str) or not chunk_content.strip():
                    continue
                elem = cl.Text(content=chunk_content, name=source_name, display="side")
            else:
                match suffix:
                    case ".pdf":
                        elem = cl.Pdf(
                            name=source_name,
                            url=file_url,
                            page=int(page) if page_label else None,
                            display="side",
                        )
                    case suffix if suffix in [".png", ".jpg", ".jpeg"]:
                        elem = cl.Image(name=source_name, url=file_url, display="side")
                    case ".mp4":
                        elem = cl.Video(name=source_name, url=file_url, display="side")
                    case ".mp3":
                        elem = cl.Audio(name=source_name, url=file_url, display="side")
                    case _:
                        chunk_url = s.get("chunk_url")
                        if not isinstance(chunk_url, str) or not chunk_url.strip():
                            continue
                        chunk_content = await __fetch_page_content(chunk_url=chunk_url, headers=headers)
                        if not isinstance(chunk_content, str) or not chunk_content.strip():
                            continue
                        elem = cl.Text(content=chunk_content, name=source_name, display="side")
        except (httpx.HTTPError, httpx.InvalidURL, TypeError, ValueError, AttributeError):
            logger.warning("Skipping an unavailable source", source_index=i)
            continue

        d[source_name] = elem

    source_names = list(d)
    elements = list(d.values())

    return elements, source_names


@cl.on_message
async def on_message(message: cl.Message):
    messages: list = cl.user_session.get("messages", [])
    model: str = cl.user_session.get("chat_profile")

    workspace_value = cl.user_session.get(WORKSPACE_SCOPE_VALUE_SESSION_KEY, WORKSPACE_NONE_VALUE)
    allowed_values = cl.user_session.get(WORKSPACE_VALUES_SESSION_KEY, {WORKSPACE_NONE_VALUE})
    workspace_scope = cl.user_session.get(WORKSPACE_SCOPE_SESSION_KEY)
    if (
        cl.user_session.get(WORKSPACE_INVALID_SESSION_KEY, False)
        or not isinstance(workspace_value, str)
        or workspace_value not in allowed_values
        or (workspace_value != WORKSPACE_NONE_VALUE and workspace_scope is None)
    ):
        cl.user_session.set(WORKSPACE_INVALID_SESSION_KEY, True)
        await cl.Message(content=t("workspace_scope_invalid")).send()
        return

    if workspace_scope:
        partition, workspace_id = workspace_scope
        # The API's `workspace` metadata contains only a workspace ID. Pair it
        # with its partition-specific model so duplicate IDs in All Partitions
        # are resolved unambiguously and retrieval remains inside that partition.
        model = f"{PARTITION_PREFIX}{partition}"

    if not cl.user_session.get(WORKSPACE_LOCKED_SESSION_KEY, False):
        cl.user_session.set(WORKSPACE_LOCKED_SESSION_KEY, True)
        await _send_workspace_settings(workspace_value, disabled=True)

    messages.append({"role": "user", "content": message.content})
    metadata = {
        "use_map_reduce": message.command == "DeepSearch",
        "spoken_style_answer": message.command == "SpokenStyleAnswer",
        "websearch": message.command == "WebSearch",
    }
    if workspace_scope:
        metadata["workspace"] = workspace_id
    data = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "stream": True,
        "frequency_penalty": 0.4,
        "metadata": metadata,
    }

    async with cl.Step(name=t("searching")):
        response_content = ""
        sources, elements, source_names = None, None, None
        # Create message content to display
        msg = cl.Message(content="")
        await msg.send()

        try:
            api_key = _current_openrag_api_key()
            client = AsyncOpenAI(
                base_url=f"{INTERNAL_BASE_URL}/v1",
                api_key=api_key,
            )
            # Stream the response using OpenAI client directly
            stream = await client.chat.completions.create(**data)
            async for chunk in stream:
                extra = getattr(chunk, "extra", None)
                # `extra` is a JSON object, but a server from before that change
                # sends the same payload as a JSON *string*. Accept both so the UI
                # survives a rolling deploy in either direction. Normalising here
                # also collapses the two "nothing to report" spellings a mid-stream
                # chunk can carry -- `{}` (falsy) and `"{}"` (truthy) -- into one.
                if isinstance(extra, str):
                    extra = json.loads(extra)
                if extra:
                    sources = extra.get("cited_sources")

                if chunk.choices and chunk.choices[0].delta.content:
                    token = chunk.choices[0].delta.content
                    response_content += token
                    await msg.stream_token(token)

            await msg.update()
            messages.append({"role": "assistant", "content": response_content})
            cl.user_session.set("messages", messages)

            # Show sources
            elements, source_names = await _format_sources(sources, api_key=api_key, only_txt=False)
            msg.elements = elements if elements else []
            if elements and source_names:
                s = "\n\n" + "-" * 50 + f"\n\n{t('sources_label')}: \n" + "\n".join(source_names)
                await msg.stream_token(s)
                await msg.update()
        except MissingOpenRAGCredentialError as e:
            logger.warning("OpenRAG Chat session expired during chat completion", error=str(e))
            await cl.Message(content=str(e)).send()
        except Exception as e:
            logger.exception("Error during chat completion", error=str(e))
            await cl.Message(content=t("error_chat").format(e)).send()
