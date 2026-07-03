from __future__ import annotations

import datetime
import json
import os
import pathlib
import tempfile
from typing import Any


_DATA_DIR = pathlib.Path("data")
_STAGING_DIR = pathlib.Path(tempfile.gettempdir()) / "youtube-archive"


def data_dir() -> pathlib.Path:
    """The archive root. Defaults to ./data; overridden at startup from
    config.toml's `data_dir` key via set_data_dir()."""
    return _DATA_DIR


def set_data_dir(path: pathlib.Path) -> None:
    global _DATA_DIR
    _DATA_DIR = path


def staging_dir() -> pathlib.Path:
    """Local scratch root where yt-dlp downloads, merges, and embeds before the
    finished file is moved into data_dir. Keeping yt-dlp's long-held write handle
    off network mounts avoids close()-on-flush failures (EBADF/EINVAL) seen on
    SMB/NFS shares. Defaults to <system temp>/youtube-archive; overridden at
    startup from config.toml's `staging_dir` key via set_staging_dir()."""
    return _STAGING_DIR


def set_staging_dir(path: pathlib.Path) -> None:
    global _STAGING_DIR
    _STAGING_DIR = path


_COOKIES_FILE: pathlib.Path | None = None


def cookies_file() -> pathlib.Path | None:
    """Optional Netscape-format cookies.txt passed to every yt-dlp call, or
    None. Lets the archiver fetch age-restricted / members-only videos.
    Set at startup from config.toml's `cookies_file` key via set_cookies_file()."""
    return _COOKIES_FILE


def set_cookies_file(path: pathlib.Path | None) -> None:
    global _COOKIES_FILE
    _COOKIES_FILE = path


def cookies_args() -> list[str]:
    """yt-dlp args for the configured cookies file, or [] when unset. Splice
    into any yt-dlp command literal with `["yt-dlp", *cookies_args(), ...]`."""
    if _COOKIES_FILE is None:
        return []
    return ["--cookies", str(_COOKIES_FILE)]


def utc_timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_date_string() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def iso_timestamp(value: datetime.datetime) -> str:
    return value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json_atomic(
    path: pathlib.Path,
    obj: dict[str, Any],
    *,
    dry_run: bool = False,
) -> None:
    if dry_run:
        return
    data = json.dumps(
        obj,
        indent=2,
        ensure_ascii=False,
        sort_keys=False,
    ).encode("utf-8")
    atomic_write_bytes(path, data + b"\n", dry_run=dry_run)


def atomic_write_bytes(path: pathlib.Path, data: bytes, *, dry_run: bool = False) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with tmp_path.open("wb") as tmp_file:
        tmp_file.write(data)
    os.replace(tmp_path, path)


def string_or_empty(value: Any) -> str:
    return value if isinstance(value, str) else ""


def codec_or_none(value: Any) -> str:
    if isinstance(value, str) and value:
        return value
    return "none"


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
