"""Register reranker implementations with the core registry."""


def register_rerankers() -> None:
    import services.inference.reranker_clients  # noqa: F401
