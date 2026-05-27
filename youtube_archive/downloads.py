from __future__ import annotations

import collections
import datetime
import fnmatch
import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from youtube_archive.errors import log_safe_detail
from youtube_archive.logging_setup import get_creator_loggers
from youtube_archive.process import run_yt_dlp_capture
from youtube_archive.utils import (
    DATA_DIR,
    iso_timestamp,
    optional_int_value,
    optional_string_value,
    parse_archive_video_ids,
    parse_printed_int,
)


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


def run_pass_two(creator: dict[str, Any], candidate_set: dict[str, Any]) -> None:
    slug = creator["slug"]
    download_log, _, errors_log, _, _, _ = get_creator_loggers(slug)
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
        subtitle_choice = resolve_subtitle_choice(
            video_id,
            creator["subtitle_preferences"],
            download_log,
        )
        command = build_download_command(creator, video_id, subtitle_choice)
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


def probe_subtitles(
    video_id: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Probe yt-dlp for available subtitles. Returns (manual, auto) dicts keyed by
    language code, or None if the probe failed."""
    result = run_yt_dlp_capture(
        [
            "yt-dlp",
            "--skip-download",
            "--no-warnings",
            "--print",
            "%(subtitles)j",
            "--print",
            "%(automatic_captions)j",
            f"https://www.youtube.com/watch?v={video_id}",
        ]
    )
    if result.returncode != 0:
        return None
    lines = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
    if len(lines) < 2:
        return None
    try:
        manual = json.loads(lines[-2])
        auto = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(manual, dict):
        manual = {}
    if not isinstance(auto, dict):
        auto = {}
    return manual, auto


def pick_subtitle(
    manual: dict[str, Any],
    auto: dict[str, Any],
    preferences: list[str],
) -> tuple[str, str] | None:
    """Walk the preference list against available languages. Each preference is a
    glob pattern (e.g. "en", "en-US", "en-*", "*"). Manual subtitles win over
    auto-generated ones for the same preference order. Returns ("manual", lang),
    ("auto", lang), or None if nothing matches."""
    for pref in preferences:
        match = _first_glob_match(pref, manual)
        if match is not None:
            return "manual", match
    for pref in preferences:
        match = _first_glob_match(pref, auto)
        if match is not None:
            return "auto", match
    return None


def _first_glob_match(pattern: str, available: dict[str, Any]) -> str | None:
    matches = sorted(key for key in available if fnmatch.fnmatchcase(key, pattern))
    return matches[0] if matches else None


def resolve_subtitle_choice(
    video_id: str,
    preferences: list[str],
    download_log: logging.Logger,
) -> tuple[str, str] | None:
    if not preferences:
        return None
    probed = probe_subtitles(video_id)
    if probed is None:
        download_log.warning(
            "subtitle probe failed for %s; downloading without subtitles",
            video_id,
        )
        return None
    manual, auto = probed
    choice = pick_subtitle(manual, auto, preferences)
    if choice is None:
        download_log.info(
            "no matching subtitle for %s (preferences=%s, manual=%s, auto=%s)",
            video_id,
            preferences,
            sorted(manual.keys()),
            sorted(auto.keys()),
        )
    else:
        kind, lang = choice
        download_log.info("subtitle selected for %s: %s (%s)", video_id, lang, kind)
    return choice


def build_download_command(
    creator: dict[str, Any],
    video_id: str,
    subtitle_choice: tuple[str, str] | None,
    *,
    include_download_archive: bool = True,
) -> list[str]:
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
        ]
    )
    if include_download_archive:
        command.extend(
            [
                "--download-archive",
                str(DATA_DIR / creator["slug"] / "archive.txt"),
            ]
        )
    command.extend(
        [
            "--write-info-json",
            "--write-thumbnail",
        ]
    )
    if subtitle_choice is not None:
        kind, lang = subtitle_choice
        if kind == "manual":
            command.append("--write-subs")
        else:
            command.append("--write-auto-subs")
        command.extend(["--sub-langs", lang])
    command.extend(
        [
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
