from api.middleware.auth import AuthMiddleware
from chainlit.utils import mount_chainlit
from fastapi import FastAPI


def _get_auth_service(request):
    container = getattr(request.app.state, "container", None)
    if container is None:
        from config import load_config
        from di.container import ServiceContainer

        container = ServiceContainer(load_config())
        request.app.state.container = container
    return container.auth_service


app = FastAPI()
app.state.container = None
app.add_middleware(
    AuthMiddleware,
    get_auth_service=_get_auth_service,
)

mount_chainlit(app=app, target="./app_front.py", path="/chainlit")
