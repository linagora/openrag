import inspect
import json
import logging
import sys
import traceback

from core.config import load_config
from loguru import logger


def escape_markup(s: str) -> str:
    return s.replace("\\", "\\\\").replace("<", "\\<").replace(">", "\\>")


def mask_email(email: str | None) -> str:
    """Mask an email address for logging — keep the first local character and
    the domain, e.g. ``alice@example.com`` -> ``a***@example.com``.

    The domain is retained so an operator can still tell which tenant/IdP an
    entry relates to; the local part (the personal identifier) is redacted.
    Non-string or malformed input returns ``"***"`` so a raw address can never
    reach the logs by accident.
    """
    if not isinstance(email, str) or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    masked_local = f"{local[0]}***" if local else "***"
    return f"{masked_local}@{domain}"


def terminal_formatter(record) -> str:
    """Build the loguru template for the colorized stderr sink.

    ``format`` is a callable, so loguru parses the *returned string* as a
    template — for color markup (``<...>``) AND field placeholders (``{...}``).
    Keep the record fields as placeholders ({level}, {name}, …): loguru
    substitutes those values after parsing, so a function named ``<module>`` /
    ``<lambda>`` or a message containing ``{}`` is inserted safely. (Splicing
    them in raw is what raised "Tag <module> does not correspond to any known
    color directive" and dropped those records.) Only the bound ``extra``
    values are literal template text, so they must escape both markup
    (escape_markup) and braces.

    A callable ``format`` also means loguru does NOT auto-append its usual
    ``"\\n{exception}"`` — that convenience only applies to string formats — so
    the trailing ``{exception}`` placeholder is required here, otherwise
    ``logger.exception()`` / ``opt(exception=True)`` records lose their
    traceback. It renders to an empty string when no exception is attached.
    """
    extra = " | ".join(f"{escape_markup(k)}={escape_markup(str(v))}" for k, v in record["extra"].items())
    suffix = f" [{extra}]".replace("{", "{{").replace("}", "}}") if extra else ""
    return "<level>{level: <8}</level> | <cyan>{name}:{function}:{line}</cyan> - {message}" + suffix + "\n{exception}"


# Keys the JSON sink owns, in emission order. A bound ``extra`` with one of
# these names is emitted as ``extra_<name>`` so it can never shadow them.
RESERVED_JSON_KEYS: tuple[str, ...] = ("ts", "level", "logger", "function", "line", "msg", "exception")


def json_record(record: dict) -> dict:
    """Flatten a loguru record into the one-object-per-line shape Loki parses.

    Pure and side-effect free so tests can assert on it without a sink. The
    ``extra`` mapping is lifted to the top level: collectors index
    ``request_id`` / ``task_id`` directly instead of digging into a nested
    ``record.extra`` (the shape loguru's ``serialize=True`` produced).

    ``logger_name`` is a *consumed* extra, reserved for
    :class:`InterceptHandler`: it is popped rather than emitted, and its value
    becomes the ``logger`` field so an intercepted stdlib record keeps its
    origin (``uvicorn.access``) instead of reporting this module. Code that
    binds ``logger_name`` for its own purposes will see it disappear from the
    payload — bind another name.
    """
    extra = dict(record["extra"])
    logger_name = extra.pop("logger_name", None) or record["name"]
    payload = {
        "ts": record["time"].isoformat(),
        "level": record["level"].name,
        "logger": logger_name,
        "function": record["function"],
        "line": record["line"],
        "msg": record["message"],
    }
    exc = record["exception"]
    # ``exc.type is None`` as well as ``exc is None``: loguru builds a
    # RecordException for any non-empty exc_info tuple, and stdlib hands it
    # ``(None, None, None)`` for ``logger.error(..., exc_info=True)`` called
    # outside an ``except`` block — a shape every intercepted library can
    # produce. ``format_exception(None, None, None)`` returns
    # ``"NoneType: None\n"``, which would render in Grafana as a traceback on
    # a record that has none.
    if exc is not None and exc.type is not None:
        payload["exception"] = "".join(traceback.format_exception(exc.type, exc.value, exc.traceback))
    for key, value in extra.items():
        payload[f"extra_{key}" if key in RESERVED_JSON_KEYS else key] = value
    return payload


def json_sink(message) -> None:
    """Loguru sink: one JSON object per line on stderr, no colour.

    A *sink* rather than a ``format`` callable: loguru parses the string a
    ``format`` returns as a template (markup + ``{}`` placeholders), which a
    JSON document full of braces would break. ``default=str`` keeps a bound
    Path/exception/dataclass from raising inside the logger.
    """
    sys.stderr.write(json.dumps(json_record(message.record), default=str) + "\n")


