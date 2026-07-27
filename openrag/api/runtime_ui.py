"""Runtime configuration exposed to the browser-facing Admin UI."""

from __future__ import annotations

import os
from urllib.parse import urlparse


def get_grafana_url() -> str | None:
    """Return a safe Grafana destination configured for this deployment."""
    value = os.getenv("GRAFANA_URL", "").strip()
    if not value:
        return None

    if value.startswith("/") and not value.startswith("//"):
        return value

    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value

    return None
