from __future__ import annotations

import datetime
import json
import os
import pathlib
from typing import Any


DATA_DIR = pathlib.Path("data")


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
