import sys

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


def get_logger(config=None):
    config = config or load_config()

    logger.remove()

    # Pretty, colorized logs to the terminal (stderr): the level label is
    # colored by severity via loguru's <level> tag and the call site is cyan.
    # colorize=True forces ANSI on even when stderr isn't a TTY (e.g. under
    # ``docker compose up``). stderr is the only sink: Docker / the kubelet
    # capture it and a collector ships it to Loki (docs: loki_logs.md).
    logger.add(sys.stderr, format=terminal_formatter, level=config.verbose.level, colorize=True)

    return logger
