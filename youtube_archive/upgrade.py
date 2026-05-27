from __future__ import annotations

import json
import logging
import os
import pathlib
from dataclasses import dataclass
from typing import Any

from youtube_archive.config import setup_creator_environment
from youtube_archive.downloads import (
    DownloadResult,
    build_download_command,
    resolve_subtitle_choice,
    run_download_subprocess,
)
from youtube_archive.errors import creator_scope, log_safe_detail
from youtube_archive.logging_setup import get_creator_loggers
from youtube_archive.metadata import metadata_filesize
from youtube_archive.utils import (
    DATA_DIR,
    codec_or_none,
    optional_int_value,
    parse_archive_video_ids,
    string_or_empty,
    utc_timestamp,
    write_json_atomic,
)


@dataclass(frozen=True)
class UpgradeTarget:
    video_id: str
    video_dir: pathlib.Path
    metadata_path: pathlib.Path
    media_path: pathlib.Path
    detected_at: str
    metadata: dict[str, Any]


@dataclass
class UpgradeCounts:
    targets: int = 0
    succeeded: int = 0
    no_op: int = 0
    failed: int = 0
    skipped: int = 0

    def add(self, other: "UpgradeCounts") -> None:
        self.targets += other.targets
        self.succeeded += other.succeeded
        self.no_op += other.no_op
        self.failed += other.failed
        self.skipped += other.skipped


