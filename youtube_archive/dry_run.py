from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass
from typing import Any

from youtube_archive.config import setup_creator_environment
from youtube_archive.downloads import (
    GatedVideo,
    build_download_work_list,
)
from youtube_archive.errors import creator_scope, log_safe_detail
from youtube_archive.logging_setup import get_creator_loggers
from youtube_archive.manifests import run_pass_one
from youtube_archive.metadata import description_hash, read_archive_video_ids_for_metadata
from youtube_archive.refresh import (
    refresh_video_metadata,
    selected_format_descriptor,
)
from youtube_archive.upgrade import (
    UpgradeTarget,
    canonical_media_path,
    read_archive_ids,
)
from youtube_archive.utils import data_dir, string_or_empty, utc_timestamp


@dataclass
class DryRunTotals:
    would_download: int = 0
    gated: int = 0
    already_archived: int = 0
    skipped_unavailable: int = 0
    manifest_writes: int = 0
    metadata_touches: int = 0
    checked: int = 0
    metadata_changes: int = 0
    availability_changes: int = 0
    upgrades_new: int = 0
    upgrades_cleared: int = 0
    transient_errors: int = 0
    skipped: int = 0
    upgrade_targets: int = 0
    upgrade_attempts: int = 0


def run_dry_run_mode(creators: list[dict[str, Any]], args: Any) -> None:
    totals = DryRunTotals()
    for creator in creators:
        with creator_scope(creator["slug"], dry_run=True):
            setup_creator_environment(creator["slug"], dry_run=True)
            if args.refresh_metadata:
                print_creator_header(creator["slug"], args)
                refresh_counts = dry_run_refresh(creator)
                add_totals(totals, refresh_counts)
                if args.upgrade:
                    print_creator_header(creator["slug"], args, only_upgrade=True)
                    upgrade_counts = dry_run_upgrade(creator)
                    add_totals(totals, upgrade_counts)
            elif args.upgrade:
                print_creator_header(creator["slug"], args)
                counts = dry_run_upgrade(creator)
                add_totals(totals, counts)
            else:
                print_creator_header(creator["slug"], args)
                counts = dry_run_default_or_manifests(creator, manifests_only=args.manifests_only)
                add_totals(totals, counts)

    print_total(totals, args, len(creators))


def dry_run_default_or_manifests(
    creator: dict[str, Any],
    *,
    manifests_only: bool,
) -> DryRunTotals:
    counts = DryRunTotals()
    candidate_set = run_pass_one(creator, dry_run=True)
    summary = candidate_set.get("pass_one_summary", {})
    counts.manifest_writes = int(summary.get("manifest_writes", 0))

    if candidate_set.get("channel_level_failed"):
        print(
            "Pass 1: CHANNEL-LEVEL FAILURE — would skip Pass 2 and PRD 4 for this creator",
            flush=True,
        )
        return counts

    print_pass_one_summary(summary)

    if manifests_only:
        print(
            f"{creator['slug']}: --manifests-only — would write {counts.manifest_writes} manifest files",
            flush=True,
        )
        return counts

    null_log, _, _, _, _, _ = get_creator_loggers(creator["slug"], dry_run=True)
    buckets = build_download_work_list(creator, candidate_set, null_log)

    print("Pass 2 (eligibility):", flush=True)
    for video in buckets.eligible_sorted:
        counts.would_download += 1
        date_text = "unknown date"
        if video.timestamp is not None:
            uploaded = datetime.datetime.fromtimestamp(
                video.timestamp,
                datetime.timezone.utc,
            )
            date_text = uploaded.strftime("%Y-%m-%d")
        print(
            f"  {video.video_id} — would download (oldest first; uploaded {date_text})",
            flush=True,
        )
    for gated in buckets.gated_live_or_premiere + buckets.gated_too_recent:
        counts.gated += 1
        print(f"  {gated.video_id} — {gated_description(gated, creator)}", flush=True)
    counts.already_archived = len(buckets.already_archived)
    print(f"  {counts.already_archived} already archived (skip)", flush=True)
    counts.skipped_unavailable = len(buckets.skipped_unavailable)
    print(f"  {counts.skipped_unavailable} flagged unavailable (skip)", flush=True)

    metadata_touches = count_metadata_touches(creator)
    counts.metadata_touches = metadata_touches
    print("PRD 4 (metadata.json):", flush=True)
    print(
        f"  {metadata_touches} metadata.json files would be touched (playlists field refresh)",
        flush=True,
    )
    print(
        f"Summary: {counts.would_download} would download, {counts.gated} gated, "
        f"{counts.already_archived} already archived, "
        f"{counts.skipped_unavailable} flagged unavailable",
        flush=True,
    )
    return counts


