"""Register embedder implementations with the core registry."""


def register_embedders() -> None:
    import services.inference.ollama_client  # noqa: F401
    import services.inference.vllm_client  # noqa: F401
