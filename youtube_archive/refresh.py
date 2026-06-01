from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any

from youtube_archive.config import setup_creator_environment
from youtube_archive.errors import creator_scope, last_five_lines, log_safe_detail
from youtube_archive.logging_setup import get_creator_loggers
from youtube_archive.metadata import description_hash, read_archive_video_ids_for_metadata
from youtube_archive.utils import (
    codec_or_none,
    data_dir,
    optional_int_value,
    string_or_empty,
    utc_timestamp,
    write_json_atomic,
)


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
    _, _, errors_log, refresh_log, _, _ = get_creator_loggers(slug)
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
    video_dir = data_dir() / slug / "videos" / video_id
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
        metadata_path = data_dir() / slug / "videos" / video_id / "metadata.json"
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
