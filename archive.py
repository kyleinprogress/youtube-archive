from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import logging
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import tomllib
import traceback
import urllib.parse
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


class ChannelLevelFailure(Exception):
    def __init__(self, phase: str, message: str, stderr: str = "") -> None:
        super().__init__(message)
        self.phase = phase
        self.stderr = stderr


class PlaylistFetchFailure(Exception):
    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


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
        print(f"==> {slug}", flush=True)
        with creator_scope(slug):
            setup_creator_environment(slug)
            candidate_set = run_pass_one(creator)
            if not context.args.manifests_only:
                run_pass_two(candidate_set)


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


def run_pass_one(creator: dict[str, Any]) -> dict[str, Any]:
    # Return a well-formed candidate set even when unexpected Pass 1 code fails.
    try:
        return _run_pass_one(creator)
    except Exception as exc:
        slug = creator["slug"]
        _, manifests_log, errors_log = get_creator_loggers(slug)
        failure = ChannelLevelFailure(
            "unexpected",
            str(exc),
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )
        log_channel_level_failure(slug, failure, manifests_log, errors_log)
        return {
            "slug": slug,
            "canonical_url": "",
            "candidate_video_ids": [],
            "channel_level_failed": True,
            "playlist_membership": {},
        }


def _run_pass_one(creator: dict[str, Any]) -> dict[str, Any]:
    slug = creator["slug"]
    _, manifests_log, errors_log = get_creator_loggers(slug)
    manifests_log.info("Pass 1 starting")

    canonical_url = ""
    candidate_video_ids: list[str] = []
    seen_video_ids: set[str] = set()
    playlist_membership: dict[str, list[dict[str, Any]]] = {}
    channel_level_failed = False
    utc_date = utc_date_string()

    try:
        resolution_payload = resolve_canonical_channel(creator["channel_url"])
        canonical_url = f"https://www.youtube.com/channel/{resolution_payload['channel_id']}"
        manifests_log.info("canonical URL resolved: %s", canonical_url)
        write_creator_json(
            slug,
            creator["channel_url"],
            canonical_url,
            resolution_payload,
        )
    except ChannelLevelFailure as exc:
        log_channel_level_failure(slug, exc, manifests_log, errors_log)
        manifests_log.info("Pass 1 complete: 0 candidate video IDs")
        return {
            "slug": slug,
            "canonical_url": canonical_url,
            "candidate_video_ids": candidate_video_ids,
            "channel_level_failed": True,
            "playlist_membership": playlist_membership,
        }

    discovered_playlist_ids: list[str] = []
    playlists_discovery_failed = False
    try:
        discovered_playlist_ids = fetch_playlists_index(slug, canonical_url, utc_date)
        manifests_log.info("playlists discovered: %s", len(discovered_playlist_ids))
    except ChannelLevelFailure as exc:
        channel_level_failed = True
        playlists_discovery_failed = True
        log_channel_level_failure(slug, exc, manifests_log, errors_log)

    final_playlist_ids = compute_final_playlist_set(
        creator,
        discovered_playlist_ids,
        playlists_discovery_failed,
        manifests_log,
    )

    for playlist_id in final_playlist_ids:
        try:
            playlist_video_ids = fetch_playlist_manifest(
                slug,
                playlist_id,
                utc_date,
                playlist_membership,
            )
        except PlaylistFetchFailure as exc:
            one_line_error = str(exc).replace("\n", " ")
            manifests_log.warning(
                "playlist fetch failed: %s — %s",
                playlist_id,
                one_line_error,
            )
            errors_log.error(
                "playlist %s: %s",
                playlist_id,
                log_safe_detail(exc.stderr or str(exc)),
            )
            continue

        for video_id in playlist_video_ids:
            append_candidate_video_id(video_id, candidate_video_ids, seen_video_ids)
        manifests_log.info(
            "playlist fetched: %s (%s videos)",
            playlist_id,
            len(playlist_video_ids),
        )

    try:
        upload_video_ids = fetch_uploads_manifest(slug, canonical_url, utc_date)
        for video_id in upload_video_ids:
            append_candidate_video_id(video_id, candidate_video_ids, seen_video_ids)
            playlist_membership.setdefault(video_id, [])
        manifests_log.info("uploads manifest fetched: %s videos", len(upload_video_ids))
    except ChannelLevelFailure as exc:
        channel_level_failed = True
        log_channel_level_failure(slug, exc, manifests_log, errors_log)

    manifests_log.info(
        "Pass 1 complete: %s candidate video IDs",
        len(candidate_video_ids),
    )
    return {
        "slug": slug,
        "canonical_url": canonical_url,
        "candidate_video_ids": candidate_video_ids,
        "channel_level_failed": channel_level_failed,
        "playlist_membership": playlist_membership,
    }


