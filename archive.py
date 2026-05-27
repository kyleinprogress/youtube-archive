from __future__ import annotations

import argparse
import collections
import contextlib
import datetime
import hashlib
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


@dataclass(frozen=True)
class EligibleVideo:
    video_id: str
    timestamp: int | None


@dataclass(frozen=True)
class GatedVideo:
    video_id: str
    reason: str
    value: str | int | None = None


@dataclass(frozen=True)
class WorkBuckets:
    already_archived: list[str]
    eligible_sorted: list[EligibleVideo]
    gated_live_or_premiere: list[GatedVideo]
    gated_too_recent: list[GatedVideo]


@dataclass(frozen=True)
class DownloadResult:
    returncode: int
    tail_lines: list[str]


@dataclass(frozen=True)
class RefreshInvocationResult:
    state: str
    payload: dict[str, Any] | None
    stderr_tail: list[str]


@dataclass(frozen=True)
class RefreshVideoResult:
    state: str
    upgrade_delta: int = 0


@dataclass
class RefreshCounts:
    checked: int = 0
    metadata_changes: int = 0
    availability_changes: int = 0
    upgrades_available: int = 0
    transient_errors: int = 0
    skipped: int = 0


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

    if context.args.refresh_metadata:
        run_refresh_mode(context.creators)
        return

    for creator in context.creators:
        slug = creator["slug"]
        print(f"==> {slug}", flush=True)
        with creator_scope(slug):
            setup_creator_environment(slug)
            candidate_set = run_pass_one(creator)
            if not context.args.manifests_only:
                run_pass_two(creator, candidate_set)
            if should_run_metadata_pass(context.args, candidate_set):
                run_pass_four(creator, candidate_set)


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

    for log_name in ("download.log", "manifests.log", "errors.log", "refresh.log"):
        log_path = base / "logs" / log_name
        if not log_path.exists():
            log_path.touch()

    # Register handlers now so later PRDs can fetch loggers without path setup.
    get_creator_loggers(slug)


def run_pass_one(creator: dict[str, Any]) -> dict[str, Any]:
    # Return a well-formed candidate set even when unexpected Pass 1 code fails.
    try:
        return _run_pass_one(creator)
    except Exception as exc:
        slug = creator["slug"]
        _, manifests_log, errors_log, _ = get_creator_loggers(slug)
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
            "video_manifest_entries": {},
        }


def _run_pass_one(creator: dict[str, Any]) -> dict[str, Any]:
    slug = creator["slug"]
    _, manifests_log, errors_log, _ = get_creator_loggers(slug)
    manifests_log.info("Pass 1 starting")

    canonical_url = ""
    candidate_video_ids: list[str] = []
    seen_video_ids: set[str] = set()
    playlist_membership: dict[str, list[dict[str, Any]]] = {}
    video_manifest_entries: dict[str, dict[str, Any]] = {}
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
            "video_manifest_entries": video_manifest_entries,
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
                video_manifest_entries,
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
        for video_id, entry in upload_video_ids:
            append_candidate_video_id(video_id, candidate_video_ids, seen_video_ids)
            playlist_membership.setdefault(video_id, [])
            merge_manifest_entry(video_id, entry, video_manifest_entries)
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
        "video_manifest_entries": video_manifest_entries,
    }


