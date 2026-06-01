from __future__ import annotations

import datetime
import json
import pathlib
import re
import shutil
import sys
from dataclasses import dataclass, field
from typing import Any

from youtube_archive.config import setup_creator_environment
from youtube_archive.errors import creator_scope
from youtube_archive.logging_setup import get_creator_loggers
from youtube_archive.utils import atomic_write_bytes, data_dir


ARCHIVE_LINE_RE = re.compile(r"^youtube ([a-zA-Z0-9_-]{11})$")
VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")


@dataclass(frozen=True)
class ArchiveLine:
    line_number: int
    content: str
    video_id: str | None


@dataclass(frozen=True)
class AuditEntry:
    video_id: str | None
    reason: str
    line_number: int | None = None
    content: str | None = None


@dataclass
class AuditResult:
    slug: str
    archive_lines: list[ArchiveLine] = field(default_factory=list)
    expected_ids: list[str] = field(default_factory=list)
    class_a: list[AuditEntry] = field(default_factory=list)
    class_b: list[AuditEntry] = field(default_factory=list)
    class_c: list[AuditEntry] = field(default_factory=list)
    class_d: list[AuditEntry] = field(default_factory=list)
    class_e: list[AuditEntry] = field(default_factory=list)
    failed: str | None = None

    @property
    def counts(self) -> tuple[int, int, int, int, int]:
        return (
            len(self.class_a),
            len(self.class_b),
            len(self.class_c),
            len(self.class_d),
            len(self.class_e),
        )


@dataclass
class AuditTotals:
    class_a: int = 0
    class_b: int = 0
    class_c: int = 0
    class_d: int = 0
    class_e: int = 0

    def add(self, result: AuditResult) -> None:
        a_count, b_count, c_count, d_count, e_count = result.counts
        self.class_a += a_count
        self.class_b += b_count
        self.class_c += c_count
        self.class_d += d_count
        self.class_e += e_count


