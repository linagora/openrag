import json
import os
from pathlib import Path
from urllib.parse import urlparse

import chainlit as cl
import httpx
from chainlit.context import get_context
from consts import PARTITION_PREFIX
from dotenv import load_dotenv
from openai import AsyncOpenAI
from utils.logger import get_logger

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

commands = [
    {
        "id": "DeepSearch",
        "icon": "brain-cog",
        "description": "This uses a custom DeepSearch RAG mechanism (Map & Reduce) to handle complex queries.\nSlower but gives accurate answers.\nUse in an empty context as it consumes more tokens.",
    },
    {
        "id": "SpokenStyleAnswer",
        "icon": "audio-lines",
        "description": "Get a conversational text answer suitable for voice assistants.\nThe answer is concise, clear, and factual.",
        "persistent": True,
    },
    {
        "id": "WebSearch",
        "icon": "globe",
        "description": "Augment the RAG context with live web search results.\nCombines document and web sources for more comprehensive answers.",
    },
]


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


if PERSISTENCY:

    @cl.on_chat_resume
    async def on_chat_resume(thread):
        pass


if AUTH_TOKEN and AUTH_MODE != "oidc":
    if not CHAINLIT_AUTH_SECRET:
        # logger.warning(
        #     "`CHAINLIT_AUTH_SECRET` is not set a default value will be used. Not recommended for production."
        # )
        os.environ["CHAINLIT_AUTH_SECRET"] = "default_secret_for_openrag_ui"  # Set default value

    @cl.password_auth_callback
    async def auth_callback(username: str, password: str):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(4 * 60.0)) as client:
                response = await client.get(
                    url=f"{INTERNAL_BASE_URL}/users/info",
                    headers=get_headers(password),
                )
                response.raise_for_status()  # raises exception for 4xx/5xx responses
                data = response.json()

            return cl.User(
                identifier=data.get("display_name", "user"),
                metadata={
                    "role": "admin" if data.pop("is_admin") else "user",
                    "provider": "credentials",
                    "api_key": password,
                    "extra": data,
                },
            )

        except httpx.HTTPStatusError:
            logger.info("Authentication failed", username=username)
            return None
        except Exception as e:
            logger.exception("Unexpected error during authentication", error=str(e))
            return None

elif AUTH_MODE == "oidc":
    if not CHAINLIT_AUTH_SECRET:
        os.environ["CHAINLIT_AUTH_SECRET"] = "default_secret_for_openrag_ui"

    @cl.header_auth_callback
    async def header_auth_callback(headers: dict) -> cl.User | None:
        """Authenticate Chainlit users via the openrag_session cookie posted by /auth/callback."""
        cookie_header = headers.get("cookie") or headers.get("Cookie") or ""
        session_token = _extract_cookie(cookie_header, "openrag_session")
        if not session_token:
            logger.info("No openrag_session cookie in Chainlit request")
            return None

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                response = await client.get(
                    url=f"{INTERNAL_BASE_URL}/users/info",
                    headers=get_headers(session_token),
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as e:
            logger.info("Session cookie rejected by /users/info", status=e.response.status_code)
            return None
        except Exception as e:
            logger.exception("Chainlit header_auth_callback failure", error=str(e))
            return None

        return cl.User(
            identifier=data.get("display_name", "user"),
            metadata={
                "role": "admin" if data.pop("is_admin", False) else "user",
                "provider": "oidc",
                "api_key": session_token,  # opaque cookie value — used as Bearer for internal calls
                "extra": data,
            },
        )


def get_external_url():
    context = get_context()
    referer = context.session.environ.get("HTTP_REFERER", "")
    parsed_url = urlparse(referer)  # Parse the referer URL
    external_base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
    return external_base_url


@cl.set_chat_profiles
async def chat_profile(current_user: cl.User):
    api_key = current_user.metadata.get("api_key", "sk-1234") if current_user else "sk-1234"
    client = AsyncOpenAI(base_url=f"{INTERNAL_BASE_URL}/v1", api_key=api_key)
    try:
        output = await client.models.list()
        models = output.data
        chat_profiles = []
        for i, m in enumerate(models, start=1):
            partition = m.id.split(PARTITION_PREFIX)[1]
            description_template = "You are interacting with the **{name}** LLM.\n" + (
                "The LLM's answers will be grounded on **all** partitions."
                if "all" in m.id
                else "The LLM's answers will be grounded only on the partition named **{partition}**."
            )
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
        await cl.Message(content=f"An error occured: {e!s}").send()


@cl.on_chat_start
async def on_chat_start():
    cl.user_session.set("messages", [])
    user = cl.user_session.get("user")
    api_key = user.metadata.get("api_key", "sk-1234") if user else "sk-1234"
    logger.debug("New Chat Started", internal_base_url=INTERNAL_BASE_URL)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(4 * 60.0)) as client:
            response = await client.get(
                url=f"{INTERNAL_BASE_URL}/health_check",
                headers=get_headers(api_key),
            )
            print(response.text)
        await cl.context.emitter.set_commands(commands)
    except Exception as e:
        logger.exception("An error occured while checking the API health", error=str(e))
        await cl.Message(content=f"An error occured while checking the API health: {e!s}").send()


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
        file_url = f"{file_url}?token={api_key}"  # add token for authentication
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
    user = cl.user_session.get("user")
    api_key = user.metadata.get("api_key") if user else "sk-1234"
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

    async with cl.Step(name="Searching for relevant documents..."):
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
                s = "\n\n" + "-" * 50 + "\n\nSources: \n" + "\n".join(source_names)
                await msg.stream_token(s)
                await msg.update()
        except Exception as e:
            logger.exception("Error during chat completion", error=str(e))
            await cl.Message(content=f"An error occurred: {e}").send()
