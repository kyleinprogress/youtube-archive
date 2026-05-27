from __future__ import annotations

import contextlib
import logging
import sys
import traceback
from typing import Iterator

from youtube_archive.logging_setup import get_creator_loggers


class ChannelLevelFailure(Exception):
    def __init__(self, phase: str, message: str, stderr: str = "") -> None:
        super().__init__(message)
        self.phase = phase
        self.stderr = stderr


class PlaylistFetchFailure(Exception):
    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


def log_channel_level_failure(
    slug: str,
    exc: ChannelLevelFailure,
    manifests_log: logging.Logger,
    errors_log: logging.Logger,
    *,
    dry_run: bool = False,
) -> None:
    detail = log_safe_detail(exc.stderr or str(exc))
    manifests_log.error(
        "channel-level failure during %s: %s",
        exc.phase,
        detail,
    )
    errors_log.error(
        "channel-level failure during %s: %s",
        exc.phase,
        detail,
    )
    if not dry_run:
        print(
            f"error: {slug}: channel-level failure during {exc.phase} — see "
            f"data/{slug}/logs/errors.log",
            file=sys.stderr,
        )


def log_safe_detail(detail: str) -> str:
    return detail.replace("\r", "\\r").replace("\n", "\\n")


def last_five_lines(text: str) -> list[str]:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-5:]


@contextlib.contextmanager
def creator_scope(slug: str, *, dry_run: bool = False) -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        _handle_creator_failure(slug, exc, dry_run=dry_run)


def _handle_creator_failure(slug: str, exc: Exception, *, dry_run: bool = False) -> None:
    if dry_run:
        print(f"[DRY RUN] error: {slug}: {exc}", file=sys.stderr)
        return

    try:
        _, _, errors_log, _, _ = get_creator_loggers(slug)
    except Exception as logger_exc:
        print(
            f"error: {slug}: {exc} (additionally, errors.log could not be written: {logger_exc})",
            file=sys.stderr,
        )
        return

    errors_log.error("%s failed: %s", slug, exc)
    for line in traceback.format_exception(exc):
        for message in line.rstrip().splitlines():
            if message:
                errors_log.error(message)

    print(f"error: {slug}: {exc}", file=sys.stderr)
