import os
import sys

from config import load_config
from loguru import logger


config = load_config()

def escape_markup(s: str) -> str:
    return s.replace("\\", "\\\\").replace("<", "\\<").replace(">", "\\>")


def get_logger():
    def formatter(record):
        level = record["level"].name
        mod = record["name"]
        func = record["function"]
        line = record["line"]

        msg = escape_markup(record["message"])
        extra = " | ".join(
            f"{k}={escape_markup(str(v))}"
            for k, v in record["extra"].items()
        )
        return (
            f"{level:<8} | {mod}:{func}:{line} - {msg}"
            + (f" [{extra}]" if extra else "")
            + "\n"
        )

    logger.remove()

    # Pretty logs to stdout (terminal)
    logger.add(
        sys.stderr,
        format=formatter,
        level=config.verbose.level,
        colorize=False
    )

    # JSON logs to file for later use (e.g. Grafana ingestion)
    log_dir = config.paths.log_dir if hasattr(config.paths, "log_dir") else "logs"
    try:
        os.makedirs(log_dir, exist_ok=True)
        logger.add(
            f"{log_dir}/app.json",
            serialize=True,
            level=config.verbose.level,
            rotation="10 MB",
            retention="10 days",
            enqueue=True,
        )
    except PermissionError:
        # Skip file logging if we don't have permission (e.g., during tests)
        pass

    return logger
