from __future__ import annotations

import logging
import pathlib
import time

from youtube_archive.utils import DATA_DIR


logging.addLevelName(logging.WARNING, "WARN")


class UTCFormatter(logging.Formatter):
    converter = time.gmtime


LOG_FORMATTER = UTCFormatter(
    fmt="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)


def get_creator_loggers(
    slug: str,
) -> tuple[logging.Logger, logging.Logger, logging.Logger, logging.Logger, logging.Logger]:
    return (
        _configure_creator_logger(slug, "download"),
        _configure_creator_logger(slug, "manifests"),
        _configure_creator_logger(slug, "errors"),
        _configure_creator_logger(slug, "refresh"),
        _configure_creator_logger(slug, "upgrade"),
    )


def _configure_creator_logger(slug: str, kind: str) -> logging.Logger:
    logger = logging.getLogger(f"archive.{slug}.{kind}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_path = DATA_DIR / slug / "logs" / f"{kind}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_log_path = log_path.resolve()

    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            if pathlib.Path(handler.baseFilename) == resolved_log_path:
                return logger

    handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    handler.setFormatter(LOG_FORMATTER)
    logger.addHandler(handler)
    return logger