def run_pass_two(candidate_set: dict[str, Any]) -> None:
    return None


def resolve_canonical_channel(config_channel_url: str) -> dict[str, Any]:
    result = run_yt_dlp_capture(
        [
            "yt-dlp",
            "--skip-download",
            "--no-warnings",
            "--playlist-items",
            "0",
            "--dump-single-json",
            config_channel_url,
        ]
    )
    if result.returncode != 0:
        raise ChannelLevelFailure(
            "canonical-resolution",
            "yt-dlp exited non-zero",
            result.stderr_text,
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ChannelLevelFailure(
            "canonical-resolution",
            f"invalid JSON: {exc}",
            result.stderr_text,
        ) from exc

    channel_id = payload.get("channel_id")
    if not isinstance(channel_id, str) or not channel_id:
        raise ChannelLevelFailure(
            "canonical-resolution",
            "missing channel_id",
            result.stderr_text,
        )
    for key in ("channel", "uploader_id"):
        value = payload.get(key)
        if not isinstance(value, str):
            raise ChannelLevelFailure(
                "canonical-resolution",
                f"missing {key}",
                result.stderr_text,
            )
    description = payload.get("description")
    if description is None:
        payload["description"] = ""
    elif not isinstance(description, str):
        raise ChannelLevelFailure(
            "canonical-resolution",
            "description: not a string",
            result.stderr_text,
        )

    return payload


def write_creator_json(
    slug: str,
    config_channel_url: str,
    canonical_url: str,
    resolution_payload: dict[str, Any],
) -> None:
    _, manifests_log, _ = get_creator_loggers(slug)
    creator_json_path = DATA_DIR / slug / "creator.json"
    previous_handle = read_existing_creator_handle(creator_json_path)

    handle = required_payload_string(resolution_payload, "uploader_id")
    snapshot = {
        "config_slug": slug,
        "config_channel_url": config_channel_url,
        "canonical_url": canonical_url,
        "channel_id": required_payload_string(resolution_payload, "channel_id"),
        "handle": handle,
        "title": required_payload_string(resolution_payload, "channel"),
        "description": required_payload_string(resolution_payload, "description"),
        "subscriber_count": optional_payload_int(
            resolution_payload,
            "channel_follower_count",
        ),
        "avatar_url": extract_avatar_url(resolution_payload.get("thumbnails")),
        "banner_url": extract_banner_url(resolution_payload.get("thumbnails")),
        "snapshot_taken_at": utc_timestamp(),
    }

    if previous_handle is not None and previous_handle != handle:
        manifests_log.info(
            "handle renamed: %s -> %s (canonical URL unchanged)",
            previous_handle,
            handle,
        )

    atomic_write_json(creator_json_path, snapshot)


def read_existing_creator_handle(path: pathlib.Path) -> str | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    handle = payload.get("handle")
    return handle if isinstance(handle, str) else None


def required_payload_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ChannelLevelFailure(
            "creator-json-write",
            f"missing {key}",
        )
    return value


def optional_payload_int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def extract_avatar_url(thumbnails: Any) -> str | None:
    if not isinstance(thumbnails, list) or not thumbnails:
        return None
    first_thumbnail = thumbnails[0]
    if not isinstance(first_thumbnail, dict):
        return None
    url = first_thumbnail.get("url")
    return url if isinstance(url, str) else None


def extract_banner_url(thumbnails: Any) -> str | None:
    if not isinstance(thumbnails, list):
        return None
    for thumbnail in thumbnails:
        if not isinstance(thumbnail, dict):
            continue
        thumbnail_id = thumbnail.get("id")
        url = thumbnail.get("url")
        if isinstance(thumbnail_id, str) and "banner" in thumbnail_id.lower():
            return url if isinstance(url, str) else None
    return None


def fetch_playlists_index(slug: str, canonical_url: str, utc_date: str) -> list[str]:
    result = run_yt_dlp_capture(
        [
            "yt-dlp",
            "--flat-playlist",
            "--dump-single-json",
            f"{canonical_url}/playlists",
        ]
    )
    if result.returncode != 0:
        raise ChannelLevelFailure(
            "playlists-discovery",
            "yt-dlp exited non-zero",
            result.stderr_text,
        )

    payload = parse_json_bytes_or_channel_failure(
        result.stdout,
        "playlists-discovery",
        result.stderr_text,
    )
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ChannelLevelFailure(
            "playlists-discovery",
            "missing entries array",
            result.stderr_text,
        )

    channel_dir = DATA_DIR / slug / "manifests" / "channel"
    atomic_write_bytes(channel_dir / "playlists_index_latest.json", result.stdout)
    atomic_write_bytes(
        channel_dir / f"playlists_index_{utc_date}.json",
        result.stdout,
    )

    playlist_ids: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        playlist_id = entry.get("id")
        if isinstance(playlist_id, str) and playlist_id:
            playlist_ids.append(playlist_id)
    return playlist_ids


def compute_final_playlist_set(
    creator: dict[str, Any],
    discovered_playlist_ids: list[str],
    playlists_discovery_failed: bool,
    manifests_log: logging.Logger,
) -> list[str]:
    final_ids: list[str] = []
    final_set: set[str] = set()
    discovered_set = set(discovered_playlist_ids)

    for playlist_id in discovered_playlist_ids:
        if playlist_id not in final_set:
            final_ids.append(playlist_id)
            final_set.add(playlist_id)

    for url in creator["extra_playlists"]:
        playlist_id = extract_playlist_id_from_url(url)
        if playlist_id is None:
            manifests_log.warning(
                "extra_playlists entry has no list= param, skipping: %s",
                url,
            )
            continue
        if playlist_id in discovered_set:
            manifests_log.info(
                "extra_playlists entry already discovered: %s",
                playlist_id,
            )
        if playlist_id not in final_set:
            final_ids.append(playlist_id)
            final_set.add(playlist_id)

    exclude_set = set(creator["exclude_playlists"])
    final_ids = [playlist_id for playlist_id in final_ids if playlist_id not in exclude_set]
    if playlists_discovery_failed:
        manifests_log.info(
            "final playlist set: %s (discovery failed, extra: %s, excluded: %s)",
            len(final_ids),
            len(creator["extra_playlists"]),
            len(creator["exclude_playlists"]),
        )
    else:
        manifests_log.info(
            "final playlist set: %s (discovered: %s, extra: %s, excluded: %s)",
            len(final_ids),
            len(discovered_playlist_ids),
            len(creator["extra_playlists"]),
            len(creator["exclude_playlists"]),
        )
    return final_ids


def extract_playlist_id_from_url(url: str) -> str | None:
    parsed_url = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed_url.query)
    values = query.get("list")
    if not values:
        return None
    playlist_id = values[0]
    return playlist_id or None