def print_pass_one_summary(summary: dict[str, Any]) -> None:
    print("Pass 1 (manifest discovery):", flush=True)
    print(f"  canonical URL: {summary.get('canonical_url', '')}", flush=True)
    print(f"  playlists discovered: {summary.get('discovered_playlists', 0)}", flush=True)
    print(
        f"  final playlist set: {summary.get('final_playlists', 0)} "
        f"(discovered: {summary.get('discovered_playlists', 0)}, "
        f"extra: {summary.get('extra_playlists', 0)}, "
        f"excluded: {summary.get('excluded_playlists', 0)})",
        flush=True,
    )
    print(
        "  per-playlist manifests: "
        f"{summary.get('playlist_successes', 0)} would be written "
        f"({summary.get('playlist_successes', 0)} succeeded, "
        f"{summary.get('playlist_failures', 0)} failed)",
        flush=True,
    )
    print(f"  uploads tab: {summary.get('uploads_count', 0)} videos", flush=True)
    print(
        f"  candidates after union: {summary.get('candidate_count', 0)} unique video IDs",
        flush=True,
    )


def gated_description(gated: GatedVideo, creator: dict[str, Any]) -> str:
    if gated.reason == "is_live":
        return "gated (live stream in progress)"
    if gated.reason == "is_upcoming":
        return "gated (upcoming premiere)"
    if gated.reason == "future_release":
        return f"gated (future release: {gated.value})"
    if gated.reason == "too_recent":
        return (
            f"gated (upload age {gated.value}h < "
            f"min_upload_age_hours={creator['min_upload_age_hours']})"
        )
    return f"gated ({gated.reason})"


def count_metadata_touches(creator: dict[str, Any]) -> int:
    slug = creator["slug"]
    null_log, _, _, _, _, _ = get_creator_loggers(slug, dry_run=True)
    video_ids = read_archive_video_ids_for_metadata(slug, null_log)
    touches = 0
    for video_id in video_ids:
        video_dir = data_dir() / slug / "videos" / video_id
        if not video_dir.exists():
            continue
        media_path = video_dir / f"{video_id}.{creator['merge_output_format']}"
        metadata_path = video_dir / "metadata.json"
        info_path = video_dir / f"{video_id}.info.json"
        if not media_path.exists():
            continue
        if metadata_path.exists() or info_path.exists():
            touches += 1
    return touches


def dry_run_refresh(creator: dict[str, Any]) -> DryRunTotals:
    slug = creator["slug"]
    null_log, _, _, _, _, _ = get_creator_loggers(slug, dry_run=True)
    video_ids = read_archive_video_ids_for_metadata(slug, null_log)
    counts = DryRunTotals()
    no_changes = 0

    for video_id in video_ids:
        metadata_path = data_dir() / slug / "videos" / video_id / "metadata.json"
        if not metadata_path.exists():
            counts.skipped += 1
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            counts.skipped += 1
            continue

        invocation = refresh_video_metadata(creator, video_id)
        if invocation.state == "transient_error":
            counts.checked += 1
            counts.transient_errors += 1
            detail = log_safe_detail("\n".join(invocation.stderr_tail))
            print(f"  {video_id} — transient error: {detail}", flush=True)
            continue

        changes = describe_refresh_changes(metadata, invocation.state, invocation.payload)
        counts.checked += 1
        if changes:
            if any(item.startswith(("title ", "thumbnail_url ", "description_hash ")) for item in changes):
                counts.metadata_changes += 1
            if any(item.startswith("availability changed") for item in changes):
                counts.availability_changes += 1
            if any(item.startswith("upgrade_available newly populated") for item in changes):
                counts.upgrades_new += 1
            if any(item.startswith("upgrade_available cleared") for item in changes):
                counts.upgrades_cleared += 1
            print(f"  {video_id} — would update: {'; '.join(changes)}", flush=True)
        else:
            no_changes += 1

    print(f"  {no_changes} no changes (skip)", flush=True)
    print(
        f"Summary: {counts.checked} checked, {counts.metadata_changes} metadata changes, "
        f"{counts.availability_changes} availability changes, "
        f"{counts.upgrades_new} upgrade newly available, "
        f"{counts.upgrades_cleared} upgrade cleared, "
        f"{counts.transient_errors} transient errors, {counts.skipped} skipped",
        flush=True,
    )
    return counts


