from __future__ import annotations

import collections
import datetime
import fnmatch
import json
import logging
import pathlib
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

from youtube_archive.errors import log_safe_detail
from youtube_archive.logging_setup import get_creator_loggers
from youtube_archive.process import run_yt_dlp_capture
from youtube_archive.utils import (
    cookies_args,
    data_dir,
    iso_timestamp,
    optional_int_value,
    optional_string_value,
    parse_archive_video_ids,
    parse_printed_int,
    staging_dir,
    utc_timestamp,
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
    skipped_unavailable: list[str]


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
        "Pass 2 filtering: %s already in archive.txt, %s flagged unavailable, %s eligible, "
        "%s gated (live/premiere), %s gated (too recent)",
        len(buckets.already_archived),
        len(buckets.skipped_unavailable),
        len(buckets.eligible_sorted),
        len(buckets.gated_live_or_premiere),
        len(buckets.gated_too_recent),
    )

    for gated_video in buckets.gated_live_or_premiere + buckets.gated_too_recent:
        log_gated_video(gated_video, creator["min_upload_age_hours"], download_log)

    total = len(buckets.eligible_sorted)
    print(
        f"{slug}: Pass 2 — {total} to download "
        f"({len(buckets.already_archived)} already archived, "
        f"{len(buckets.skipped_unavailable)} unavailable, {gated_count} gated)",
        flush=True,
    )

    downloaded = 0
    unavailable = 0
    failed = 0
    for index, eligible_video in enumerate(buckets.eligible_sorted, start=1):
        video_id = eligible_video.video_id
        download_log.info("==> downloading %s", video_id)
        print(f"  [{index}/{total}] downloading {video_id} …", flush=True)
        started = time.monotonic()
        subtitle_choice = resolve_subtitle_choice(
            video_id,
            creator["subtitle_preferences"],
            download_log,
        )
        command = build_download_command(creator, video_id, subtitle_choice)
        result = run_download_subprocess(command, download_log)
        if result.returncode != 0 and is_stale_resume_failure(result.tail_lines):
            removed = clear_partial_downloads(creator, video_id)
            download_log.warning(
                "stale resume for %s (HTTP 416); cleared %s, retrying from scratch",
                video_id,
                removed,
            )
            print(
                f"  [{index}/{total}] stale resume for {video_id}; retrying clean …",
                flush=True,
            )
            result = run_download_subprocess(command, download_log)
        elif result.returncode != 0 and is_transient_io_failure(result.tail_lines):
            download_log.warning(
                "transient I/O failure for %s (destination volume may have dropped); retrying",
                video_id,
            )
            print(
                f"  [{index}/{total}] I/O error for {video_id}; retrying …",
                flush=True,
            )
            result = run_download_subprocess(command, download_log)
        elapsed = round(time.monotonic() - started)
        if result.returncode == 0:
            downloaded += 1
            candidate_set["downloaded_this_run"].append(video_id)
            remove_staging_dir(creator, video_id)
            download_log.info("<-- done: %s", video_id)
            print(f"  [{index}/{total}] done: {video_id} ({elapsed}s)", flush=True)
        else:
            unavailable_reason = classify_unavailable(result.tail_lines)
            if unavailable_reason is not None:
                unavailable += 1
                record_unavailable_video(slug, video_id, unavailable_reason)
                download_log.warning(
                    "<-- UNAVAILABLE (%s): %s — flagged, will skip on future runs",
                    unavailable_reason,
                    video_id,
                )
                print(
                    f"  [{index}/{total}] unavailable ({unavailable_reason}): {video_id} "
                    f"({elapsed}s; flagged, will skip next run)",
                    flush=True,
                )
            else:
                failed += 1
                download_log.info("<-- FAILED: %s", video_id)
                errors_log.error(
                    "download failed: %s — %s",
                    video_id,
                    log_safe_detail("\n".join(result.tail_lines)),
                )
                print(
                    f"  [{index}/{total}] FAILED: {video_id} ({elapsed}s; see logs/errors.log)",
                    flush=True,
                )

    download_log.info(
        "Pass 2 complete: %s downloaded, %s gated, %s unavailable, %s failed",
        downloaded,
        gated_count,
        unavailable,
        failed,
    )
    print(
        f"{slug}: Pass 2 — {downloaded} downloaded, {gated_count} gated, "
        f"{unavailable} unavailable, {failed} failed",
        flush=True,
    )


def build_download_work_list(
    creator: dict[str, Any],
    candidate_set: dict[str, Any],
    download_log: logging.Logger,
) -> WorkBuckets:
    slug = creator["slug"]
    archive_ids = read_download_archive(slug, download_log)
    unavailable_ids = read_unavailable_ids(slug, download_log)
    already_archived: list[str] = []
    skipped_unavailable: list[str] = []
    eligible: list[EligibleVideo] = []
    gated_live_or_premiere: list[GatedVideo] = []
    gated_too_recent: list[GatedVideo] = []
    entries = candidate_set.get("video_manifest_entries", {})
    now = datetime.datetime.now(datetime.timezone.utc)

    for video_id in candidate_set["candidate_video_ids"]:
        if video_id in archive_ids:
            already_archived.append(video_id)
            continue
        if video_id in unavailable_ids:
            skipped_unavailable.append(video_id)
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
        skipped_unavailable=skipped_unavailable,
    )