def fetch_playlist_manifest(
    slug: str,
    playlist_id: str,
    utc_date: str,
    playlist_membership: dict[str, list[dict[str, Any]]],
) -> list[str]:
    result = run_yt_dlp_capture(
        [
            "yt-dlp",
            "--flat-playlist",
            "--dump-single-json",
            f"https://www.youtube.com/playlist?list={playlist_id}",
        ]
    )
    if result.returncode != 0:
        raise PlaylistFetchFailure("yt-dlp exited non-zero", result.stderr_text)

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PlaylistFetchFailure(f"invalid JSON: {exc}", result.stderr_text) from exc

    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise PlaylistFetchFailure("missing entries array", result.stderr_text)

    playlists_dir = DATA_DIR / slug / "manifests" / "playlists"
    atomic_write_bytes(playlists_dir / f"{playlist_id}_latest.json", result.stdout)
    atomic_write_bytes(playlists_dir / f"{playlist_id}_{utc_date}.json", result.stdout)

    playlist_title = payload.get("title")
    if not isinstance(playlist_title, str):
        playlist_title = ""

    video_ids: list[str] = []
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            continue
        video_id = entry.get("id")
        if not isinstance(video_id, str) or not video_id:
            continue
        index = position
        entry_index = entry.get("index")
        if isinstance(entry_index, int) and not isinstance(entry_index, bool):
            index = entry_index
        video_ids.append(video_id)
        playlist_membership.setdefault(video_id, []).append(
            {"id": playlist_id, "title": playlist_title, "index": index}
        )

    return video_ids


