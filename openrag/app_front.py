import json
import os
import secrets
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import chainlit as cl
import httpx
from chainlit.config import config as cl_config
from chainlit.context import get_context
from consts import PARTITION_PREFIX
from core.auth.chainlit import CHAINLIT_TOKEN_COOKIE_NAME
from core.utils.logging import get_logger, mask_email
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
_OPENRAG_TOKEN_STORE: dict[str, tuple[str, float]] = {}


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


def _openrag_api_key_from_context_cookie() -> str | None:
    try:
        context = get_context()
        cookie_header = context.session.environ.get("HTTP_COOKIE", "")
    except Exception:
        return None
    return _extract_cookie(cookie_header, "openrag_session") or _extract_cookie(
        cookie_header, CHAINLIT_TOKEN_COOKIE_NAME
    )


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
    api_key = _openrag_api_key_from_context_cookie()
    if api_key:
        cl.user_session.set(OPENRAG_API_KEY_SESSION_KEY, api_key)
        return api_key
    return default


def _current_openrag_auth_provider() -> str | None:
    user = cl.user_session.get("user")
    return _openrag_auth_provider_from_user(user)


def _chainlit_user_from_info(data: dict, *, provider: str, api_key: str) -> cl.User:
    metadata = {
        "role": "admin" if data.pop("is_admin", False) else "user",
        "provider": provider,
        "extra": data,
    }
    auth_handle = _remember_openrag_api_key(api_key)
    if auth_handle:
        metadata[OPENRAG_AUTH_HANDLE_METADATA_KEY] = auth_handle

    return cl.User(
        identifier=data.get("display_name", "user"),
        metadata=metadata,
    )


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
    except httpx.HTTPStatusError as e:
        logger.info("Session cookie rejected by /users/info", status=e.response.status_code)
        return None
    except Exception as e:
        logger.exception("Chainlit header_auth_callback failure", error=str(e))
        return None

    return _chainlit_user_from_info(data, provider=provider, api_key=api_key)


if PERSISTENCY:

    @cl.on_chat_resume
    async def on_chat_resume(thread):
        pass


if AUTH_TOKEN and AUTH_MODE != "oidc":
    if not CHAINLIT_AUTH_SECRET:
        raise RuntimeError(
            "CHAINLIT_AUTH_SECRET is required when authentication is enabled. "
            'Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"` '
            "and set it in your environment."
        )

    @cl.password_auth_callback
    async def auth_callback(username: str, password: str):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(4 * 60.0)) as client:
                data = await _load_user_info(client, password)

            return _chainlit_user_from_info(data, provider="credentials", api_key=password)

        except httpx.HTTPStatusError:
            logger.info("Authentication failed", username=mask_email(username))
            return None
        except Exception as e:
            logger.exception("Unexpected error during authentication", error=str(e))
            return None

    @cl.header_auth_callback
    async def header_auth_callback(headers: dict) -> cl.User | None:
        return await _chainlit_user_from_browser_cookies(headers)

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


def get_external_url():
    context = get_context()
    referer = context.session.environ.get("HTTP_REFERER", "")
    parsed_url = urlparse(referer)  # Parse the referer URL
    external_base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
    return external_base_url


@cl.set_chat_profiles
async def chat_profile(current_user: cl.User):
    api_key = _openrag_api_key_from_user_or_context(current_user)
    client = AsyncOpenAI(base_url=f"{INTERNAL_BASE_URL}/v1", api_key=api_key)
    try:
        output = await client.models.list()
        models = output.data
        chat_profiles = []
        for i, m in enumerate(models, start=1):
            partition = m.id.split(PARTITION_PREFIX)[1]
            description_key = "profile_description_all" if partition == "all" else "profile_description_partition"
            description_template = t(description_key)
            chat_profiles.append(
                cl.ChatProfile(
                    name=m.id,
                    markdown_description=description_template.format(name=m.id, partition=partition),
                    icon="/public/favicon.svg",
                    default=m.id == f"{PARTITION_PREFIX}all",
                )
            )
        return chat_profiles
    except Exception as e:
        await cl.Message(content=t("error_profiles").format(e)).send()


