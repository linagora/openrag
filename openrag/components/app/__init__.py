from .interfaces import OpenRAGApiInterface

__all__ = ["OpenRAGApiInterface", "OpenRAGApplicationService"]


def __getattr__(name: str):
    if name == "OpenRAGApplicationService":
        from .service import OpenRAGApplicationService  # noqa: PLC0415

        return OpenRAGApplicationService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