def fetch_uploads_manifest(slug: str, canonical_url: str, utc_date: str) -> list[str]:
    result = run_yt_dlp_capture(
        [
            "yt-dlp",
            "--flat-playlist",
            "--dump-single-json",
            f"{canonical_url}/videos",
        ]
    )
    if result.returncode != 0:
        raise ChannelLevelFailure("uploads", "yt-dlp exited non-zero", result.stderr_text)

    payload = parse_json_bytes_or_channel_failure(
        result.stdout,
        "uploads",
        result.stderr_text,
    )
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ChannelLevelFailure("uploads", "missing entries array", result.stderr_text)

    channel_dir = DATA_DIR / slug / "manifests" / "channel"
    atomic_write_bytes(channel_dir / "uploads_latest.json", result.stdout)
    atomic_write_bytes(channel_dir / f"uploads_{utc_date}.json", result.stdout)

    video_ids: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        video_id = entry.get("id")
        if isinstance(video_id, str) and video_id:
            video_ids.append(video_id)
    return video_ids


def parse_json_bytes_or_channel_failure(
    data: bytes,
    phase: str,
    stderr: str,
) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ChannelLevelFailure(phase, f"invalid JSON: {exc}", stderr) from exc
    if not isinstance(payload, dict):
        raise ChannelLevelFailure(phase, "JSON root must be an object", stderr)
    return payload


@dataclass(frozen=True)
class YtDlpResult:
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace").strip()


def run_yt_dlp_capture(cmd: list[str]) -> YtDlpResult:
    process = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return YtDlpResult(
        returncode=process.returncode,
        stdout=process.stdout,
        stderr=process.stderr,
    )


def log_channel_level_failure(
    slug: str,
    exc: ChannelLevelFailure,
    manifests_log: logging.Logger,
    errors_log: logging.Logger,
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
    print(
        f"error: {slug}: channel-level failure during {exc.phase} — see "
        f"data/{slug}/logs/errors.log",
        file=sys.stderr,
    )


def log_safe_detail(detail: str) -> str:
    return detail.replace("\r", "\\r").replace("\n", "\\n")


def append_candidate_video_id(
    video_id: str,
    candidate_video_ids: list[str],
    seen_video_ids: set[str],
) -> None:
    if video_id in seen_video_ids:
        return
    candidate_video_ids.append(video_id)
    seen_video_ids.add(video_id)


def utc_timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_date_string() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def atomic_write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    atomic_write_bytes(path, data + b"\n")


def atomic_write_bytes(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with tmp_path.open("wb") as tmp_file:
        tmp_file.write(data)
    os.replace(tmp_path, path)


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