def read_download_archive(slug: str, download_log: logging.Logger) -> set[str]:
    archive_path = data_dir() / slug / "archive.txt"
    if not archive_path.exists():
        return set()

    try:
        archive_text = archive_path.read_text(encoding="utf-8")
    except OSError as exc:
        download_log.warning("archive.txt exists but is unreadable: %s", exc)
        raise

    return set(parse_archive_video_ids(archive_text))


def unavailable_archive_path(slug: str) -> pathlib.Path:
    return data_dir() / slug / "unavailable.txt"


def read_unavailable_ids(slug: str, download_log: logging.Logger) -> set[str]:
    """Video IDs previously flagged as permanently gone (see classify_unavailable).
    These are skipped in Pass 2 so removed videos stop re-failing every run. An
    unreadable file degrades to "no flags" rather than aborting the run."""
    path = unavailable_archive_path(slug)
    if not path.exists():
        return set()

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        download_log.warning("unavailable.txt exists but is unreadable: %s", exc)
        return set()

    ids: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        ids.add(stripped.split()[0])
    return ids


def record_unavailable_video(slug: str, video_id: str, reason: str) -> None:
    path = unavailable_archive_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with path.open("a", encoding="utf-8") as handle:
        if write_header:
            handle.write(
                "# Permanently unavailable videos, skipped on future runs.\n"
                "# video_id<TAB>reason<TAB>flagged_at — delete a line to allow re-download.\n"
            )
        handle.write(f"{video_id}\t{reason}\t{utc_timestamp()}\n")


_UNAVAILABLE_MARKERS: tuple[tuple[str, str], ...] = (
    ("removed by the uploader", "removed by uploader"),
    ("account associated with this video has been terminated", "account terminated"),
    ("removed for violating", "removed for policy violation"),
    ("This video is no longer available", "no longer available"),
)


def classify_unavailable(tail_lines: list[str]) -> str | None:
    """Detect a download failure where the video is permanently gone — removed by
    the uploader, the channel terminated, a policy takedown, or otherwise no
    longer available. These never recover, so the caller records the ID to skip it
    on future runs. Private videos and bare "Video unavailable" are intentionally
    excluded: those can be transient or reversible."""
    text = "\n".join(tail_lines)
    for marker, reason in _UNAVAILABLE_MARKERS:
        if marker in text:
            return reason
    return None


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
            *cookies_args(),
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
            *cookies_args(),
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
        *cookies_args(),
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
                str(data_dir() / creator["slug"] / "archive.txt"),
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
    # Download + merge + embed happen in a local staging dir, then yt-dlp moves
    # the finished files into data_dir. This keeps yt-dlp's long-held write
    # handle off network mounts, where close()-on-flush can fail (EBADF/EINVAL).
    # --paths is honored only when -o is a relative template, hence the split.
    command.extend(
        [
            "--convert-thumbnails",
            "webp",
            "--embed-metadata",
            "--embed-thumbnail",
            "--no-progress",
            "-P",
            f"home:{data_dir() / creator['slug'] / 'videos'}",
            "-P",
            f"temp:{staging_dir() / creator['slug']}",
            "-o",
            "%(id)s/%(id)s.%(ext)s",
            f"https://www.youtube.com/watch?v={video_id}",
        ]
    )
    return command


def is_stale_resume_failure(tail_lines: list[str]) -> bool:
    """A leftover .part file from an earlier run can no longer be resumed once
    YouTube's signed format URL rotates: yt-dlp's Range request comes back as
    HTTP 416. Detect that so the caller can clear the partials and restart."""
    text = "\n".join(tail_lines)
    return "HTTP Error 416" in text or "Requested range not satisfiable" in text


_ERRNO_DOWNLOAD_RE = re.compile(r"Unable to download video: \[Errno \d+\]")


def is_transient_io_failure(tail_lines: list[str]) -> bool:
    """A write to the destination volume can fail mid-download when the mount
    drops out or hiccups — an external drive sleeping, a network share
    reconnecting. yt-dlp surfaces these as OS-level errno errors at the download
    step ("Unable to download video: [Errno N] …", e.g. EBADF "Bad file
    descriptor", EINVAL "Invalid argument", EIO "Input/output error"). They are
    not tied to the video itself, so a retry usually succeeds once the volume is
    stable."""
    return bool(_ERRNO_DOWNLOAD_RE.search("\n".join(tail_lines)))


def clear_partial_downloads(creator: dict[str, Any], video_id: str) -> list[str]:
    """Delete a video's leftover .part/.ytdl files so the next attempt downloads
    from scratch. Partials live in the local staging dir, not data_dir, since
    that's where yt-dlp writes before moving the finished file. Returns the names
    removed."""
    video_dir = staging_dir() / creator["slug"] / video_id
    removed: list[str] = []
    if not video_dir.is_dir():
        return removed
    for path in sorted(video_dir.iterdir()):
        if not path.is_file():
            continue
        if ".part" in path.name or path.name.endswith(".ytdl"):
            path.unlink()
            removed.append(path.name)
    return removed


def remove_staging_dir(creator: dict[str, Any], video_id: str) -> None:
    """Drop the per-video staging dir once yt-dlp has moved the finished files
    into data_dir, so local scratch doesn't accumulate empty dirs over a run."""
    shutil.rmtree(staging_dir() / creator["slug"] / video_id, ignore_errors=True)


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
