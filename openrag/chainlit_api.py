from api.middleware.auth import AuthMiddleware
from api.routers.user.download import router as download_router
from chainlit.utils import mount_chainlit
from fastapi import FastAPI


def _get_auth_service(request):
    container = getattr(request.app.state, "container", None)
    if container is None:
        from core.config import load_config
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

# Ray Serve mode runs the API and Chainlit on separate ports. Source previews
# rewrite their file download links to the browser origin (the Chainlit host),
# so this standalone Chainlit app must also expose the authorized,
# partition-checked source-download route (/static/{extract_id}) or those
# links would 404. In the mounted (/chainlit) deployment the UI shares the
# API's origin, which already serves this route.
app.include_router(download_router)

mount_chainlit(app=app, target="./app_front.py", path="/chainlit")