class InterceptHandler(logging.Handler):
    """Forward stdlib ``logging`` records to loguru (the loguru-documented recipe).

    Installed only in JSON mode so uvicorn, Ray and Chainlit lines come out
    as JSON like everything else; in text mode their own handlers stay.

    It sits on the *root* logger, so it catches every library that propagates
    — which is most of them. Chainlit, for instance, is covered because its
    logger adds no handler of its own and propagates to the root, not because
    it appears in :data:`_STDLIB_LOGGERS_TO_FLATTEN`; that tuple exists only
    for the few libraries that set ``propagate = False``.
    """

    def emit(self, record: logging.LogRecord) -> None:
        # Mirror stdlib's own handlers: anything raised while rendering the
        # record (a library's mis-formatted ``%d`` call, say) goes through
        # ``handleError`` — printed, never propagated into the caller. Without
        # this, a format bug text mode tolerates becomes an exception in
        # production JSON mode.
        try:
            try:
                level: str | int = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno
            # Walk out of the ``logging`` machinery to the frame that called
            # it, so loguru reports the real call site rather than this
            # handler. Start at this ``emit`` frame with depth 0
            # (``inspect.currentframe()``, NOT ``logging.currentframe()`` — the
            # latter is ``sys._getframe(3)``, which on CPython 3.12 already
            # lands past the ``depth == 0`` guard and leaves every line
            # reporting ``callHandlers`` at logging/__init__.py).
            frame, depth = inspect.currentframe(), 0
            while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
                frame = frame.f_back
                depth += 1
            message = record.getMessage()
            logger.bind(logger_name=record.name).opt(depth=depth, exception=record.exc_info).log(level, message)
        except Exception:
            self.handleError(record)


# Loggers that libraries configure with their own handlers and
# ``propagate=False`` at import (or, for Ray Serve, in every replica), which
# would otherwise keep printing text next to the JSON stream.
_STDLIB_LOGGERS_TO_FLATTEN = ("uvicorn", "uvicorn.error", "uvicorn.access", "ray", "ray.serve")

# Libraries that log one line per operation at INFO/DEBUG: asyncio's "Using
# selector", httpx/httpcore's request URLs, urllib3's connection pool chatter,
# openai's ``Request options`` dump (the full outbound prompt and retrieved
# chunks). In text mode their own (absent) handlers meant they printed
# nothing; once the root is intercepted they would all ship at LOG_LEVEL, so
# they are capped at WARNING to keep the collector's volume — and what it
# contains — sane. Only loggers still at NOTSET are touched, so an operator's
# explicit ``setLevel`` survives.
_STDLIB_LOGGERS_QUIETED = ("asyncio", "httpcore", "httpx", "urllib3", "openai")


def _stdlib_level(level: str) -> int:
    """Numeric stdlib level for a loguru level name (``TRACE`` -> 5).

    stdlib accepts any int, so loguru-only names map fine; an unknown name
    falls back to ``NOTSET`` rather than raising at import.
    """
    try:
        return logger.level(level).no
    except ValueError:
        return logging.NOTSET


def intercept_stdlib_logging(level: str) -> None:
    """Route stdlib ``logging`` into loguru. Idempotent: safe on every call.

    ``get_logger()`` runs at import in ~70 modules and in error branches at
    runtime, so this must be cheap to repeat and must not undo state it did
    not create:

    1. Exactly one :class:`InterceptHandler` on the root; other root handlers
       are *detached*, never ``close()``d — they may belong to another tool
       (pytest's ``--log-file`` FileHandler would otherwise stay closed and
       empty for the whole session).
    2. The root level mirrors ``LOG_LEVEL`` numerically. The loguru sink is
       the real gate, but libraries guard expensive work with
       ``isEnabledFor(DEBUG)``: at ``NOTSET`` the openai client would
       ``model_dump`` the full prompt on every call only for loguru to drop
       it. Using the numeric value is also what makes a loguru-only level
       (``TRACE``, ``SUCCESS``) work — ``basicConfig(level="TRACE")`` raises.
    3. The loggers in :data:`_STDLIB_LOGGERS_TO_FLATTEN` are reset to
       propagate (uvicorn and Ray install their own handlers with
       ``propagate=False``).
    4. The chatty loggers in :data:`_STDLIB_LOGGERS_QUIETED` are pinned to
       WARNING, but only while still at ``NOTSET``, so an explicit level set
       by an operator is respected.
    """
    root = logging.getLogger()
    if not any(isinstance(h, InterceptHandler) for h in root.handlers):
        root.addHandler(InterceptHandler())
    for handler in list(root.handlers):
        if not isinstance(handler, InterceptHandler):
            root.removeHandler(handler)
    root.setLevel(_stdlib_level(level))
    for name in _STDLIB_LOGGERS_TO_FLATTEN:
        std_logger = logging.getLogger(name)
        std_logger.handlers = []
        std_logger.propagate = True
    for name in _STDLIB_LOGGERS_QUIETED:
        std_logger = logging.getLogger(name)
        if std_logger.level == logging.NOTSET:
            std_logger.setLevel(logging.WARNING)


def get_logger(config=None):
    config = config or load_config()

    logger.remove()

    if getattr(config.verbose, "format", "text") == "json":
        # Machine format for collectors: Docker / the kubelet capture stderr
        # and Alloy/Promtail parse each line (docs: loki_logs.md).
        logger.add(json_sink, level=config.verbose.level)
        intercept_stdlib_logging(config.verbose.level)
    else:
        # Pretty, colorized logs to the terminal (stderr): the level label is
        # colored by severity via loguru's <level> tag and the call site is
        # cyan. colorize=True forces ANSI on even when stderr isn't a TTY
        # (e.g. under ``docker compose up``).
        logger.add(sys.stderr, format=terminal_formatter, level=config.verbose.level, colorize=True)

    return logger