def run_pass_two(creator: dict[str, Any], candidate_set: dict[str, Any]) -> None:
    slug = creator["slug"]
    download_log, _, errors_log, _ = get_creator_loggers(slug)
    candidate_set["downloaded_this_run"] = []

    if candidate_set["channel_level_failed"]:
        download_log.info("Pass 2 skipped: channel-level failure in Pass 1")
        print(f"{slug}: skipping Pass 2 due to Pass 1 channel-level failure", file=sys.stderr)
        return

    total_candidates = len(candidate_set["candidate_video_ids"])
    download_log.info("Pass 2 starting: %s", total_candidates)
    buckets = build_download_work_list(creator, candidate_set, download_log)
    gated_count = len(buckets.gated_live_or_premiere) + len(buckets.gated_too_recent)
    download_log.info(
        "Pass 2 filtering: %s already in archive.txt, %s eligible, %s gated (live/premiere), %s gated (too recent)",
        len(buckets.already_archived),
        len(buckets.eligible_sorted),
        len(buckets.gated_live_or_premiere),
        len(buckets.gated_too_recent),
    )

    for gated_video in buckets.gated_live_or_premiere + buckets.gated_too_recent:
        log_gated_video(gated_video, creator["min_upload_age_hours"], download_log)

    downloaded = 0
    failed = 0
    for eligible_video in buckets.eligible_sorted:
        video_id = eligible_video.video_id
        download_log.info("==> downloading %s", video_id)
        command = build_download_command(creator, video_id)
        result = run_download_subprocess(command, download_log)
        if result.returncode == 0:
            downloaded += 1
            candidate_set["downloaded_this_run"].append(video_id)
            download_log.info("<-- done: %s", video_id)
        else:
            failed += 1
            download_log.info("<-- FAILED: %s", video_id)
            errors_log.error(
                "download failed: %s — %s",
                video_id,
                log_safe_detail("\n".join(result.tail_lines)),
            )

    download_log.info(
        "Pass 2 complete: %s downloaded, %s gated, %s failed",
        downloaded,
        gated_count,
        failed,
    )
    print(
        f"{slug}: Pass 2 — {downloaded} downloaded, {gated_count} gated, {failed} failed",
        flush=True,
    )


def build_download_work_list(
    creator: dict[str, Any],
    candidate_set: dict[str, Any],
    download_log: logging.Logger,
) -> WorkBuckets:
    slug = creator["slug"]
    archive_ids = read_download_archive(slug, download_log)
    already_archived: list[str] = []
    eligible: list[EligibleVideo] = []
    gated_live_or_premiere: list[GatedVideo] = []
    gated_too_recent: list[GatedVideo] = []
    entries = candidate_set.get("video_manifest_entries", {})
    now = datetime.datetime.now(datetime.timezone.utc)

    for video_id in candidate_set["candidate_video_ids"]:
        if video_id in archive_ids:
            already_archived.append(video_id)
            continue

        entry = entries.get(video_id, {})
        if not isinstance(entry, dict):
            entry = {}
        metadata = extract_eligibility_metadata(entry)
        if metadata["timestamp"] is None and metadata["live_status"] is None:
            fallback = fetch_eligibility_metadata(video_id)
            if fallback is None:
                download_log.warning(
                    "could not determine eligibility for %s, treating as eligible",
                    video_id,
                )
            else:
                metadata = fallback

        gated = classify_video(video_id, metadata, creator["min_upload_age_hours"], now)
        if gated is None:
            eligible.append(EligibleVideo(video_id, metadata["timestamp"]))
        elif gated.reason == "too_recent":
            gated_too_recent.append(gated)
        else:
            gated_live_or_premiere.append(gated)

    eligible.sort(
        key=lambda item: (
            item.timestamp is None,
            item.timestamp if item.timestamp is not None else 0,
        )
    )
    return WorkBuckets(
        already_archived=already_archived,
        eligible_sorted=eligible,
        gated_live_or_premiere=gated_live_or_premiere,
        gated_too_recent=gated_too_recent,
    )


def read_download_archive(slug: str, download_log: logging.Logger) -> set[str]:
    archive_path = DATA_DIR / slug / "archive.txt"
    if not archive_path.exists():
        return set()

    try:
        archive_text = archive_path.read_text(encoding="utf-8")
    except OSError as exc:
        download_log.warning("archive.txt exists but is unreadable: %s", exc)
        raise

    return set(parse_archive_video_ids(archive_text))


def extract_eligibility_metadata(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": optional_int_value(entry.get("timestamp")),
        "live_status": optional_string_value(entry.get("live_status")),
        "release_timestamp": optional_int_value(entry.get("release_timestamp")),
    }