def describe_refresh_changes(
    metadata: dict[str, Any],
    availability: str,
    payload: dict[str, Any] | None,
) -> list[str]:
    changes: list[str] = []
    prior_availability = metadata.get("availability")
    if prior_availability != availability:
        changes.append(f"availability changed ({prior_availability} -> {availability})")

    if availability == "available" and payload is not None:
        current = metadata.get("current", {})
        if not isinstance(current, dict):
            current = {}
        new_title = string_or_empty(payload.get("title"))
        if current.get("title") != new_title:
            changes.append(f'title changed ("{string_or_empty(current.get("title"))}" -> "{new_title}")')
        new_thumbnail = string_or_empty(payload.get("thumbnail"))
        if current.get("thumbnail_url") != new_thumbnail:
            changes.append("thumbnail_url changed")
        new_description_hash = description_hash(payload.get("description"))
        if current.get("description_hash") != new_description_hash:
            changes.append("description_hash changed")

        now = utc_timestamp()
        selected = selected_format_descriptor(payload, now)
        downloaded = metadata.get("downloaded", {})
        if not isinstance(downloaded, dict):
            downloaded = {}
        prior_upgrade = metadata.get("upgrade_available")
        if selected["format_id"] != downloaded.get("format_id"):
            if prior_upgrade is None:
                changes.append(
                    "upgrade_available newly populated "
                    f"(height {downloaded.get('height')} -> {selected.get('height')})"
                )
        elif prior_upgrade is not None:
            if isinstance(prior_upgrade, dict):
                changes.append(
                    "upgrade_available cleared "
                    f"(was: height {prior_upgrade.get('height')}, "
                    f"format {prior_upgrade.get('format_id')})"
                )
            else:
                changes.append("upgrade_available cleared")
    return changes


def dry_run_upgrade(creator: dict[str, Any]) -> DryRunTotals:
    slug = creator["slug"]
    _, _, _, _, upgrade_log, _ = get_creator_loggers(slug, dry_run=True)
    recovery_skips = dry_run_recovery(creator, upgrade_log)
    targets, skipped, unavailable_lines = collect_dry_upgrade_targets(
        creator,
        recovery_skips,
    )
    counts = DryRunTotals(
        upgrade_targets=len(targets) + skipped,
        upgrade_attempts=len(targets),
        skipped=skipped,
    )
    for line in unavailable_lines:
        print(line, flush=True)
    for target in targets:
        from_downloaded = target.metadata.get("downloaded", {})
        if not isinstance(from_downloaded, dict):
            from_downloaded = {}
        to_downloaded = target.metadata.get("upgrade_available", {})
        if not isinstance(to_downloaded, dict):
            to_downloaded = {}
        detected = string_or_empty(to_downloaded.get("detected_at"))
        if len(detected) >= 10:
            detected = detected[:10]
        print(
            f"  {target.video_id} — would upgrade "
            f"{from_downloaded.get('format_id')}/{from_downloaded.get('height')}p "
            f"({from_downloaded.get('vcodec')}) -> "
            f"{to_downloaded.get('format_id')}/{to_downloaded.get('height')}p "
            f"({to_downloaded.get('vcodec')}); detected {detected}",
            flush=True,
        )
    print(
        f"Summary: {len(targets) + skipped} targets, "
        f"would attempt upgrade on {len(targets)}, {skipped} skipped",
        flush=True,
    )
    return counts


