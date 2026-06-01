from __future__ import annotations

import argparse
import http.server
import json
import pathlib
import shutil
import sys
from datetime import datetime, timezone
from functools import partial
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"


def run_serve_mode(
    *,
    repo_root: pathlib.Path,
    data_root: pathlib.Path,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    dry_run: bool = False,
) -> None:
    if not data_root.is_dir():
        print(f"error: data directory not found: {data_root}", file=sys.stderr)
        raise SystemExit(2)

    # The served site lives entirely under the data dir so it works whether
    # data is ./data or a mounted NAS path outside the repo: index.json + the
    # media files share one HTTP root, with media paths relative to it.
    output_path = data_root / "index.json"
    build_index(
        data_dir=data_root,
        output_path=output_path,
        serve_root=data_root,
        dry_run=dry_run,
    )

    if dry_run:
        print("[DRY RUN] server not started.", flush=True)
        return

    # index.html is repo source; copy it next to index.json so the data dir is
    # self-contained and the browser can load the page from the HTTP root.
    repo_index_html = repo_root / "index.html"
    if repo_index_html.exists():
        shutil.copy2(repo_index_html, data_root / "index.html")
    else:
        print(
            f"warn: {repo_index_html} not found; the UI page won't be served. "
            "Browse to /index.json directly to verify the build.",
            file=sys.stderr,
        )

    serve(data_root, host=host, port=port)


def build_index(
    *,
    data_dir: pathlib.Path,
    output_path: pathlib.Path,
    serve_root: pathlib.Path,
    dry_run: bool = False,
) -> tuple[int, int]:
    creator_entries: list[dict[str, Any]] = []
    for creator_dir in sorted(data_dir.iterdir()):
        if not creator_dir.is_dir():
            continue
        print(f"==> {creator_dir.name}", flush=True)
        entry = build_creator_entry(creator_dir, serve_root)
        if entry is not None:
            creator_entries.append(entry)
            print(
                f"    {entry['video_count']} videos, {entry['playlist_count']} playlists",
                flush=True,
            )

    creator_entries.sort(key=lambda c: c["name"].lower())

    output = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "creators": creator_entries,
    }

    total_videos = sum(c["video_count"] for c in creator_entries)
    if dry_run:
        print(
            f"\n[DRY RUN] would write {output_path} — "
            f"{len(creator_entries)} creators, {total_videos} videos",
            flush=True,
        )
        return len(creator_entries), total_videos

    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, ensure_ascii=False)
    tmp.replace(output_path)

    print(
        f"\nwrote {output_path} — "
        f"{len(creator_entries)} creators, {total_videos} videos",
        flush=True,
    )
    return len(creator_entries), total_videos


def serve(serve_root: pathlib.Path, *, host: str = DEFAULT_HOST, port: int) -> None:
    handler_class = partial(
        http.server.SimpleHTTPRequestHandler,
        directory=str(serve_root),
    )
    try:
        httpd = http.server.ThreadingHTTPServer((host, port), handler_class)
    except OSError as exc:
        print(
            f"error: could not bind {host}:{port} — {exc}. "
            f"Pass --host/--port to change.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    scope = "loopback only" if host in ("127.0.0.1", "localhost") else f"bound to {host}"
    url = f"http://{host}:{port}"
    print(
        f"Serving {serve_root} at {url} ({scope}; Ctrl-C to stop)",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        httpd.server_close()


def build_creator_entry(
    creator_dir: pathlib.Path,
    serve_root: pathlib.Path,
) -> dict[str, Any] | None:
    slug = creator_dir.name
    creator = read_json(creator_dir / "creator.json", serve_root) or {}

    videos_dir = creator_dir / "videos"
    video_entries: list[dict[str, Any]] = []
    if videos_dir.is_dir():
        for video_dir in sorted(videos_dir.iterdir()):
            if not video_dir.is_dir():
                continue
            entry = build_video_entry(slug, video_dir, serve_root)
            if entry is not None:
                video_entries.append(entry)

    def sort_key(v: dict[str, Any]) -> tuple[str, str, str]:
        return (
            v.get("upload_date") or "",
            v.get("archived_at") or "",
            v["id"],
        )

    video_entries.sort(key=sort_key, reverse=True)

    playlists_index = read_json(
        creator_dir / "manifests" / "channel" / "playlists_index_latest.json",
        serve_root,
    )
    playlists: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    if playlists_index and isinstance(playlists_index.get("entries"), list):
        for pl in playlists_index["entries"]:
            pid = pl.get("id")
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)
            playlists.append({"id": pid, "title": pl.get("title") or pid})

    for v in video_entries:
        for pl in v.get("playlists", []):
            pid = pl.get("id")
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)
            playlists.append({"id": pid, "title": pl.get("title") or pid})

    counts: dict[str, int] = {}
    for v in video_entries:
        for pl in v.get("playlists", []):
            pid = pl.get("id")
            if pid:
                counts[pid] = counts.get(pid, 0) + 1
    for pl in playlists:
        pl["count"] = counts.get(pl["id"], 0)

    display_name = creator.get("title") or creator.get("name") or slug
    channel_url = (
        creator.get("canonical_url")
        or creator.get("channel_url")
        or creator.get("config_channel_url")
    )
    return {
        "slug": slug,
        "name": display_name,
        "channel_url": channel_url,
        "avatar_url": creator.get("avatar_url"),
        "handle": creator.get("handle"),
        "description": creator.get("description"),
        "snapshot_taken_at": creator.get("snapshot_taken_at"),
        "video_count": len(video_entries),
        "playlist_count": len(playlists),
        "playlists": playlists,
        "videos": video_entries,
    }


