from .settings import Settings, get_settings


def load_config(config_path=None, overrides=None) -> Settings:
    """Return the cached Pydantic Settings singleton.

    The ``config_path`` and ``overrides`` parameters are kept for backward
    compatibility but are no longer used — all configuration is read from
    environment variables (with sensible defaults).
    """
    return get_settings()