class UpgradeFailure(Exception):
    def __init__(self, reason: str, tail_lines: list[str] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.tail_lines = tail_lines or []


def run_upgrade_mode(creators: list[dict[str, Any]], *, dry_run: bool = False) -> None:
    recovery_skips_by_slug: dict[str, set[str]] = {}
    for creator in creators:
        slug = creator["slug"]
        with creator_scope(slug):
            setup_creator_environment(slug)
            _, _, errors_log, _, upgrade_log = get_creator_loggers(slug)
            if dry_run:
                upgrade_log.info("upgrade dry-run: startup recovery skipped")
                recovery_skips_by_slug[slug] = set()
            else:
                recovery_skips_by_slug[slug] = recover_pre_upgrade_leftovers(
                    creator,
                    upgrade_log,
                    errors_log,
                )

    total = UpgradeCounts()
    for creator in creators:
        slug = creator["slug"]
        recovery_skips_by_slug.setdefault(slug, set())
        with creator_scope(slug):
            setup_creator_environment(slug)
            counts = run_upgrade(
                creator,
                recovery_skips=recovery_skips_by_slug.get(slug, set()),
                dry_run=dry_run,
            )
            total.add(counts)

    print(
        f"Total: {total.succeeded} succeeded, {total.no_op} no-op, "
        f"{total.failed} failed, {total.skipped} skipped across {len(creators)} creators",
        flush=True,
    )


def run_upgrade(
    creator: dict[str, Any],
    *,
    recovery_skips: set[str],
    dry_run: bool = False,
) -> UpgradeCounts:
    slug = creator["slug"]
    download_log, _, errors_log, _, upgrade_log = get_creator_loggers(slug)
    targets, skipped = select_upgrade_targets(creator, recovery_skips, upgrade_log)
    counts = UpgradeCounts(targets=len(targets), skipped=skipped)
    if not targets:
        upgrade_log.info("upgrade: 0 targets")
        print_creator_summary(slug, counts)
        return counts

    upgrade_log.info("upgrade pass starting (%s targets after filtering)", len(targets))
    if dry_run:
        upgrade_log.info("upgrade dry-run: would process %s targets", len(targets))
        print_creator_summary(slug, counts)
        return counts

    for target in targets:
        result = upgrade_one_video(creator, target, download_log, upgrade_log, errors_log)
        if result == "succeeded":
            counts.succeeded += 1
        elif result == "no_op":
            counts.no_op += 1
        elif result == "failed":
            counts.failed += 1

    upgrade_log.info(
        "upgrade complete: %s succeeded, %s no-op, %s failed, %s skipped",
        counts.succeeded,
        counts.no_op,
        counts.failed,
        counts.skipped,
    )
    print_creator_summary(slug, counts)
    return counts


def recover_pre_upgrade_leftovers(
    creator: dict[str, Any],
    upgrade_log: logging.Logger,
    errors_log: logging.Logger,
) -> set[str]:
    slug = creator["slug"]
    videos_dir = DATA_DIR / slug / "videos"
    archive_ids = read_archive_ids(slug, upgrade_log)
    skipped_video_ids: set[str] = set()
    if not videos_dir.exists():
        return skipped_video_ids

    for video_dir in sorted(path for path in videos_dir.iterdir() if path.is_dir()):
        pre_upgrade_files = sorted(video_dir.glob("*.pre-upgrade"))
        if not pre_upgrade_files:
            continue

        video_id = video_dir.name
        media_path = canonical_media_path(creator, video_id)
        info_path = video_dir / f"{video_id}.info.json"

        if video_id not in archive_ids:
            upgrade_log.warning(
                "startup recovery: orphan .pre-upgrade files for %s; leaving untouched",
                video_id,
            )
            continue

        if media_path.exists() and os.path.getsize(media_path) > 0 and info_path.exists():
            message = (
                "startup recovery: ambiguous .pre-upgrade leftovers for "
                f"{video_id} — both old and new media present. Skipping this video; "
                "manually delete either the .pre-upgrade files or the new files before "
                "re-running --upgrade."
            )
            upgrade_log.warning(message)
            errors_log.warning(message)
            skipped_video_ids.add(video_id)
            continue

        # If new media exists without fresh info.json, treat it as incomplete and
        # restore the old sidecar set; PRD 6 only defines the media+info case as ambiguous.
        restore_pre_upgrade_files(video_dir)
        delete_part_files(video_dir, video_id)
        upgrade_log.info(
            "startup recovery: restored %s from .pre-upgrade (canonical media missing)",
            video_id,
        )

    return skipped_video_ids


def select_upgrade_targets(
    creator: dict[str, Any],
    recovery_skips: set[str],
    upgrade_log: logging.Logger,
) -> tuple[list[UpgradeTarget], int]:
    slug = creator["slug"]
    videos_dir = DATA_DIR / slug / "videos"
    if not videos_dir.exists():
        return [], 0

    skipped = len(recovery_skips)
    targets: list[UpgradeTarget] = []
    for metadata_path in sorted(videos_dir.glob("*/metadata.json")):
        video_id = metadata_path.parent.name
        if video_id in recovery_skips:
            continue
        try:
            with metadata_path.open("r", encoding="utf-8") as metadata_file:
                metadata = json.load(metadata_file)
        except (OSError, json.JSONDecodeError) as exc:
            upgrade_log.warning("upgrade skipped: %s (metadata unreadable: %s)", video_id, exc)
            skipped += 1
            continue

        if metadata.get("upgrade_available") is None:
            continue

        availability = metadata.get("availability")
        if availability != "available":
            upgrade_log.info("upgrade skipped: %s (availability: %s)", video_id, availability)
            skipped += 1
            continue

        media_path = canonical_media_path(creator, video_id)
        if not media_path.exists():
            upgrade_log.warning("upgrade skipped: %s (canonical media missing)", video_id)
            skipped += 1
            continue

        upgrade_available = metadata.get("upgrade_available")
        detected_at = ""
        if isinstance(upgrade_available, dict):
            detected_at = string_or_empty(upgrade_available.get("detected_at"))
        targets.append(
            UpgradeTarget(
                video_id=video_id,
                video_dir=metadata_path.parent,
                metadata_path=metadata_path,
                media_path=media_path,
                detected_at=detected_at,
                metadata=metadata,
            )
        )

    targets.sort(key=lambda target: (target.detected_at, target.video_id))
    return targets, skipped


def upgrade_one_video(
    creator: dict[str, Any],
    target: UpgradeTarget,
    download_log: logging.Logger,
    upgrade_log: logging.Logger,
    errors_log: logging.Logger,
) -> str:
    video_id = target.video_id
    from_downloaded = dict(target.metadata.get("downloaded", {}))
    renamed: list[pathlib.Path] = []
    result: DownloadResult | None = None

    try:
        renamed = move_sidecars_to_pre_upgrade(target.video_dir)
        subtitle_choice = resolve_subtitle_choice(
            video_id,
            creator["subtitle_preferences"],
            download_log,
        )
        command = build_upgrade_command(creator, video_id, subtitle_choice)
        download_log.info("==> upgrading %s", video_id)
        result = run_download_subprocess(command, download_log)
        if result.returncode != 0:
            download_log.info("<-- FAILED: %s", video_id)
            raise UpgradeFailure(
                f"yt-dlp exited with {result.returncode}",
                result.tail_lines,
            )
        download_log.info("<-- done: %s", video_id)

        fresh_info_path = target.video_dir / f"{video_id}.info.json"
        if not target.media_path.exists() or os.path.getsize(target.media_path) == 0:
            raise UpgradeFailure("canonical media missing after yt-dlp", result.tail_lines)
        if not fresh_info_path.exists():
            raise UpgradeFailure("fresh info.json missing after yt-dlp", result.tail_lines)

        new_downloaded = build_downloaded_block(fresh_info_path, target.media_path)
        old_format = string_or_empty(from_downloaded.get("format_id"))
        new_format = string_or_empty(new_downloaded.get("format_id"))
        if new_format == old_format:
            target.metadata["upgrade_available"] = None
            write_json_atomic(target.metadata_path, target.metadata)
            delete_pre_upgrade_files(target.video_dir)
            upgrade_log.info(
                "upgrade no-op: %s (format_id unchanged: %s; upgrade_available cleared)",
                video_id,
                new_format,
            )
            return "no_op"

        target.metadata["downloaded"] = new_downloaded
        target.metadata["upgrade_available"] = None
        target.metadata.setdefault("history", []).append(
            {
                "type": "upgrade",
                "observed_at": utc_timestamp(),
                "from": from_downloaded,
                "to": new_downloaded,
            }
        )
        write_json_atomic(target.metadata_path, target.metadata)
        delete_pre_upgrade_files(target.video_dir)
        upgrade_log.info(
            "upgrade succeeded: %s (%s -> %s, height %s -> %s)",
            video_id,
            old_format,
            new_format,
            from_downloaded.get("height"),
            new_downloaded.get("height"),
        )
        return "succeeded"
    except Exception as exc:
        reason = exc.reason if isinstance(exc, UpgradeFailure) else str(exc)
        tail_lines = exc.tail_lines if isinstance(exc, UpgradeFailure) else []
        if result is not None and not tail_lines:
            tail_lines = result.tail_lines
        return handle_upgrade_failure(
            target,
            reason,
            tail_lines,
            upgrade_log,
            errors_log,
            renamed,
        )


def build_upgrade_command(
    creator: dict[str, Any],
    video_id: str,
    subtitle_choice: tuple[str, str] | None,
) -> list[str]:
    return build_download_command(
        creator,
        video_id,
        subtitle_choice,
        # The video ID is already in archive.txt; passing this would make yt-dlp skip.
        include_download_archive=False,
    )


def move_sidecars_to_pre_upgrade(video_dir: pathlib.Path) -> list[pathlib.Path]:
    renamed: list[pathlib.Path] = []
    for path in sorted(video_dir.iterdir()):
        if not path.is_file():
            continue
        if path.name in {"metadata.json", "metadata.json.tmp"}:
            continue
        if path.name.endswith(".pre-upgrade"):
            continue
        destination = path.with_name(f"{path.name}.pre-upgrade")
        try:
            os.rename(path, destination)
        except OSError:
            restore_renamed_paths(renamed)
            raise
        renamed.append(destination)
    return renamed


def restore_renamed_paths(paths: list[pathlib.Path]) -> None:
    for pre_upgrade_path in reversed(paths):
        original_path = original_path_for_pre_upgrade(pre_upgrade_path)
        os.rename(pre_upgrade_path, original_path)


def restore_pre_upgrade_files(video_dir: pathlib.Path) -> None:
    for pre_upgrade_path in sorted(video_dir.glob("*.pre-upgrade")):
        original_path = original_path_for_pre_upgrade(pre_upgrade_path)
        if original_path.exists():
            os.unlink(original_path)
        os.rename(pre_upgrade_path, original_path)


def delete_pre_upgrade_files(video_dir: pathlib.Path) -> None:
    for pre_upgrade_path in sorted(video_dir.glob("*.pre-upgrade")):
        os.unlink(pre_upgrade_path)


def delete_part_files(video_dir: pathlib.Path, video_id: str) -> None:
    for part_path in sorted(video_dir.glob(f"{video_id}.*.part")):
        os.unlink(part_path)


def original_path_for_pre_upgrade(pre_upgrade_path: pathlib.Path) -> pathlib.Path:
    return pre_upgrade_path.with_name(pre_upgrade_path.name.removesuffix(".pre-upgrade"))


def build_downloaded_block(info_path: pathlib.Path, media_path: pathlib.Path) -> dict[str, Any]:
    with info_path.open("r", encoding="utf-8") as info_file:
        info = json.load(info_file)
    return {
        "format_id": string_or_empty(info.get("format_id")),
        "height": optional_int_value(info.get("height")),
        "vcodec": codec_or_none(info.get("vcodec")),
        "acodec": codec_or_none(info.get("acodec")),
        "filesize": metadata_filesize(info, media_path),
    }


def handle_upgrade_failure(
    target: UpgradeTarget,
    reason: str,
    tail_lines: list[str],
    upgrade_log: logging.Logger,
    errors_log: logging.Logger,
    renamed: list[pathlib.Path],
) -> str:
    try:
        restore_pre_upgrade_files(target.video_dir)
        delete_part_files(target.video_dir, target.video_id)
    except Exception:
        errors_log.error(
            "upgrade rollback FAILED: %s — manual intervention required; check %s",
            target.video_id,
            target.video_dir,
        )
        return "failed"

    upgrade_log.warning(
        "upgrade failed: %s — %s; rollback completed",
        target.video_id,
        reason,
    )
    detail = "\n".join(tail_lines) if tail_lines else reason
    errors_log.error(
        "upgrade failed: %s — %s",
        target.video_id,
        log_safe_detail(detail),
    )
    return "failed"


def canonical_media_path(creator: dict[str, Any], video_id: str) -> pathlib.Path:
    return DATA_DIR / creator["slug"] / "videos" / video_id / (
        f"{video_id}.{creator['merge_output_format']}"
    )


def read_archive_ids(slug: str, upgrade_log: logging.Logger) -> set[str]:
    archive_path = DATA_DIR / slug / "archive.txt"
    if not archive_path.exists():
        return set()
    try:
        return set(parse_archive_video_ids(archive_path.read_text(encoding="utf-8")))
    except OSError as exc:
        upgrade_log.warning("archive.txt exists but is unreadable: %s", exc)
        raise


def print_creator_summary(slug: str, counts: UpgradeCounts) -> None:
    print(
        f"{slug}: upgrade — {counts.targets} targets, {counts.succeeded} succeeded, "
        f"{counts.no_op} no-op, {counts.failed} failed, {counts.skipped} skipped",
        flush=True,
    )
