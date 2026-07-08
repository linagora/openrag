"""Register VLM implementations with the core registry."""


def register_vlms() -> None:
    import services.inference.vllm_client  # noqa: F401