def fetch_eligibility_metadata(video_id: str) -> dict[str, Any] | None:
    result = run_yt_dlp_capture(
        [
            "yt-dlp",
            "--skip-download",
            "--no-warnings",
            "--print",
            "%(timestamp)s|%(live_status)s|%(release_timestamp)s",
            f"https://www.youtube.com/watch?v={video_id}",
        ]
    )
    if result.returncode != 0:
        return None

    line = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
    if not line:
        return None
    parts = line[-1].split("|")
    if len(parts) != 3:
        return None
    timestamp, live_status, release_timestamp = parts
    return {
        "timestamp": parse_printed_int(timestamp),
        "live_status": optional_string_value(live_status),
        "release_timestamp": parse_printed_int(release_timestamp),
    }


def classify_video(
    video_id: str,
    metadata: dict[str, Any],
    min_upload_age_hours: int,
    now: datetime.datetime,
) -> GatedVideo | None:
    release_timestamp = metadata["release_timestamp"]
    if release_timestamp is not None:
        release_time = datetime.datetime.fromtimestamp(
            release_timestamp,
            datetime.timezone.utc,
        )
        if release_time > now:
            return GatedVideo(video_id, "future_release", iso_timestamp(release_time))

    live_status = metadata["live_status"]
    if live_status == "is_live":
        return GatedVideo(video_id, "is_live")
    if live_status == "is_upcoming":
        return GatedVideo(video_id, "is_upcoming")

    upload_timestamp = metadata["timestamp"]
    if upload_timestamp is None:
        upload_timestamp = release_timestamp
    if min_upload_age_hours > 0 and upload_timestamp is not None:
        upload_time = datetime.datetime.fromtimestamp(
            upload_timestamp,
            datetime.timezone.utc,
        )
        age_hours = (now - upload_time).total_seconds() / 3600
        if age_hours < min_upload_age_hours:
            return GatedVideo(video_id, "too_recent", int(age_hours))

    return None


def log_gated_video(
    gated_video: GatedVideo,
    min_upload_age_hours: int,
    download_log: logging.Logger,
) -> None:
    if gated_video.reason == "is_live":
        download_log.warning("gated (live stream in progress): %s", gated_video.video_id)
    elif gated_video.reason == "is_upcoming":
        download_log.warning("gated (upcoming premiere): %s", gated_video.video_id)
    elif gated_video.reason == "future_release":
        download_log.warning(
            "gated (future release: %s): %s",
            gated_video.value,
            gated_video.video_id,
        )
    elif gated_video.reason == "too_recent":
        download_log.warning(
            "gated (upload age %sh < min_upload_age_hours=%s): %s",
            gated_video.value,
            min_upload_age_hours,
            gated_video.video_id,
        )


def build_download_command(creator: dict[str, Any], video_id: str) -> list[str]:
    command = [
        "yt-dlp",
        "-f",
        creator["format"],
    ]
    if creator["format_sort"]:
        command.extend(["-S", ",".join(creator["format_sort"])])
    command.extend(
        [
            "--merge-output-format",
            creator["merge_output_format"],
            "--download-archive",
            str(DATA_DIR / creator["slug"] / "archive.txt"),
            "--write-info-json",
            "--write-thumbnail",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            "en.*,en",
            "--convert-thumbnails",
            "webp",
            "--embed-metadata",
            "--embed-thumbnail",
            "--no-progress",
            "-o",
            str(DATA_DIR / creator["slug"] / "videos" / "%(id)s" / "%(id)s.%(ext)s"),
            f"https://www.youtube.com/watch?v={video_id}",
        ]
    )
    return command


def run_download_subprocess(cmd: list[str], logger: logging.Logger) -> DownloadResult:
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    tail_lines: collections.deque[str] = collections.deque(maxlen=5)
    if process.stdout is not None:
        with process.stdout:
            for line in process.stdout:
                message = line.rstrip("\n")
                if message.strip():
                    if message.startswith(("WARNING:", "ERROR:")):
                        logger.warning(message)
                    else:
                        logger.info(message)
                    tail_lines.append(message)

    return DownloadResult(process.wait(), list(tail_lines))


