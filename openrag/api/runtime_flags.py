"""Runtime feature flags shared by the API routers."""

from __future__ import annotations

import os


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() == "true"


WITH_CHAINLIT_UI: bool = env_bool("WITH_CHAINLIT_UI", True)
WITH_OPENAI_API: bool = env_bool("WITH_OPENAI_API", True)