@cl.on_chat_start
async def on_chat_start():
    cl.user_session.set("messages", [])
    api_key = _current_openrag_api_key()
    logger.debug("New Chat Started", internal_base_url=INTERNAL_BASE_URL)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(4 * 60.0)) as client:
            response = await client.get(
                url=f"{INTERNAL_BASE_URL}/health_check",
                headers=get_headers(api_key),
            )
            print(response.text)
        commands = t("commands")
        await cl.context.emitter.set_commands(commands if isinstance(commands, list) else [])
    except Exception as e:
        logger.exception("An error occured while checking the API health", error=str(e))
        await cl.Message(content=t("error_health").format(e)).send()


async def __fetch_page_content(chunk_url, headers=None):
    async with httpx.AsyncClient() as client:
        response = await client.get(chunk_url, headers=headers)
        response.raise_for_status()  # raises exception for 4xx/5xx responses
        data = response.json()
        return data.get("page_content", "")


async def _format_sources(metadata_sources, only_txt=False, api_key=None):
    external_url = get_external_url()  # used to override the base URL when the front-end requests a file resource
    if not metadata_sources:
        return None, None

    d = {}
    headers = get_headers(api_key)
    for i, s in enumerate(metadata_sources):
        if s.get("source_type") == "web":
            title = s.get("title") or s.get("url", f"Web source {i + 1}")
            url = s.get("url", "")
            snippet = s.get("snippet", "")
            content = f"**[{title}]({url})**\n\n{snippet}"
            source_name = title
            if source_name in d:
                source_name = f"{title} ({i})"
            d[source_name] = cl.Text(content=content, name=source_name, display="side")
            continue

        filename = Path(s["filename"])
        file_url = s["file_url"]
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
        page = s["page"]
        source_name = f"{filename}" + (
            f" (page: {page})" if filename.suffix in [".pdf", ".pptx", ".docx", ".doc"] else ""
        )

        if only_txt:
            chunk_content = await __fetch_page_content(chunk_url=s["chunk_url"], headers=headers)
            elem = cl.Text(content=chunk_content, name=source_name, display="side")
        else:
            match filename.suffix.lower():
                case ".pdf":
                    elem = cl.Pdf(
                        name=source_name,
                        url=file_url,
                        page=int(s["page"]),
                        display="side",
                    )
                case suffix if suffix in [".png", ".jpg", ".jpeg"]:
                    elem = cl.Image(name=source_name, url=file_url, display="side")
                case ".mp4":
                    elem = cl.Video(name=source_name, url=file_url, display="side")
                case ".mp3":
                    elem = cl.Audio(name=source_name, url=file_url, display="side")
                case _:
                    chunk_content = await __fetch_page_content(chunk_url=s["chunk_url"], headers=headers)
                    elem = cl.Text(content=chunk_content, name=source_name, display="side")

        d[source_name] = elem

    source_names = list(d.keys())
    elements = list(d.values())

    return elements, source_names


@cl.on_message
async def on_message(message: cl.Message):
    messages: list = cl.user_session.get("messages", [])
    model: str = cl.user_session.get("chat_profile")
    api_key = _current_openrag_api_key()
    client = AsyncOpenAI(
        base_url=f"{INTERNAL_BASE_URL}/v1",
        api_key=api_key,
    )

    messages.append({"role": "user", "content": message.content})
    data = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "stream": True,
        "frequency_penalty": 0.4,
        "metadata": {
            "use_map_reduce": message.command == "DeepSearch",
            "spoken_style_answer": message.command == "SpokenStyleAnswer",
            "websearch": message.command == "WebSearch",
        },
    }

    async with cl.Step(name=t("searching")):
        response_content = ""
        sources, elements, source_names = None, None, None
        # Create message content to display
        msg = cl.Message(content="")
        await msg.send()

        try:
            # Stream the response using OpenAI client directly
            stream = await client.chat.completions.create(**data)
            async for chunk in stream:
                if chunk.extra:
                    extra = json.loads(chunk.extra)
                    if "sources" in extra:
                        sources = extra["sources"]

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
            if source_names:
                s = "\n\n" + "-" * 50 + f"\n\n{t('sources_label')}: \n" + "\n".join(source_names)
                await msg.stream_token(s)
                await msg.update()
        except Exception as e:
            logger.exception("Error during chat completion", error=str(e))
            await cl.Message(content=t("error_chat").format(e)).send()