def should_run_metadata_pass(args: argparse.Namespace, candidate_set: dict[str, Any]) -> bool:
    if candidate_set["channel_level_failed"]:
        return False
    return not (
        args.manifests_only
        or args.refresh_metadata
        or args.upgrade
        or args.audit
    )


def run_pass_four(creator: dict[str, Any], candidate_set: dict[str, Any]) -> None:
    slug = creator["slug"]
    download_log, _, _, _ = get_creator_loggers(slug)
    download_log.info("PRD 4 metadata pass starting")

    created = 0
    rebuilt = 0
    playlists_updated = 0
    skipped = 0
    playlist_membership = candidate_set["playlist_membership"]
    downloaded_this_run = set(candidate_set.get("downloaded_this_run", []))

    for video_id in read_archive_video_ids_for_metadata(slug, download_log):
        result = process_metadata_video(
            creator,
            video_id,
            playlist_membership,
            downloaded_this_run,
            download_log,
        )
        if result == "created":
            created += 1
        elif result == "rebuilt":
            rebuilt += 1
        elif result == "playlists_updated":
            playlists_updated += 1
        elif result == "skipped":
            skipped += 1
        elif result == "unchanged":
            pass

    download_log.info(
        "PRD 4 metadata pass complete: %s created, %s rebuilt, %s playlists-updated, %s skipped",
        created,
        rebuilt,
        playlists_updated,
        skipped,
    )


def run_refresh_mode(creators: list[dict[str, Any]]) -> None:
    total = RefreshCounts()
    for creator in creators:
        with creator_scope(creator["slug"]):
            setup_creator_environment(creator["slug"])
            counts = run_refresh(creator)
            total.metadata_changes += counts.metadata_changes
            total.availability_changes += counts.availability_changes
            total.upgrades_available += counts.upgrades_available
            total.transient_errors += counts.transient_errors

    print(
        f"Total: {total.metadata_changes} metadata changes, "
        f"{total.availability_changes} availability changes, "
        f"{total.upgrades_available} upgrades available, "
        f"{total.transient_errors} transient errors across {len(creators)} creators",
        flush=True,
    )


def run_refresh(creator: dict[str, Any]) -> RefreshCounts:
    slug = creator["slug"]
    _, _, errors_log, refresh_log = get_creator_loggers(slug)
    video_ids = read_archive_video_ids_for_metadata(slug, refresh_log)
    refresh_log.info("refresh starting (%s videos in archive.txt)", len(video_ids))

    counts = RefreshCounts()
    counts.upgrades_available = count_current_upgrades(slug, video_ids, refresh_log)
    for video_id in video_ids:
        result = refresh_one_video(creator, video_id, refresh_log, errors_log)
        counts.upgrades_available += result.upgrade_delta
        if result.state == "checked":
            counts.checked += 1
        elif result.state == "metadata_change":
            counts.checked += 1
            counts.metadata_changes += 1
        elif result.state == "availability_change":
            counts.checked += 1
            counts.availability_changes += 1
        elif result.state == "metadata_and_availability_change":
            counts.checked += 1
            counts.metadata_changes += 1
            counts.availability_changes += 1
        elif result.state == "transient_error":
            counts.checked += 1
            counts.transient_errors += 1
        elif result.state == "skipped":
            counts.skipped += 1

    refresh_log.info(
        "refresh complete: %s checked, %s metadata changes, %s availability changes, %s upgrades available, %s transient errors, %s skipped",
        counts.checked,
        counts.metadata_changes,
        counts.availability_changes,
        counts.upgrades_available,
        counts.transient_errors,
        counts.skipped,
    )
    print(
        f"{slug}: refresh — {counts.checked} checked, "
        f"{counts.metadata_changes} metadata changes, "
        f"{counts.availability_changes} availability changes, "
        f"{counts.upgrades_available} upgrades available, "
        f"{counts.transient_errors} transient errors, "
        f"{counts.skipped} skipped",
        flush=True,
    )
    return counts


