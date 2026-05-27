from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import pathlib
from typing import Any

from youtube_archive.logging_setup import get_creator_loggers
from youtube_archive.utils import (
    DATA_DIR,
    codec_or_none,
    optional_int_value,
    parse_archive_video_ids,
    string_or_empty,
    utc_timestamp,
    write_json_atomic,
)


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
    download_log, _, _, _, _, _ = get_creator_loggers(slug)
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
