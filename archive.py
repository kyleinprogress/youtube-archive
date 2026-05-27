from __future__ import annotations

import argparse
import contextlib
import logging
import pathlib
import re
import subprocess
import sys
import threading
import time
import tomllib
import traceback
from dataclasses import dataclass
from typing import Any, Iterator, NoReturn


CONFIG_PATH = pathlib.Path("config.toml")
DATA_DIR = pathlib.Path("data")
SLUG_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")

logging.addLevelName(logging.WARNING, "WARN")


@dataclass(frozen=True)
class RunContext:
    args: argparse.Namespace
    creators: list[dict[str, Any]]


class ConfigError(Exception):
    pass


class UTCFormatter(logging.Formatter):
    converter = time.gmtime


LOG_FORMATTER = UTCFormatter(
    fmt="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    raw_config = load_config()
    creators = validate_config(raw_config)
    selected_creators = select_creators(creators, args.creator)
    context = RunContext(args=args, creators=selected_creators)

    check_ffmpeg()

    for creator in context.creators:
        slug = creator["slug"]
        print(f"==> {slug}")
        with creator_scope(slug):
            setup_creator_environment(slug)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Archive configured YouTube creators with yt-dlp."
    )
    parser.add_argument(
        "--creator",
        metavar="slug",
        help="Run only the creator with this configured slug.",
    )
    parser.add_argument(
        "--manifests-only",
        action="store_true",
        help="Only refresh manifest data; media download behavior is added later.",
    )
    parser.add_argument(
        "--refresh-metadata",
        action="store_true",
        help="Refresh normalized metadata for already archived videos.",
    )
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help="Check for and download improved formats for archived videos.",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Audit archive consistency without changing media files.",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Repair audit findings when supported by later PRDs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan work without applying download or repair changes.",
    )
    return parser


def load_config() -> dict[str, Any]:
    try:
        with CONFIG_PATH.open("rb") as config_file:
            data = tomllib.load(config_file)
    except FileNotFoundError:
        print(
            f"error: ./config.toml not found (cwd: {pathlib.Path.cwd()})",
            file=sys.stderr,
        )
        raise SystemExit(2)
    except tomllib.TOMLDecodeError as exc:
        print(f"error: config.toml: {exc}", file=sys.stderr)
        raise SystemExit(2)
    return data


def validate_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        return _validate_config(config)
    except ConfigError as exc:
        print(f"error: config.toml: {exc}", file=sys.stderr)
        raise SystemExit(2)


def _validate_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    defaults = config.get("defaults")
    if not isinstance(defaults, dict):
        raise ConfigError("defaults: must be a table")

    default_settings = {
        "format": _require_non_empty_string(defaults, "format", "defaults.format"),
        "format_sort": _optional_string_list(
            defaults, "format_sort", "defaults.format_sort", default=[]
        ),
        "merge_output_format": _require_non_empty_string(
            defaults, "merge_output_format", "defaults.merge_output_format"
        ),
        "min_upload_age_hours": _optional_non_negative_int(
            defaults,
            "min_upload_age_hours",
            "defaults.min_upload_age_hours",
            default=48,
        ),
    }

    raw_creators = config.get("creator")
    if not isinstance(raw_creators, list) or not raw_creators:
        raise ConfigError("creator: at least one [[creator]] entry is required")

    resolved_creators: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()

    for index, creator in enumerate(raw_creators):
        path = f"creator[{index}]"
        if not isinstance(creator, dict):
            raise ConfigError(f"{path}: must be a table")

        slug = _require_non_empty_string(creator, "slug", f"{path}.slug")
        name = _require_non_empty_string(creator, "name", f"{path}.name")
        channel_url = _require_non_empty_string(
            creator, "channel_url", f"{path}.channel_url"
        )

        if not SLUG_PATTERN.fullmatch(slug):
            raise ConfigError(
                f'{path}.slug: must match {SLUG_PATTERN.pattern} (got "{slug}")'
            )
        if slug in seen_slugs:
            raise ConfigError(f'{path}.slug: duplicate slug "{slug}"')
        seen_slugs.add(slug)

        if not channel_url.startswith("https://www.youtube.com/"):
            raise ConfigError(
                f'{path}.channel_url: must start with "https://www.youtube.com/"'
            )

        settings = dict(default_settings)
        if "format" in creator:
            settings["format"] = _require_non_empty_string(
                creator, "format", f"{path}.format"
            )
        if "format_sort" in creator:
            settings["format_sort"] = _optional_string_list(
                creator, "format_sort", f"{path}.format_sort", default=[]
            )
        if "merge_output_format" in creator:
            settings["merge_output_format"] = _require_non_empty_string(
                creator, "merge_output_format", f"{path}.merge_output_format"
            )
        if "min_upload_age_hours" in creator:
            settings["min_upload_age_hours"] = _optional_non_negative_int(
                creator,
                "min_upload_age_hours",
                f"{path}.min_upload_age_hours",
                default=48,
            )

        resolved_creators.append(
            {
                "slug": slug,
                "name": name,
                "channel_url": channel_url,
                "format": settings["format"],
                "format_sort": settings["format_sort"],
                "merge_output_format": settings["merge_output_format"],
                "min_upload_age_hours": settings["min_upload_age_hours"],
                "exclude_playlists": _optional_string_list(
                    creator,
                    "exclude_playlists",
                    f"{path}.exclude_playlists",
                    default=[],
                ),
                "extra_playlists": _optional_string_list(
                    creator,
                    "extra_playlists",
                    f"{path}.extra_playlists",
                    default=[],
                ),
            }
        )

    return resolved_creators


