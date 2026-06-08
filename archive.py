from __future__ import annotations

import argparse
import pathlib
from dataclasses import dataclass
from typing import Any

from youtube_archive.audit import run_audit_mode, validate_audit_args
from youtube_archive.config import (
    check_ffmpeg,
    load_config,
    resolve_data_dir,
    resolve_staging_dir,
    select_creators,
    setup_creator_environment,
    validate_config,
)
from youtube_archive.dry_run import run_dry_run_mode
from youtube_archive.downloads import run_pass_two
from youtube_archive.errors import creator_scope
from youtube_archive.manifests import run_pass_one
from youtube_archive.metadata import run_pass_four, should_run_metadata_pass
from youtube_archive.refresh import run_refresh_mode
from youtube_archive.upgrade import run_upgrade_mode
from youtube_archive.utils import data_dir, set_data_dir, set_staging_dir
from youtube_archive.web_ui import DEFAULT_HOST, DEFAULT_PORT, build_index, run_serve_mode


@dataclass(frozen=True)
class RunContext:
    args: argparse.Namespace
    creators: list[dict[str, Any]]


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    raw_config = load_config()
    creators = validate_config(raw_config)
    set_data_dir(resolve_data_dir(raw_config))
    set_staging_dir(resolve_staging_dir(raw_config))

    if args.serve:
        # --serve reads config only to learn where data lives, then builds the
        # index and serves the data dir (no ffmpeg or creator selection needed).
        run_serve_mode(
            repo_root=pathlib.Path(__file__).resolve().parent,
            data_root=data_dir(),
            host=args.host,
            port=args.port,
            dry_run=args.dry_run,
        )
        return

    selected_creators = select_creators(creators, args.creator)
    context = RunContext(args=args, creators=selected_creators)

    validate_audit_args(context.args)
    check_ffmpeg()

    if context.args.audit:
        run_audit_mode(
            context.creators,
            repair=context.args.repair,
            dry_run=context.args.dry_run,
        )
        return

    if context.args.dry_run:
        run_dry_run_mode(context.creators, context.args)
        return

    if context.args.refresh_metadata:
        run_refresh_mode(context.creators)
        rebuild_index()
        return

    if context.args.upgrade:
        run_upgrade_mode(context.creators)
        rebuild_index()
        return

    for creator in context.creators:
        slug = creator["slug"]
        print(f"==> {slug}", flush=True)
        with creator_scope(slug):
            setup_creator_environment(slug)
            candidate_set = run_pass_one(creator)
            if not context.args.manifests_only:
                run_pass_two(creator, candidate_set)
            if should_run_metadata_pass(context.args, candidate_set):
                run_pass_four(creator, candidate_set)

    rebuild_index()


def rebuild_index() -> None:
    # Keep index.json fresh for the long-running --serve process, which only
    # rebuilds at startup. Called after any run that may have changed the
    # archive contents (sync, refresh, upgrade).
    root = data_dir()
    build_index(data_dir=root, output_path=root / "index.json", serve_root=root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Archive configured YouTube creators with yt-dlp."
    )
    parser.add_argument(
        "--creator",
        metavar="slug",
        help="Run only the creator with this configured slug.",
    )
    parser.add_argument(
        "--manifests-only",
        action="store_true",
        help="Only refresh manifest data; media download behavior is added later.",
    )
    parser.add_argument(
        "--refresh-metadata",
        action="store_true",
        help="Refresh normalized metadata for already archived videos.",
    )
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help="Check for and download improved formats for archived videos.",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Audit archive consistency without changing media files.",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Repair audit findings when supported by later PRDs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan work without applying download or repair changes.",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Build index.json from data/ and serve the browse UI on 127.0.0.1.",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=DEFAULT_HOST,
        help=(
            f"Bind address for --serve (default: {DEFAULT_HOST}, loopback-only). "
            "Set to 0.0.0.0 to expose on the LAN (e.g. inside a container)."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port for --serve (default: {DEFAULT_PORT}).",
    )
    return parser


if __name__ == "__main__":
    main()