def refresh_one_video(
    creator: dict[str, Any],
    video_id: str,
    refresh_log: logging.Logger,
    errors_log: logging.Logger,
) -> RefreshVideoResult:
    slug = creator["slug"]
    video_dir = DATA_DIR / slug / "videos" / video_id
    if not video_dir.exists():
        return RefreshVideoResult("skipped")

    metadata_path = video_dir / "metadata.json"
    if not metadata_path.exists():
        refresh_log.warning(
            "refresh skipped: %s (no metadata.json; run a normal sync first)",
            video_id,
        )
        return RefreshVideoResult("skipped")

    with metadata_path.open("r", encoding="utf-8") as metadata_file:
        metadata = json.load(metadata_file)

    invocation = refresh_video_metadata(creator, video_id)
    if invocation.state == "transient_error":
        errors_log.error(
            "refresh failed: %s — %s",
            video_id,
            log_safe_detail("\n".join(invocation.stderr_tail)),
        )
        refresh_log.warning("refresh transient error: %s", video_id)
        return RefreshVideoResult("transient_error")

    previous_last_check = metadata.get("last_metadata_check")
    now = utc_timestamp()
    prior_availability = metadata.get("availability")
    metadata_changed = False
    upgrade_delta = 0
    availability_changed = apply_availability_refresh(
        metadata,
        invocation.state,
        previous_last_check,
        now,
    )
    if availability_changed:
        refresh_log.info(
            "availability_change: %s (%s -> %s)",
            video_id,
            prior_availability,
            invocation.state,
        )

    if invocation.state == "available" and invocation.payload is not None:
        metadata_changed = apply_metadata_refresh(
            video_id,
            metadata,
            invocation.payload,
            previous_last_check,
            refresh_log,
        )
        upgrade_delta = apply_upgrade_detection(
            video_id,
            metadata,
            invocation.payload,
            now,
            refresh_log,
        )

    metadata["last_metadata_check"] = now
    write_json_atomic(metadata_path, metadata)

    if metadata_changed and availability_changed:
        return RefreshVideoResult("metadata_and_availability_change", upgrade_delta)
    if metadata_changed:
        return RefreshVideoResult("metadata_change", upgrade_delta)
    if availability_changed:
        return RefreshVideoResult("availability_change", upgrade_delta)
    return RefreshVideoResult("checked", upgrade_delta)