def build_video_entry(
    creator_slug: str,
    video_dir: pathlib.Path,
    serve_root: pathlib.Path,
) -> dict[str, Any] | None:
    video_id = video_dir.name
    metadata = read_json(video_dir / "metadata.json", serve_root)
    if metadata is None:
        return None

    info = read_json(video_dir / f"{video_id}.info.json", serve_root) or {}

    media_path = find_media_file(video_dir, video_id)
    if media_path is None:
        # Archive directory exists but the media is missing — skip rather than
        # surface an unplayable entry. --audit is the right tool to flag this.
        print(f"  warn: no media file for {creator_slug}/{video_id}", file=sys.stderr)
        return None

    thumb_path = find_thumbnail(video_dir, video_id)
    media_rel = media_path.relative_to(serve_root).as_posix()
    thumb_rel = thumb_path.relative_to(serve_root).as_posix() if thumb_path else None

    downloaded = metadata.get("downloaded") or {}
    current = metadata.get("current") or {}

    return {
        "id": video_id,
        "title": current.get("title") or info.get("title") or video_id,
        "upload_date": parse_upload_date(info.get("upload_date")),
        "archived_at": metadata.get("archived_at"),
        "duration": format_duration(info.get("duration")),
        "duration_seconds": info.get("duration"),
        "availability": metadata.get("availability", "available"),
        "removed_detected_at": metadata.get("removed_detected_at"),
        "upgrade_available": metadata.get("upgrade_available") is not None,
        "format": {
            "height": downloaded.get("height"),
            "vcodec_short": (downloaded.get("vcodec") or "").split(".")[0] or None,
            "acodec": downloaded.get("acodec"),
            "filesize_human": format_filesize(downloaded.get("filesize")),
            "filesize_bytes": downloaded.get("filesize"),
        },
        "youtube_url": info.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}",
        "playlists": metadata.get("playlists") or [],
        "media_path": media_rel,
        "thumbnail_path": thumb_rel,
    }


def read_json(path: pathlib.Path, serve_root: pathlib.Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        try:
            display = path.relative_to(serve_root)
        except ValueError:
            display = path
        print(f"  warn: could not read {display}: {exc}", file=sys.stderr)
        return None


def find_media_file(video_dir: pathlib.Path, video_id: str) -> pathlib.Path | None:
    for ext in ("mkv", "mp4", "webm", "mov"):
        candidate = video_dir / f"{video_id}.{ext}"
        if candidate.exists():
            return candidate
    # Fallback: anything matching <id>.<ext> that isn't a sidecar.
    sidecar_exts = {".json", ".webp", ".jpg", ".png", ".vtt", ".srt", ".part"}
    for child in video_dir.iterdir():
        if child.stem == video_id and child.suffix.lower() not in sidecar_exts:
            return child
    return None


def find_thumbnail(video_dir: pathlib.Path, video_id: str) -> pathlib.Path | None:
    for ext in ("webp", "jpg", "png"):
        candidate = video_dir / f"{video_id}.{ext}"
        if candidate.exists():
            return candidate
    return None


def format_duration(seconds: float | int | None) -> str | None:
    if seconds is None:
        return None
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return None
    if total < 0:
        return None
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_filesize(size_bytes: int | float | None) -> str | None:
    if size_bytes is None:
        return None
    try:
        size = float(size_bytes)
    except (TypeError, ValueError):
        return None
    if size < 1024:
        return f"{int(size)} B"
    if size < 1024**2:
        return f"{size / 1024:.0f} KB"
    if size < 1024**3:
        return f"{size / 1024**2:.0f} MB"
    return f"{size / 1024**3:.2f} GB"


def parse_upload_date(yyyymmdd: str | None) -> str | None:
    if not yyyymmdd or len(yyyymmdd) != 8:
        return None
    try:
        return f"{yyyymmdd[0:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
    except Exception:
        return None


def cli_build_main() -> int:
    """Entry point for the build_index.py shim at the repo root.

    Standalone build with no server. Kept so `uv run build_index.py` (and
    anything that scripts it) keeps working after the move into the package.
    For a config-aware build that reads config.toml's data_dir, prefer
    `uv run archive.py --serve`. This shim scans whatever --data-dir points at
    (default ./data) and writes index.json into that directory.
    """
    parser = argparse.ArgumentParser(
        description="Build index.json describing every archived video."
    )
    parser.add_argument(
        "--data-dir",
        type=pathlib.Path,
        default=pathlib.Path("data"),
        help="Path to the archive data directory (default: ./data)",
    )
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        default=None,
        help="Where to write index.json (default: <data-dir>/index.json)",
    )
    args = parser.parse_args()

    if not args.data_dir.is_dir():
        print(f"error: data directory not found: {args.data_dir}", file=sys.stderr)
        return 2

    output_path = args.output or (args.data_dir / "index.json")
    build_index(
        data_dir=args.data_dir,
        output_path=output_path,
        serve_root=args.data_dir,
    )
    return 0
