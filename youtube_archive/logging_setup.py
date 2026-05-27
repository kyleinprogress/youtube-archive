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
    dry_run: bool = False,
) -> tuple[logging.Logger, logging.Logger, logging.Logger, logging.Logger, logging.Logger]:
    return (
        _configure_creator_logger(slug, "download", dry_run=dry_run),
        _configure_creator_logger(slug, "manifests", dry_run=dry_run),
        _configure_creator_logger(slug, "errors", dry_run=dry_run),
        _configure_creator_logger(slug, "refresh", dry_run=dry_run),
        _configure_creator_logger(slug, "upgrade", dry_run=dry_run),
    )


def _configure_creator_logger(
    slug: str,
    kind: str,
    *,
    dry_run: bool = False,
) -> logging.Logger:
    logger_name = f"archive.{slug}.{kind}"
    if dry_run:
        logger_name = f"{logger_name}.dry_run"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if dry_run:
        if not any(isinstance(handler, logging.NullHandler) for handler in logger.handlers):
            logger.addHandler(logging.NullHandler())
        return logger

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
