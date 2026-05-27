from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import tomllib
from typing import Any, NoReturn

from youtube_archive.logging_setup import get_creator_loggers
from youtube_archive.utils import DATA_DIR


CONFIG_PATH = pathlib.Path("config.toml")
SLUG_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


class ConfigError(Exception):
    pass


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
        "subtitle_preferences": _optional_string_list(
            defaults,
            "subtitle_preferences",
            "defaults.subtitle_preferences",
            default=["en"],
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
        if "subtitle_preferences" in creator:
            settings["subtitle_preferences"] = _optional_string_list(
                creator,
                "subtitle_preferences",
                f"{path}.subtitle_preferences",
                default=["en"],
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
                "subtitle_preferences": settings["subtitle_preferences"],
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


def setup_creator_environment(slug: str, *, dry_run: bool = False) -> None:
    base = DATA_DIR / slug
    for directory in (
        base / "videos",
        base / "manifests" / "playlists",
        base / "manifests" / "channel",
        base / "logs",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    if not dry_run:
        for log_name in (
            "download.log",
            "manifests.log",
            "errors.log",
            "refresh.log",
            "upgrade.log",
            "audit.log",
        ):
            log_path = base / "logs" / log_name
            if not log_path.exists():
                log_path.touch()

    # Register handlers now so later PRDs can fetch loggers without path setup.
    get_creator_loggers(slug, dry_run=dry_run)
