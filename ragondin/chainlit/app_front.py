import json
import os
from pathlib import Path
from urllib.parse import urlparse

import chainlit as cl
import httpx
from chainlit.context import get_context
from openai import AsyncOpenAI
from utils.logger import get_logger

from dotenv import load_dotenv

load_dotenv()
logger = get_logger()

PERSISTENCY = os.environ.get("CHAINLIT_DATALAYER_COMPOSE", "") != ""
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")

# Chainlit authentication
CHAINLIT_AUTH_SECRET = os.environ.get("CHAINLIT_AUTH_SECRET")
CHAINLIT_USERNAME = os.environ.get("CHAINLIT_USERNAME", "OpenRAG")
CHAINLIT_PASSWORD = os.environ.get("CHAINLIT_PASSWORD", "OpenRAG2025")

headers = {
    "accept": "application/json",
    "Content-Type": "application/json",
}
if AUTH_TOKEN:
    headers["Authorization"] = f"Bearer {AUTH_TOKEN}"


if PERSISTENCY:

    @cl.on_chat_resume
    async def on_chat_resume(thread):
        pass


if CHAINLIT_AUTH_SECRET:

    @cl.password_auth_callback
    def auth_callback(username: str, password: str):
        # Fetch the user matching username from your database
        # and compare the hashed password with the value stored in the database
        if (username, password) == (CHAINLIT_USERNAME, CHAINLIT_PASSWORD):
            return cl.User(
                identifier=CHAINLIT_USERNAME,
                metadata={"role": "admin", "provider": "credentials"},
            )
        else:
            return None


def get_base_url():
    try:
        context = get_context()
        referer = context.session.http_referer
        parsed_url = urlparse(referer)  # Parse the referer URL
        base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
        return base_url
    except Exception:
        logger.exception("Error retrieving Chainlit context")
        port = os.environ.get("APP_iPORT", "8080")
        return f"http://localhost:{port}"  # Default fallback URL


@cl.set_chat_profiles
async def chat_profile():
    base_url = get_base_url()
    client = AsyncOpenAI(
        base_url=f"{base_url}/v1",
        api_key=AUTH_TOKEN if AUTH_TOKEN else "sk-1234",
    )
    try:
        output = await client.models.list()
        models = output.data
        chat_profiles = []
        for i, m in enumerate(models, start=1):
            partition = m.id.split("openrag-")[1]
            description_template = "You are interacting with the **{name}** LLM.\n" + (
                "The LLM's answers will be grounded on **all** partitions."
                if "all" in m.id
                else "The LLM's answers will be grounded only on the partition named **{partition}**."
            )
            chat_profiles.append(
                cl.ChatProfile(
                    name=m.id,
                    markdown_description=description_template.format(
                        name=m.id, partition=partition
                    ),
                    icon=f"https://picsum.photos/{250 + i}",
                )
            )
        return chat_profiles
    except Exception as e:
        await cl.Message(content=f"An error occured: {str(e)}").send()


@cl.on_chat_start
async def on_chat_start():
    base_url = get_base_url()
    cl.user_session.set("messages", [])
    logger.debug("New Chat Started", base_url=base_url)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout=httpx.Timeout(4 * 60.0)), headers=headers
        ) as client:
            response = await client.get(url=f"{base_url}/health_check", headers=headers)
            print(response.text)
    except Exception:
        logger.exception("An error occured while checking the API health")
        logger.warning("Make sur the fastapi is up!!")
    cl.user_session.set("BASE URL", base_url)


async def __fetch_page_content(chunk_url):
    async with httpx.AsyncClient() as client:
        response = await client.get(chunk_url, headers=headers)
        response.raise_for_status()  # raises exception for 4xx/5xx responses
        data = response.json()
        return data.get("page_content", "")


async def __format_sources(metadata_sources, only_txt=False):
    if not metadata_sources:
        return None, None

    d = {}
    for i, s in enumerate(metadata_sources):
        filename = Path(s["filename"])
        file_url = s["file_url"]
        page = s["page"]

        source_name = f"{filename}" + (
            f" (page: {page})"
            if filename.suffix in [".pdf", ".pptx", ".docx", ".doc"]
            else ""
        )

        if only_txt:
            chunk_content = await __fetch_page_content(chunk_url=s["chunk_url"])
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
                    chunk_content = await __fetch_page_content(chunk_url=s["chunk_url"])
                    elem = cl.Text(
                        content=chunk_content, name=source_name, display="side"
                    )

            d[source_name] = elem

    source_names = list(d.keys())
    elements = list(d.values())

    return elements, source_names


@cl.on_message
async def on_message(message: cl.Message):
    messages: list = cl.user_session.get("messages", [])
    model: str = cl.user_session.get("chat_profile")

    base_url = get_base_url()
    client = AsyncOpenAI(
        base_url=f"{base_url}/v1",
        api_key=AUTH_TOKEN if AUTH_TOKEN else "sk-1234",
    )

    messages.append({"role": "user", "content": message.content})
    data = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "stream": True,
        "frequency_penalty": 0.4,
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
                if sources is None:
                    extra = json.loads(chunk.extra)
                    sources = extra["sources"]

                if chunk.choices and chunk.choices[0].delta.content:
                    token = chunk.choices[0].delta.content
                    response_content += token
                    await msg.stream_token(token)

            await msg.update()
            messages.append({"role": "assistant", "content": response_content})
            cl.user_session.set("messages", messages)

            # Show sources
            elements, source_names = await __format_sources(sources)
            msg.elements = elements if elements else []
            if source_names:
                s = "\n\n" + "-" * 50 + "\n\nSources: \n" + "\n".join(source_names)
                await msg.stream_token(s)
                await msg.update()
        except Exception as e:
            logger.exception("Error during chat completion")
            await cl.Message(content=f"An error occurred: {str(e)}").send()


if __name__ == "__main__":
    from chainlit.cli import run_chainlit

    run_chainlit(__file__)