def refresh_video_metadata(creator: dict[str, Any], video_id: str) -> RefreshInvocationResult:
    command = [
        "yt-dlp",
        "--skip-download",
        "--no-warnings",
        "-f",
        creator["format"],
    ]
    if creator["format_sort"]:
        command.extend(["-S", ",".join(creator["format_sort"])])
    command.extend(["--dump-json", f"https://www.youtube.com/watch?v={video_id}"])

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return RefreshInvocationResult("transient_error", None, last_five_lines(stderr))

    if result.returncode != 0:
        return RefreshInvocationResult(
            classify_refresh_stderr(result.stderr),
            None,
            last_five_lines(result.stderr),
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return RefreshInvocationResult(
            "transient_error",
            None,
            last_five_lines(result.stderr or result.stdout),
        )
    if not isinstance(payload, dict):
        return RefreshInvocationResult("transient_error", None, last_five_lines(result.stderr))
    return RefreshInvocationResult("available", payload, last_five_lines(result.stderr))


def classify_refresh_stderr(stderr: str) -> str:
    if any(
        text in stderr
        for text in (
            "Video unavailable",
            "This video has been removed",
            "This video is no longer available",
            "has been terminated",
        )
    ):
        return "removed"
    if "Private video" in stderr or "This video is private" in stderr:
        return "private"
    if any(
        text in stderr
        for text in ("members-only", "members only", "requires a Channel Membership")
    ):
        return "members_only"
    if "Sign in to confirm your age" in stderr or "age-restricted" in stderr:
        return "age_restricted"
    return "transient_error"


def apply_metadata_refresh(
    video_id: str,
    metadata: dict[str, Any],
    payload: dict[str, Any],
    previous_last_check: Any,
    refresh_log: logging.Logger,
) -> bool:
    prior_current = metadata.get("current", {})
    if not isinstance(prior_current, dict):
        prior_current = {}
    new_current = {
        "title": string_or_empty(payload.get("title")),
        "thumbnail_url": string_or_empty(payload.get("thumbnail")),
        "description_hash": description_hash(payload.get("description")),
    }
    changed_fields = sorted(
        field
        for field in ("title", "thumbnail_url", "description_hash")
        if prior_current.get(field) != new_current[field]
    )
    if not changed_fields:
        return False

    history = metadata.setdefault("history", [])
    history.append(
        {
            "type": "metadata_change",
            "previous_observed_until": previous_last_check,
            "changed_fields": changed_fields,
            "previous": {
                "title": string_or_empty(prior_current.get("title")),
                "thumbnail_url": string_or_empty(prior_current.get("thumbnail_url")),
                "description_hash": string_or_empty(prior_current.get("description_hash")),
            },
        }
    )
    metadata["current"] = new_current
    refresh_log.info(
        "metadata_change: %s (changed: %s)",
        video_id,
        ", ".join(changed_fields),
    )
    return True


def apply_availability_refresh(
    metadata: dict[str, Any],
    new_availability: str,
    previous_last_check: Any,
    now: str,
) -> bool:
    prior_availability = metadata.get("availability")
    metadata["availability"] = new_availability

    if new_availability != "available":
        if metadata.get("removed_detected_at") is None:
            metadata["removed_detected_at"] = now
    elif prior_availability != "available":
        metadata["removed_detected_at"] = None

    if prior_availability == new_availability:
        return False

    metadata.setdefault("history", []).append(
        {
            "type": "availability_change",
            "previous_observed_until": previous_last_check,
            "previous": prior_availability,
        }
    )
    return True


def apply_upgrade_detection(
    video_id: str,
    metadata: dict[str, Any],
    payload: dict[str, Any],
    now: str,
    refresh_log: logging.Logger,
) -> int:
    prior_upgrade = metadata.get("upgrade_available")
    downloaded = metadata.get("downloaded", {})
    if not isinstance(downloaded, dict):
        downloaded = {}
    selected = selected_format_descriptor(payload, now)

    if selected["format_id"] != downloaded.get("format_id"):
        metadata["upgrade_available"] = selected
        if prior_upgrade is None:
            old_height = downloaded.get("height")
            refresh_log.info(
                "upgrade_available: %s (height %s -> %s)",
                video_id,
                old_height if old_height is not None else "unknown",
                selected["height"],
            )
            return 1
        return 0
    else:
        metadata["upgrade_available"] = None
        if prior_upgrade is not None:
            refresh_log.info("upgrade_cleared: %s", video_id)
            return -1
        return 0


def selected_format_descriptor(payload: dict[str, Any], detected_at: str) -> dict[str, Any]:
    return {
        "detected_at": detected_at,
        "format_id": string_or_empty(payload.get("format_id")),
        "height": optional_int_value(payload.get("height")),
        "vcodec": codec_or_none(payload.get("vcodec")),
        "acodec": codec_or_none(payload.get("acodec")),
        "filesize": refresh_filesize(payload),
    }


def refresh_filesize(payload: dict[str, Any]) -> int | None:
    filesize = optional_int_value(payload.get("filesize"))
    if filesize is not None:
        return filesize
    return optional_int_value(payload.get("filesize_approx"))


def count_current_upgrades(
    slug: str,
    video_ids: list[str],
    refresh_log: logging.Logger,
) -> int:
    count = 0
    for video_id in video_ids:
        metadata_path = DATA_DIR / slug / "videos" / video_id / "metadata.json"
        if not metadata_path.exists():
            continue
        try:
            with metadata_path.open("r", encoding="utf-8") as metadata_file:
                metadata = json.load(metadata_file)
        except OSError as exc:
            refresh_log.warning("metadata.json unreadable during upgrade count: %s", exc)
            continue
        except json.JSONDecodeError:
            continue
        if metadata.get("upgrade_available") is not None:
            count += 1
    return count


def last_five_lines(text: str) -> list[str]:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-5:]


