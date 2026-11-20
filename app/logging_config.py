from __future__ import annotations

import logging
import os
import sys
from typing import TextIO

LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def configured_log_level() -> int:
    value = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    return LOG_LEVELS.get(value, logging.INFO)


def configured_log_level_name() -> str:
    value = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    return value if value in LOG_LEVELS else "INFO"


def configure_logging(*, stream: TextIO | None = None) -> int:
    raw_value = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    level = LOG_LEVELS.get(raw_value, logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=stream or sys.stdout,
    )
    root = logging.getLogger()
    root.setLevel(level)

    for name in ("ytmusicapi", "yt_dlp"):
        component_logger = logging.getLogger(name)
        component_logger.setLevel(level)
        component_logger.propagate = True

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    if raw_value not in LOG_LEVELS:
        logging.getLogger("musicasyt.logging").warning(
            "Unknown LOG_LEVEL=%r; using INFO. Valid values: %s",
            raw_value,
            ", ".join(LOG_LEVELS),
        )
    else:
        reported = getattr(root, "_musicasyt_reported_log_level", None)
        if reported != raw_value:
            logging.getLogger("musicasyt.logging").info(
                "Logging initialized: LOG_LEVEL=%s",
                raw_value,
            )
            root._musicasyt_reported_log_level = raw_value

    return level
