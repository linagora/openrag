import os

import ray
from dotenv import dotenv_values

SHARED_ENV = os.environ.get("SHARED_ENV", None)

env_vars = dotenv_values(SHARED_ENV) if SHARED_ENV else {}
env_vars["PYTHONPATH"] = "/app/ragondin"


ray.init(dashboard_host="0.0.0.0")


import os
from enum import Enum
from pathlib import Path
from typing import Optional

import uvicorn
from components import RagPipeline
from config import load_config
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from routers.extract import router as extract_router
from routers.indexer import router as indexer_router
from routers.openai import router as openai_router
from routers.partition import router as partition_router
from routers.queue import router as queue_router
from routers.search import router as search_router
from utils.dependencies import vectordb
from utils.logger import get_logger

logger = get_logger()
config = load_config()
DATA_DIR = Path(config.paths.data_dir)

ragPipe = RagPipeline(config=config, vectordb=vectordb, logger=logger)


class Tags(Enum):
    VDB = "VectorDB operations"
    INDEXER = ("Indexer",)
    SEARCH = ("Semantic Search",)
    OPENAI = ("OpenAI Compatible API",)
    EXTRACT = ("Document extracts",)
    PARTITION = ("Partitions & files",)
    QUEUE = ("Queue management",)


class AppState:
    def __init__(self, config):
        self.config = config
        self.ragpipe = ragPipe
        self.vectordb = vectordb
        self.data_dir = Path(config.paths.data_dir)


# Read the token from env (or None if not set)
AUTH_TOKEN: Optional[str] = os.getenv("AUTH_TOKEN")

INDEXERUI_URL: Optional[str] = os.getenv("INDEXERUI_URL", None)
INDEXERUI_COMPOSE_FILE = os.getenv("INDEXERUI_COMPOSE_FILE", None)
INDEXERUI_PORT: Optional[str] = os.getenv("INDEXERUI_PORT", "3042")


security = HTTPBearer()


# Dependency to verify token
def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    if AUTH_TOKEN is None:
        return  # Auth disabled
    if token != AUTH_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or missing token"
        )


# Apply globally only if AUTH_TOKEN is set
dependencies = [Depends(verify_token)] if AUTH_TOKEN else []
app = FastAPI(dependencies=dependencies)

# Add CORS middleware
if INDEXERUI_URL and INDEXERUI_COMPOSE_FILE:
    allow_origins = [
        "http://localhost:3042",
        "http://localhost:5173",
        INDEXERUI_URL,
        f"http://localhost:{INDEXERUI_PORT}",
    ]
else:
    allow_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,  # Adjust as needed for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.state.app_state = AppState(config)
app.mount(
    "/static", StaticFiles(directory=DATA_DIR.resolve(), check_dir=True), name="static"
)


@app.get(
    "/health_check", summary="Toy endpoint to check that the api is up", dependencies=[]
)
async def health_check(request: Request):
    # TODO : Error reporting about llm and vlm
    return "RAG API is up."


WITH_CHAINLIT_UI: Optional[bool] = (
    os.getenv("WITH_CHAINLIT_UI", "true").lower() == "true"
)
WITH_OPENAI_API: Optional[bool] = os.getenv("WITH_OPENAI_API", "true").lower() == "true"


# Mount the indexer router
app.include_router(indexer_router, prefix="/indexer", tags=[Tags.INDEXER])
# Mount the extract router
app.include_router(extract_router, prefix="/extract", tags=[Tags.EXTRACT])
# Mount the search router
app.include_router(search_router, prefix="/search", tags=[Tags.SEARCH])
# Mount the partition router
app.include_router(partition_router, prefix="/partition", tags=[Tags.PARTITION])
# Mount the queue router
app.include_router(queue_router, prefix="/queue", tags=[Tags.QUEUE])

if WITH_OPENAI_API:
    # Mount the openai router
    app.include_router(openai_router, prefix="/v1", tags=[Tags.OPENAI])

if WITH_CHAINLIT_UI:
    # Mount the default front
    from chainlit.utils import mount_chainlit

    logger.debug("Mounting Chainlit UI")

    mount_chainlit(app, "./chainlit/app_front.py", path="/chainlit")
    app.include_router(
        openai_router, prefix="/v1", tags=[Tags.OPENAI]
    )  # cause chainlit uses openai api endpoints

if __name__ == "__main__":
    uvicorn.run("api:app", host="0.0.0.0", port=8080, reload=True, proxy_headers=True)
