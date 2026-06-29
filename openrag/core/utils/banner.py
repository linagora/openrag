"""Startup banner.

Renders the OpenRAG ASCII logo + version once the FastAPI lifespan has
finished booting — the vLLM-style "the server is up" splash.

Printed straight to the stream (default ``sys.stderr``) rather than through
loguru: the structured log formatter (:mod:`core.utils.logging`) wraps each
record with its own level/location markup and would mangle the figlet art.

Color (a per-line true-color gradient) is on by default and suppressed only by
``NO_COLOR`` / ``TERM=dumb`` — matching the logger sink, which now forces ANSI
on even when stderr isn't a TTY (``docker compose up``). Set
``OPENRAG_BANNER=false`` to suppress the banner entirely.

The art is the ``speed`` figlet font, hardcoded so there is no runtime
``pyfiglet`` dependency. It is pure ASCII, so it renders identically on any
terminal — the plain fallback (no gradient) is only taken when color is
disabled.
"""

from __future__ import annotations

import os
import sys
from importlib.metadata import version as _pkg_version

# "OpenRAG" in the speed figlet font (pure ASCII, bold slanted strokes).
_LOGO_LINES = (
    r"_______                    ________________________",
    r"__  __ \______________________  __ \__    |_  ____/",
    r"_  / / /__  __ \  _ \_  __ \_  /_/ /_  /| |  / __",
    r"/ /_/ /__  /_/ /  __/  / / /  _, _/_  ___ / /_/ /",
    r"\____/ _  .___/\___//_/ /_//_/ |_| /_/  |_\____/",
    r"       /_/",
)

# Gradient endpoints (R, G, B): the OpenRAG brand crimson, swept top to bottom.
# Sourced from the indexer-ui's ``--color-linagora-*`` scale (src/app.css); 500
# (#c71f45) is the logo circle fill in src/lib/icons/OpenRAG.svelte.
_GRAD_START = (0xC7, 0x1F, 0x45)  # linagora-500 — brand red (logo fill)
_GRAD_END = (0x83, 0x13, 0x2D)  # linagora-800 — deep crimson

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"


def _supports_color() -> bool:
    """Color on by default, opted out only via ``NO_COLOR`` / ``TERM=dumb``.

    Deliberately not gated on ``isatty()``: OpenRAG usually runs with its
    stderr piped (``docker compose up``, log capture), and the logger sink
    already forces ANSI there — the banner matches so the splash isn't the one
    colorless thing in an otherwise colored boot log.
    """
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return True


def _disabled() -> bool:
    return os.environ.get("OPENRAG_BANNER", "true").strip().lower() in {"0", "false", "no", "off"}


def _fg(rgb: tuple[int, int, int]) -> str:
    return f"\033[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def _gradient(i: int, n: int) -> str:
    """24-bit foreground escape for row ``i`` of ``n``, interpolated start->end."""
    t = i / (n - 1) if n > 1 else 0.0
    rgb = tuple(round(a + (b - a) * t) for a, b in zip(_GRAD_START, _GRAD_END))
    return _fg(rgb)  # type: ignore[arg-type]


def print_startup_banner(version: str | None = None, *, stream=None) -> None:
    """Print the OpenRAG logo, version, and the local API docs URL.

    Safe to call unconditionally from the lifespan: it no-ops when
    ``OPENRAG_BANNER`` is falsy and never raises (a banner must not be able
    to break boot).
    """
    if _disabled():
        return
    stream = stream or sys.stderr

    if version is None:
        try:
            version = _pkg_version("openrag")
        except Exception:
            version = "unknown"

    port = os.environ.get("APP_PORT", "8080")
    docs_url = f"http://localhost:{port}/docs"

    if _supports_color():
        n = len(_LOGO_LINES)
        logo = "\n".join(f"{_BOLD}{_gradient(i, n)}{line}{_RESET}" for i, line in enumerate(_LOGO_LINES))
        meta = (
            f"  {_BOLD}{_fg(_GRAD_START)}OpenRAG{_RESET} {_DIM}v{version}{_RESET}"
            f"   {_DIM}API docs:{_RESET} {_fg(_GRAD_START)}{docs_url}{_RESET}"
        )
    else:
        logo = "\n".join(_LOGO_LINES)
        meta = f"  OpenRAG v{version}   API docs: {docs_url}"

    try:
        stream.write(f"\n{logo}\n\n{meta}\n\n")
        stream.flush()
    except Exception:  # pragma: no cover - a banner must never break boot
        pass
