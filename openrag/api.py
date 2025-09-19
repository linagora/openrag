import os

import ray
from dotenv import dotenv_values

SHARED_ENV = os.environ.get("SHARED_ENV", None)

env_vars = dotenv_values(SHARED_ENV) if SHARED_ENV else {}
env_vars["PYTHONPATH"] = "/app/openrag"


ray.init(dashboard_host="0.0.0.0")


import os
from enum import Enum
from pathlib import Path
from typing import Optional

import uvicorn
from config import load_config
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from routers.actors import router as actors_router
from routers.extract import router as extract_router
from routers.indexer import router as indexer_router
from routers.openai import router as openai_router
from routers.partition import router as partition_router
from routers.queue import router as queue_router
from routers.search import router as search_router
from utils.exceptions import OpenRAGError
from utils.logger import get_logger

logger = get_logger()
config = load_config()
DATA_DIR = Path(config.paths.data_dir)


class Tags(Enum):
    VDB = "VectorDB operations"
    INDEXER = ("Indexer",)
    SEARCH = ("Semantic Search",)
    OPENAI = ("OpenAI Compatible API",)
    EXTRACT = ("Document extracts",)
    PARTITION = ("Partitions & files",)
    QUEUE = ("Queue management",)
    ACTORS = ("Ray Actors",)


class AppState:
    def __init__(self, config):
        self.config = config
        self.data_dir = Path(config.paths.data_dir)


# Read the token from env (or None if not set)
AUTH_TOKEN: Optional[str] = os.getenv("AUTH_TOKEN")

INDEXERUI_URL: Optional[str] = os.getenv("INDEXERUI_URL", None)
INDEXERUI_COMPOSE_FILE = os.getenv("INDEXERUI_COMPOSE_FILE", None)
INDEXERUI_PORT: Optional[str] = os.getenv("INDEXERUI_PORT", "3042")

DISABLE_EXCEPTION_HANDLER: bool = (
    os.getenv("DISABLE_EXCEPTION_HANDLER", "false").lower() == "true"
)

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


# Exception handlers
if not DISABLE_EXCEPTION_HANDLER:

    @app.exception_handler(OpenRAGError)
    async def openrag_exception_handler(request: Request, exc: OpenRAGError):
        logger.error("OpenRAGError occurred", error=str(exc))
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())


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
# Mount the actors router
app.include_router(actors_router, prefix="/actors", tags=[Tags.ACTORS])

if WITH_OPENAI_API:
    # Mount the openai router
    app.include_router(openai_router, prefix="/v1", tags=[Tags.OPENAI])

if WITH_CHAINLIT_UI:
    # Mount the default front
    from chainlit.utils import mount_chainlit

    mount_chainlit(app, "./app_front.py", path="/chainlit")
    app.include_router(
        openai_router, prefix="/v1", tags=[Tags.OPENAI]
    )  # cause chainlit uses openai api endpoints

if __name__ == "__main__":
    if config.ray.serve.enable:
        from ray import serve

        @serve.deployment(num_replicas=config.ray.serve.num_replicas)
        @serve.ingress(app)
        class OpenRagAPI:
            pass

        serve.start(
            http_options={"host": config.ray.serve.host, "port": config.ray.serve.port}
        )
        serve.run(OpenRagAPI.bind(), route_prefix="/", blocking=True)

    else:
        uvicorn.run(
            "api:app", host="0.0.0.0", port=8080, reload=True, proxy_headers=True
        )