def read_archive_video_ids_for_metadata(
    slug: str,
    download_log: logging.Logger,
) -> list[str]:
    archive_path = DATA_DIR / slug / "archive.txt"
    if not archive_path.exists():
        return []

    try:
        archive_text = archive_path.read_text(encoding="utf-8")
    except OSError as exc:
        download_log.warning("archive.txt exists but is unreadable: %s", exc)
        raise

    return parse_archive_video_ids(archive_text)


def parse_archive_video_ids(archive_text: str) -> list[str]:
    video_ids: list[str] = []
    for line in archive_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) == 2 and parts[0] == "youtube" and parts[1]:
            video_ids.append(parts[1])
    return video_ids


def process_metadata_video(
    creator: dict[str, Any],
    video_id: str,
    playlist_membership: dict[str, list[dict[str, Any]]],
    downloaded_this_run: set[str],
    download_log: logging.Logger,
) -> str:
    slug = creator["slug"]
    video_dir = DATA_DIR / slug / "videos" / video_id
    if not video_dir.exists():
        return "skipped"

    media_path = video_dir / f"{video_id}.{creator['merge_output_format']}"
    metadata_path = video_dir / "metadata.json"
    info_path = video_dir / f"{video_id}.info.json"
    if not media_path.exists():
        if not info_path.exists() and not metadata_path.exists():
            download_log.warning(
                "metadata.json skipped: %s (info.json missing — folder corruption?)",
                video_id,
            )
        else:
            download_log.warning("metadata.json skipped: %s (media file missing)", video_id)
        return "skipped"

    playlists = copy_playlist_membership(video_id, playlist_membership)

    if metadata_path.exists():
        with metadata_path.open("r", encoding="utf-8") as metadata_file:
            metadata = json.load(metadata_file)
        old_playlists = metadata.get("playlists")
        metadata["playlists"] = playlists
        write_json_atomic(metadata_path, metadata)
        if old_playlists != playlists:
            download_log.info(
                "metadata.json playlists updated: %s (%s entries)",
                video_id,
                len(playlists),
            )
            return "playlists_updated"
        return "unchanged"

    if not info_path.exists():
        download_log.warning(
            "metadata.json skipped: %s (info.json missing — folder corruption?)",
            video_id,
        )
        return "skipped"

    metadata = build_metadata_from_info(
        slug,
        video_id,
        info_path,
        media_path,
        playlists,
    )
    write_json_atomic(metadata_path, metadata)
    if video_id in downloaded_this_run:
        download_log.info("metadata.json created: %s", video_id)
        return "created"
    download_log.info("metadata.json rebuilt: %s (was missing)", video_id)
    return "rebuilt"


def build_metadata_from_info(
    slug: str,
    video_id: str,
    info_path: pathlib.Path,
    media_path: pathlib.Path,
    playlists: list[dict[str, Any]],
) -> dict[str, Any]:
    with info_path.open("r", encoding="utf-8") as info_file:
        info = json.load(info_file)
    timestamp = utc_timestamp()
    return build_metadata_dict(
        video_id=video_id,
        source_creator=slug,
        archived_at=timestamp,
        last_metadata_check=timestamp,
        title=string_or_empty(info.get("title")),
        thumbnail_url=string_or_empty(info.get("thumbnail")),
        description_hash_value=description_hash(info.get("description")),
        format_id=string_or_empty(info.get("format_id")),
        height=optional_int_value(info.get("height")),
        vcodec=codec_or_none(info.get("vcodec")),
        acodec=codec_or_none(info.get("acodec")),
        filesize=metadata_filesize(info, media_path),
        playlists=playlists,
    )


