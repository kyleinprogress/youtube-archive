from __future__ import annotations

import json
import logging
import pathlib
import traceback
import urllib.parse
from typing import Any

from youtube_archive.errors import (
    ChannelLevelFailure,
    PlaylistFetchFailure,
    log_channel_level_failure,
    log_safe_detail,
)
from youtube_archive.logging_setup import get_creator_loggers
from youtube_archive.process import run_yt_dlp_capture
from youtube_archive.utils import (
    DATA_DIR,
    atomic_write_bytes,
    utc_date_string,
    utc_timestamp,
    write_json_atomic,
)


def run_pass_one(creator: dict[str, Any]) -> dict[str, Any]:
    # Return a well-formed candidate set even when unexpected Pass 1 code fails.
    try:
        return _run_pass_one(creator)
    except Exception as exc:
        slug = creator["slug"]
        _, manifests_log, errors_log, _, _ = get_creator_loggers(slug)
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
    _, manifests_log, errors_log, _, _ = get_creator_loggers(slug)
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
    _, manifests_log, _, _, _ = get_creator_loggers(slug)
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
