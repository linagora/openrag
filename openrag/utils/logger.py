from loguru import logger
import sys
import os
from config import load_config

config = load_config()


def get_logger():
    def formatter(record):
        level = record["level"].name
        mod = record["name"]
        func = record["function"]
        line = record["line"]
        message = record["message"]
        context = record["extra"]
        context_str = " | ".join(f"{k}={v}" for k, v in context.items())
        return (
            f"{level:<8} | {mod}:{func}:{line} - {message}"
            + (f" [{context_str}]" if context else "")
            + "\n"
        )

    logger.remove()

    # Pretty logs to stdout (terminal)
    logger.add(
        sys.stderr,
        format=formatter,
        level=config.verbose.level,  # INFO or DEBUG
    )

    # JSON logs to file for later use (e.g. Grafana ingestion)
    log_dir = config.paths.log_dir if hasattr(config.paths, "log_dir") else "logs"
    os.makedirs(log_dir, exist_ok=True)

    logger.add(
        f"{log_dir}/app.json",
        serialize=True,
        level=config.verbose.level,
        rotation="10 MB",
        retention="10 days",
        enqueue=True,
    )

    return logger