def build_metadata_dict(
    *,
    video_id: str,
    source_creator: str,
    archived_at: str,
    last_metadata_check: str,
    title: str,
    thumbnail_url: str,
    description_hash_value: str,
    format_id: str,
    height: int | None,
    vcodec: str,
    acodec: str,
    filesize: int,
    playlists: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "video_id": video_id,
        "source_creator": source_creator,
        "archived_at": archived_at,
        "last_metadata_check": last_metadata_check,
        "availability": "available",
        "removed_detected_at": None,
        "current": {
            "title": title,
            "thumbnail_url": thumbnail_url,
            "description_hash": description_hash_value,
        },
        "downloaded": {
            "format_id": format_id,
            "height": height,
            "vcodec": vcodec,
            "acodec": acodec,
            "filesize": filesize,
        },
        "upgrade_available": None,
        "history": [],
        "playlists": playlists,
    }


def description_hash(description: Any) -> str:
    if not isinstance(description, str):
        description = ""
    digest = hashlib.sha256(description.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def metadata_filesize(info: dict[str, Any], media_path: pathlib.Path) -> int:
    filesize = optional_int_value(info.get("filesize"))
    if filesize is not None:
        return filesize
    filesize_approx = optional_int_value(info.get("filesize_approx"))
    if filesize_approx is not None:
        return filesize_approx
    return os.path.getsize(media_path)


def copy_playlist_membership(
    video_id: str,
    playlist_membership: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    return [dict(item) for item in playlist_membership.get(video_id, [])]


def string_or_empty(value: Any) -> str:
    return value if isinstance(value, str) else ""


def codec_or_none(value: Any) -> str:
    if isinstance(value, str) and value:
        return value
    return "none"


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
    _, manifests_log, _, _ = get_creator_loggers(slug)
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

    write_json_atomic(creator_json_path, snapshot)


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
    video_manifest_entries: dict[str, dict[str, Any]],
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
        merge_manifest_entry(video_id, entry, video_manifest_entries)
        playlist_membership.setdefault(video_id, []).append(
            {"id": playlist_id, "title": playlist_title, "index": index}
        )

    return video_ids


def fetch_uploads_manifest(
    slug: str,
    canonical_url: str,
    utc_date: str,
) -> list[tuple[str, dict[str, Any]]]:
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

    video_entries: list[tuple[str, dict[str, Any]]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        video_id = entry.get("id")
        if isinstance(video_id, str) and video_id:
            video_entries.append((video_id, entry))
    return video_entries


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


def optional_int_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def optional_string_value(value: Any) -> str | None:
    return value if isinstance(value, str) and value != "NA" else None


def parse_printed_int(value: str) -> int | None:
    return optional_int_value(None if value == "NA" else value)


def iso_timestamp(value: datetime.datetime) -> str:
    return value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def merge_manifest_entry(
    video_id: str,
    entry: dict[str, Any],
    video_manifest_entries: dict[str, dict[str, Any]],
) -> None:
    current_entry = video_manifest_entries.get(video_id)
    if current_entry is None:
        video_manifest_entries[video_id] = dict(entry)
        return

    for key in ("timestamp", "live_status", "release_timestamp"):
        if current_entry.get(key) is None and entry.get(key) is not None:
            current_entry[key] = entry[key]


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


def write_json_atomic(path: pathlib.Path, obj: dict[str, Any]) -> None:
    data = json.dumps(
        obj,
        indent=2,
        ensure_ascii=False,
        sort_keys=False,
    ).encode("utf-8")
    atomic_write_bytes(path, data + b"\n")


def atomic_write_bytes(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with tmp_path.open("wb") as tmp_file:
        tmp_file.write(data)
    os.replace(tmp_path, path)


def get_creator_loggers(
    slug: str,
) -> tuple[logging.Logger, logging.Logger, logging.Logger, logging.Logger]:
    return (
        _configure_creator_logger(slug, "download"),
        _configure_creator_logger(slug, "manifests"),
        _configure_creator_logger(slug, "errors"),
        _configure_creator_logger(slug, "refresh"),
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
        _, _, errors_log, _ = get_creator_loggers(slug)
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
