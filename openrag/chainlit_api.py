from chainlit.utils import mount_chainlit
from components.auth.middleware import AuthMiddleware
from fastapi import FastAPI
from utils.dependencies import get_vectordb

app = FastAPI()
app.add_middleware(AuthMiddleware, get_vectordb=get_vectordb)

mount_chainlit(app=app, target="./app_front.py", path="/chainlit")