def run_audit_mode(
    creators: list[dict[str, Any]],
    *,
    repair: bool = False,
    dry_run: bool = False,
) -> None:
    totals = AuditTotals()
    for creator in creators:
        slug = creator["slug"]
        with creator_scope(slug, dry_run=dry_run):
            setup_creator_environment(slug, dry_run=dry_run)
            result = run_audit(creator, dry_run=dry_run)
            if result.failed is not None:
                print(
                    f"{slug}: AUDIT FAILED — {result.failed}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            print_audit_result(result, repair=repair)
            if repair:
                run_repair(creator, result, dry_run=dry_run)
            totals.add(result)

    print(
        f"Total: {totals.class_a} Class A, {totals.class_b} Class B, "
        f"{totals.class_c} Class C, {totals.class_d} Class D, "
        f"{totals.class_e} Class E across {len(creators)} creators",
        flush=True,
    )


def run_audit(creator: dict[str, Any], *, dry_run: bool = False) -> AuditResult:
    slug = creator["slug"]
    _, _, errors_log, _, _, audit_log = get_creator_loggers(slug, dry_run=dry_run)
    audit_log.info("audit starting")
    result = AuditResult(slug=slug)

    try:
        archive_lines = parse_archive(slug)
    except OSError as exc:
        message = f"archive.txt unreadable — {exc}"
        audit_log.error("audit failed: %s", message)
        errors_log.error("audit failed: %s", message)
        result.failed = "archive.txt unreadable"
        return result

    result.archive_lines = archive_lines
    result.class_e = [
        AuditEntry(
            video_id=None,
            reason="malformed archive.txt line",
            line_number=line.line_number,
            content=line.content,
        )
        for line in archive_lines
        if line.video_id is None and line.content.strip() and not line.content.strip().startswith("#")
    ]
    for entry in result.class_e:
        audit_log.warning(
            'class_e malformed_archive_line: line %s ("%s")',
            entry.line_number,
            entry.content,
        )

    expected_ids = [line.video_id for line in archive_lines if line.video_id is not None]
    result.expected_ids = expected_ids
    expected_set = set(expected_ids)
    videos_dir = data_dir() / slug / "videos"
    try:
        disk_dirs = scan_video_dirs(videos_dir, audit_log)
    except OSError as exc:
        message = f"videos/ directory unreadable — {exc}"
        audit_log.error("audit failed: %s", message)
        errors_log.error("audit failed: %s", message)
        result.failed = "videos/ directory unreadable"
        return result

    ext = creator["merge_output_format"]
    for video_id in expected_ids:
        video_dir = videos_dir / video_id
        classification = classify_expected_video(video_id, video_dir, ext)
        if classification is None:
            continue
        class_name, reason = classification
        entry = AuditEntry(video_id=video_id, reason=reason)
        if class_name == "A":
            result.class_a.append(entry)
            audit_log.warning("class_a orphan_in_archive: %s (%s)", video_id, reason)
        elif class_name == "C":
            result.class_c.append(entry)
            audit_log.warning("class_c incomplete_folder: %s (%s)", video_id, reason)
        elif class_name == "D":
            result.class_d.append(entry)
            audit_log.warning("class_d invalid_metadata: %s (%s)", video_id, reason)

    for video_id, video_dir in disk_dirs:
        if video_id in expected_set:
            continue
        media_path = video_dir / f"{video_id}.{ext}"
        if media_path.exists() and media_path.is_file() and media_path.stat().st_size > 0:
            entry = AuditEntry(video_id=video_id, reason="not in archive.txt")
            result.class_b.append(entry)
            audit_log.info("class_b orphan_on_disk: %s (not in archive.txt)", video_id)

    audit_log.info(
        "audit complete: A=%s B=%s C=%s D=%s E=%s",
        len(result.class_a),
        len(result.class_b),
        len(result.class_c),
        len(result.class_d),
        len(result.class_e),
    )
    return result


def parse_archive(slug: str) -> list[ArchiveLine]:
    archive_path = data_dir() / slug / "archive.txt"
    if not archive_path.exists():
        return []

    lines: list[ArchiveLine] = []
    for line_number, line in enumerate(
        archive_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(ArchiveLine(line_number, line, None))
            continue
        match = ARCHIVE_LINE_RE.fullmatch(stripped)
        if match is None:
            lines.append(ArchiveLine(line_number, line, None))
        else:
            lines.append(ArchiveLine(line_number, line, match.group(1)))
    return lines


def scan_video_dirs(
    videos_dir: pathlib.Path,
    audit_log: Any,
) -> list[tuple[str, pathlib.Path]]:
    if not videos_dir.exists():
        return []

    disk_dirs: list[tuple[str, pathlib.Path]] = []
    for path in sorted(videos_dir.iterdir(), key=lambda item: item.name):
        if path.is_symlink():
            audit_log.info("symlink skipped: %s", path)
            continue
        if not path.is_dir():
            continue
        if VIDEO_ID_RE.fullmatch(path.name):
            disk_dirs.append((path.name, path))
    return disk_dirs


def classify_expected_video(
    video_id: str,
    video_dir: pathlib.Path,
    ext: str,
) -> tuple[str, str] | None:
    media_path = video_dir / f"{video_id}.{ext}"
    info_path = video_dir / f"{video_id}.info.json"

    if not video_dir.exists():
        return "A", "folder missing"
    if not media_path.exists():
        if info_path.exists():
            return "A", "media file missing (folder + info.json present)"
        return "A", "media file missing"
    if media_path.stat().st_size == 0:
        return "A", "media file zero-size"

    part_path = video_dir / f"{video_id}.{ext}.part"
    if not info_path.exists():
        return "C", "info.json missing"
    if part_path.exists():
        return "C", "resume marker present alongside complete media"

    metadata_reason = validate_metadata(video_id, video_dir / "metadata.json")
    if metadata_reason is not None:
        return "D", metadata_reason
    return None


def validate_metadata(video_id: str, metadata_path: pathlib.Path) -> str | None:
    if not metadata_path.exists():
        return "missing"
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "unparseable"
    except OSError:
        return "unreadable"
    if not isinstance(payload, dict):
        return "unparseable"

    schema_version = payload.get("schema_version")
    if schema_version != 1:
        actual = "missing" if "schema_version" not in payload else schema_version
        return f"schema_version={actual}"

    actual_video_id = payload.get("video_id")
    if actual_video_id != video_id:
        return f"video_id_mismatch (got {actual_video_id}, folder is {video_id})"
    return None


def print_audit_result(result: AuditResult, *, repair: bool) -> None:
    suffix = " --repair" if repair else ""
    print(f"==> {result.slug} (AUDIT{suffix})", flush=True)
    if not any(result.counts):
        print("  OK — no discrepancies detected", flush=True)
        return

    print_class("Orphan in archive.txt", "A", result.class_a)
    print_class("Orphan on disk", "B", result.class_b)
    print_class("Incomplete folder", "C", result.class_c)
    print_class("Invalid metadata.json", "D", result.class_d)
    print_class("Malformed archive.txt lines", "E", result.class_e)
    a_count, b_count, c_count, d_count, e_count = result.counts
    print(
        f"Summary: {a_count} Class A, {b_count} Class B, {c_count} Class C, "
        f"{d_count} Class D, {e_count} Class E",
        flush=True,
    )


def print_class(name: str, letter: str, entries: list[AuditEntry]) -> None:
    if not entries:
        return
    print(f"{name} (Class {letter}) — {len(entries)}:", flush=True)
    for entry in entries:
        if letter == "E":
            print(
                f'  Line {entry.line_number}: "{entry.content}" (invalid video ID format)',
                flush=True,
            )
        else:
            print(f"  {entry.video_id} — {entry.reason}", flush=True)


def run_repair(
    creator: dict[str, Any],
    result: AuditResult,
    *,
    dry_run: bool = False,
) -> None:
    slug = creator["slug"]
    _, _, _, _, _, audit_log = get_creator_loggers(slug, dry_run=dry_run)
    prune_ids = [entry.video_id for entry in result.class_a]
    if not prune_ids:
        audit_log.info("repair: 0 orphans to prune")
        return

    archive_path = data_dir() / slug / "archive.txt"
    backup_path = next_backup_path(archive_path)
    print("--repair:", flush=True)
    if dry_run:
        print(f"  [DRY RUN] would back up archive.txt to {backup_path}", flush=True)
        print(
            f"  [DRY RUN] would prune from archive.txt: {', '.join(prune_ids)}",
            flush=True,
        )
        print(f"  [DRY RUN] {len(prune_ids)} entries would be removed", flush=True)
        return

    shutil.copy2(archive_path, backup_path)
    audit_log.info("repair backup: %s", backup_path)
    pruned = rewrite_archive_without_ids(archive_path, set(prune_ids))
    for video_id in prune_ids:
        audit_log.info("repair pruned: %s (orphan in archive.txt)", video_id)
    audit_log.info("repair complete: %s entries pruned", pruned)
    print(f"  backup written: {backup_path}", flush=True)
    print(f"  pruned: {', '.join(prune_ids)}", flush=True)
    print(f"  {pruned} entries removed from archive.txt", flush=True)


def next_backup_path(archive_path: pathlib.Path) -> pathlib.Path:
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base_path = archive_path.with_name(f"{archive_path.name}.pre-repair-{timestamp}")
    if not base_path.exists():
        return base_path
    counter = 2
    while True:
        candidate = archive_path.with_name(
            f"{archive_path.name}.pre-repair-{timestamp}-{counter}"
        )
        if not candidate.exists():
            return candidate
        counter += 1


def rewrite_archive_without_ids(archive_path: pathlib.Path, prune_ids: set[str]) -> int:
    kept_lines: list[str] = []
    pruned = 0
    for line in archive_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        match = ARCHIVE_LINE_RE.fullmatch(stripped)
        if match is not None and match.group(1) in prune_ids:
            pruned += 1
            continue
        kept_lines.append(line)

    data = "\n".join(kept_lines)
    if data:
        data += "\n"
    atomic_write_bytes(archive_path, data.encode("utf-8"))
    return pruned


def validate_audit_args(args: Any) -> None:
    if args.repair and not args.audit:
        print("error: --repair requires --audit", file=sys.stderr)
        raise SystemExit(2)
