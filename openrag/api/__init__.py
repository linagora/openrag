"""Compatibility entrypoint for ASGI loaders using ``api:app``."""


def __getattr__(name: str):
    if name == "app":
        from api.main import app

        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = ["app"]