def _require_non_empty_string(data: dict[str, Any], key: str, path: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{path}: must be a non-empty string")
    return value


def _optional_string_list(
    data: dict[str, Any], key: str, path: str, *, default: list[str]
) -> list[str]:
    if key not in data:
        return list(default)
    value = data[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{path}: must be a list of strings")
    return list(value)


def _optional_non_negative_int(
    data: dict[str, Any], key: str, path: str, *, default: int
) -> int:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ConfigError(f"{path}: must be a non-negative integer")
    return value


def select_creators(
    creators: list[dict[str, Any]], requested_slug: str | None
) -> list[dict[str, Any]]:
    if requested_slug is None:
        return creators

    for creator in creators:
        if creator["slug"] == requested_slug:
            return [creator]

    print(
        f'error: --creator: no creator with slug "{requested_slug}" found in config.toml',
        file=sys.stderr,
    )
    raise SystemExit(2)


def check_ffmpeg() -> None:
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        _ffmpeg_error()

    if result.returncode != 0:
        _ffmpeg_error()


def _ffmpeg_error() -> NoReturn:
    print(
        "error: ffmpeg not found or not working — install with: brew install ffmpeg",
        file=sys.stderr,
    )
    raise SystemExit(2)


def setup_creator_environment(slug: str) -> None:
    base = DATA_DIR / slug
    for directory in (
        base / "videos",
        base / "manifests" / "playlists",
        base / "manifests" / "channel",
        base / "logs",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    for log_name in ("download.log", "manifests.log", "errors.log"):
        (base / "logs" / log_name).touch(exist_ok=True)

    # Register handlers now so later PRDs can fetch loggers without path setup.
    get_creator_loggers(slug)


def get_creator_loggers(
    slug: str,
) -> tuple[logging.Logger, logging.Logger, logging.Logger]:
    return (
        _configure_creator_logger(slug, "download"),
        _configure_creator_logger(slug, "manifests"),
        _configure_creator_logger(slug, "errors"),
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


def run_subprocess_streaming(
    cmd: list[str],
    logger: logging.Logger,
    *,
    level: int = logging.INFO,
    stderr_level: int = logging.WARNING,
) -> int:
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    threads = []
    for stream, stream_level in (
        (process.stdout, level),
        (process.stderr, stderr_level),
    ):
        if stream is None:
            continue
        thread = threading.Thread(
            target=_pump_stream_to_logger,
            args=(stream, logger, stream_level),
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    return_code = process.wait()
    for thread in threads:
        thread.join()
    return return_code


def _pump_stream_to_logger(stream: Any, logger: logging.Logger, level: int) -> None:
    with stream:
        for line in stream:
            message = line.rstrip("\n")
            if message.strip():
                logger.log(level, message)


@contextlib.contextmanager
def creator_scope(slug: str) -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        _handle_creator_failure(slug, exc)


def _handle_creator_failure(slug: str, exc: Exception) -> None:
    try:
        _, _, errors_log = get_creator_loggers(slug)
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


if __name__ == "__main__":
    main()