def collect_dry_upgrade_targets(
    creator: dict[str, Any],
    recovery_skips: set[str],
) -> tuple[list[Any], int, list[str]]:
    slug = creator["slug"]
    videos_dir = data_dir() / slug / "videos"
    if not videos_dir.exists():
        return [], len(recovery_skips), []

    skipped = len(recovery_skips)
    unavailable_lines: list[str] = []
    targets: list[Any] = []
    for metadata_path in sorted(videos_dir.glob("*/metadata.json")):
        video_id = metadata_path.parent.name
        if video_id in recovery_skips:
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if metadata.get("upgrade_available") is None:
            continue
        availability = metadata.get("availability")
        if availability != "available":
            unavailable_lines.append(
                f"  {video_id} — skipped (availability: {availability})"
            )
            skipped += 1
            continue
        media_path = canonical_media_path(creator, video_id)
        if not media_path.exists():
            unavailable_lines.append(
                f"  {video_id} — skipped (canonical media missing)"
            )
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
    return targets, skipped, unavailable_lines


def dry_run_recovery(
    creator: dict[str, Any],
    upgrade_log: logging.Logger,
) -> set[str]:
    slug = creator["slug"]
    videos_dir = data_dir() / slug / "videos"
    archive_ids = read_archive_ids(slug, upgrade_log)
    recovery_skips: set[str] = set()
    found = 0
    if not videos_dir.exists():
        print("  startup recovery: 0 .pre-upgrade leftovers found", flush=True)
        return recovery_skips

    for video_dir in sorted(path for path in videos_dir.iterdir() if path.is_dir()):
        pre_upgrade_files = sorted(video_dir.glob("*.pre-upgrade"))
        if not pre_upgrade_files:
            continue
        found += 1
        video_id = video_dir.name
        media_path = canonical_media_path(creator, video_id)
        info_path = video_dir / f"{video_id}.info.json"
        if video_id not in archive_ids:
            print(
                f"  startup recovery: would leave {video_id} untouched (Case C orphan)",
                flush=True,
            )
        elif media_path.exists() and media_path.stat().st_size > 0 and info_path.exists():
            print(
                f"  startup recovery: would skip {video_id} (Case B ambiguous)",
                flush=True,
            )
            recovery_skips.add(video_id)
        else:
            print(
                f"  startup recovery: would restore {video_id} from .pre-upgrade (Case A)",
                flush=True,
            )
    if found == 0:
        print("  startup recovery: 0 .pre-upgrade leftovers found", flush=True)
    return recovery_skips


def print_creator_header(slug: str, args: Any, *, only_upgrade: bool = False) -> None:
    suffixes: list[str] = []
    if args.refresh_metadata and not only_upgrade:
        suffixes.append("--refresh-metadata")
    if args.upgrade:
        suffixes.append("--upgrade")
    if args.manifests_only:
        suffixes.append("--manifests-only")
    suffix = f" {' '.join(suffixes)}" if suffixes else ""
    print(f"==> {slug} (DRY RUN{suffix})", flush=True)


def add_totals(total: DryRunTotals, counts: DryRunTotals) -> None:
    for field in total.__dataclass_fields__:
        setattr(total, field, getattr(total, field) + getattr(counts, field))


def print_total(total: DryRunTotals, args: Any, creator_count: int) -> None:
    if args.refresh_metadata:
        print(
            f"Total: {total.checked} checked, {total.metadata_changes} metadata changes, "
            f"{total.availability_changes} availability changes, "
            f"{total.upgrades_new} upgrade newly available, "
            f"{total.upgrades_cleared} upgrade cleared, "
            f"{total.transient_errors} transient errors, {total.skipped} skipped "
            f"across {creator_count} creators",
            flush=True,
        )
    elif args.upgrade:
        print(
            f"Total: {total.upgrade_targets} targets, would attempt upgrade on "
            f"{total.upgrade_attempts}, {total.skipped} skipped across {creator_count} creators",
            flush=True,
        )
    elif args.manifests_only:
        print(
            f"Total: {total.manifest_writes} manifest files would be written across "
            f"{creator_count} creators",
            flush=True,
        )
    else:
        print(
            f"Total: {total.would_download} would download, {total.gated} gated, "
            f"{total.already_archived} already archived, "
            f"{total.skipped_unavailable} flagged unavailable across {creator_count} creators",
            flush=True,
        )
