# YouTube Archive

A single-file Python orchestrator that archives YouTube creators using `yt-dlp`. Each creator gets a self-contained directory tree containing the media files, raw `info.json` from yt-dlp, normalized `metadata.json` files, and timestamped manifest snapshots that preserve the channel's playlist structure over time.

The central design point: **playlist membership is derived from per-playlist manifests, not from per-video `info.json`**. A video that appears in three of a creator's playlists exists as one media file with one `metadata.json` whose `playlists` array has three entries — never as duplicated downloads.

## Requirements

- Python 3.14
- [`uv`](https://docs.astral.sh/uv/) (used for running the script)
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) on `PATH` (tested against `2026.03.17`)
- [`ffmpeg`](https://ffmpeg.org/) on `PATH` (for `mkv` merge + metadata embedding)

On macOS:

```sh
brew install yt-dlp ffmpeg
```

The script itself uses only the Python standard library — no `pip install` required.

## Setup

1. Clone the repo.
2. Copy the example config and edit it for your creators:

   ```sh
   cp config.example.toml config.toml
   ```

3. Edit `config.toml`. The minimal shape is:

   ```toml
   [defaults]
   format = "bv*+ba/b"
   format_sort = ["res:1080", "codec:av01", "acodec:opus"]
   merge_output_format = "mkv"
   min_upload_age_hours = 48

   [[creator]]
   slug = "creator-slug"            # filesystem-safe directory name under data/
   name = "Creator Display Name"
   channel_url = "https://www.youtube.com/@theirhandle"
   ```

   See `config.example.toml` for the full schema, including per-creator overrides, `extra_playlists`, and `exclude_playlists`.

`config.toml` is gitignored. Only `config.example.toml` is checked in.

## Usage

All invocations run from the repo root:

```sh
uv run archive.py [flags]
```

### Default — discover and download

```sh
uv run archive.py
uv run archive.py --creator creator-slug   # scope to one creator
```

Runs Pass 1 (manifest discovery) + Pass 2 (media download) + the metadata pass, sequentially, for each configured creator. Already-archived videos are skipped via `archive.txt`. New uploads younger than `min_upload_age_hours` are gated and naturally retried on the next run.

### `--manifests-only` — refresh manifests without downloading

```sh
uv run archive.py --manifests-only
```

Runs Pass 1 only — produces `creator.json`, `manifests/channel/playlists_index_*.json`, `manifests/channel/uploads_*.json`, and per-playlist manifests under `manifests/playlists/`. No media is touched. Useful for a quick "what changed on this channel?" weekly cron.

### `--refresh-metadata` — refresh metadata for archived videos

```sh
uv run archive.py --refresh-metadata
uv run archive.py --refresh-metadata --creator creator-slug
```

For every video in `archive.txt`, calls `yt-dlp --skip-download --dump-json` once and updates `metadata.json`:
- **Title / thumbnail / description changes** are recorded in `history` with the prior values preserved.
- **Availability changes** (deleted, private, members-only, age-restricted) flip `availability` and stamp `removed_detected_at`. Media files on disk are never touched.
- **Resurrections** (a previously-removed video that's available again) are detected automatically.
- **Upgrade availability** is computed from the current `format` + `format_sort` config. If yt-dlp would now select a format different from what's on disk, `upgrade_available` is populated with a descriptor of the better format.

No media is downloaded in this mode. Cheap enough to run weekly.

### `--upgrade` — re-download videos with a better format available

```sh
uv run archive.py --upgrade
uv run archive.py --upgrade --creator creator-slug
```

For every video with a non-null `upgrade_available` (populated by a prior `--refresh-metadata` run), re-downloads the video at the current format settings and atomically replaces the on-disk media. Skips Pass 1, Pass 2, the metadata pass, and the refresh pass — `--upgrade` trusts the recorded `upgrade_available` state.

- **`.pre-upgrade` rollback safety net.** Before invoking yt-dlp, every sidecar (media, `info.json`, thumbnail, subtitles) is renamed to `<filename>.pre-upgrade`. On success the `.pre-upgrade` files are deleted; on failure they're restored. The original media file is never lost.
- **Startup recovery.** Before processing upgrade targets, the script sweeps each creator's `videos/` tree for `.pre-upgrade` leftovers from a prior interrupted run and restores them automatically.
- **Identical-format no-op.** If yt-dlp picks the same `format_id` as what's already on disk (stale `upgrade_available` flag), the upgrade is recorded as a no-op: `upgrade_available` is cleared, `downloaded` stays untouched, no `history` entry is appended.
- **Serial.** One yt-dlp invocation completes before the next starts.

A successful upgrade replaces `metadata.json.downloaded` with the new format block, clears `upgrade_available`, and appends one `{type: "upgrade", observed_at, from, to}` entry to `history`. All other fields (`current.*`, `playlists`, `archived_at`, etc.) stay byte-identical.

### `--dry-run` — preview without side effects

```sh
uv run archive.py --dry-run
uv run archive.py --dry-run --manifests-only --creator creator-slug
uv run archive.py --dry-run --refresh-metadata
uv run archive.py --dry-run --upgrade
```

Previews what each mode *would* do without writing any files. Composes with every other mode flag. The only side effect is human-readable output to stdout:

- **No writes** to `data/` — no `creator.json`, manifests, `metadata.json`, log files, `.pre-upgrade` renames, or media downloads.
- **Directory creation is allowed** (empty directories are harmless) so missed write sites fail loudly with `FileNotFoundError` rather than silently no-op.
- **Read-only network calls deliberately happen** for accurate previews — Pass 1 manifest fetches and `--refresh-metadata` per-video `yt-dlp --dump-json` calls still run. Downloads do not. `--dry-run --upgrade` makes no network calls and trusts the recorded `upgrade_available`.
- **Per-creator and final summary** lines printed to stdout. Exit code is always `0`.

### `--help`

```sh
uv run archive.py --help
```

Lists all supported flags. The full set is `--creator`, `--manifests-only`, `--refresh-metadata`, `--upgrade`, `--audit`, `--repair`, `--dry-run`. Of those, only `--audit` and `--repair` are not yet wired up — they're accepted so command lines stay stable, but their behavior arrives in PRD 8.

## Output layout

For each creator with slug `<slug>`:

```
data/<slug>/
├── creator.json                            # curated channel-level snapshot
├── archive.txt                             # yt-dlp's "already downloaded" log (one line per video)
├── manifests/
│   ├── channel/
│   │   ├── playlists_index_latest.json     # raw yt-dlp output: channel /playlists
│   │   ├── playlists_index_YYYY-MM-DD.json # same-day timestamped copy
│   │   ├── uploads_latest.json             # raw yt-dlp output: channel /videos
│   │   └── uploads_YYYY-MM-DD.json
│   └── playlists/
│       ├── <PLAYLIST_ID>_latest.json       # raw yt-dlp output: one per discovered/extra playlist
│       └── <PLAYLIST_ID>_YYYY-MM-DD.json
├── videos/
│   └── <VIDEO_ID>/
│       ├── <VIDEO_ID>.mkv                  # merged media (extension = merge_output_format)
│       ├── <VIDEO_ID>.info.json            # raw yt-dlp metadata
│       ├── <VIDEO_ID>.webp                 # thumbnail
│       ├── <VIDEO_ID>.en.vtt               # English subtitles (when available)
│       └── metadata.json                   # normalized schema (see below)
└── logs/
    ├── manifests.log                       # Pass 1 activity
    ├── download.log                        # Pass 2 + metadata pass activity (yt-dlp output during --upgrade also streams here)
    ├── refresh.log                         # --refresh-metadata activity
    ├── upgrade.log                         # --upgrade activity (startup recovery, per-video outcomes)
    └── errors.log                          # any error, scoped to this creator
```

The whole `data/` directory is gitignored. Manifest snapshots accumulate per UTC date so you can answer "when did this playlist disappear?" by reading the historical files.

### `metadata.json` schema

Every archived video has a `metadata.json` with this shape:

```json
{
  "schema_version": 1,
  "video_id": "abc123",
  "source_creator": "creator-slug",
  "archived_at": "2026-05-26T10:30:42Z",
  "last_metadata_check": "2026-05-26T10:30:42Z",
  "availability": "available",
  "removed_detected_at": null,
  "current": {
    "title": "Current Title",
    "thumbnail_url": "https://i.ytimg.com/...",
    "description_hash": "sha256:..."
  },
  "downloaded": {
    "format_id": "401+251",
    "height": 1080,
    "vcodec": "av01.0.08M.08",
    "acodec": "opus",
    "filesize": 412345678
  },
  "upgrade_available": null,
  "history": [],
  "playlists": [
    {"id": "PLxyz", "title": "Best Videos", "index": 7}
  ]
}
```

- `current.*` and `availability` are mutated by `--refresh-metadata`. Prior values land in `history`.
- `playlists` is rewritten from Pass 1 manifests on every default run — historical playlist state lives in the timestamped manifest snapshots, not in `metadata.json`.
- `upgrade_available` is populated by `--refresh-metadata` when a better format is now offered. `--upgrade` consumes it: re-downloads the video, replaces `downloaded` with the new format block, clears `upgrade_available`, and appends a `{type: "upgrade", observed_at, from, to}` entry to `history`.

## Logging conventions

- All log lines: `<ISO 8601 UTC timestamp> <LEVEL> <message>` — e.g. `2026-05-26T15:30:42Z INFO Pass 1 starting`.
- Levels in use: `INFO`, `WARN`, `ERROR`. (`WARN` rather than `WARNING` for grep ergonomics.)
- Console output is minimal: a `==> <slug>` header per creator and one summary line per pass. Detailed activity goes to log files only.
- Errors scoped to one creator log to that creator's `errors.log` and a single line to stderr, then processing continues with the next creator. A single video failure never aborts the run.

## Failure model

- **Single video fails to download** → `ERROR download failed: <id> — <tail>` in `errors.log`; the next video proceeds. The failed ID is *not* added to `archive.txt`, so it's naturally retried on the next run.
- **Single playlist manifest fails** → `WARN playlist fetch failed: <id>` in `manifests.log`; the other playlists still get fetched; the prior `_latest.json` file (if any) is preserved.
- **Channel-level failure** (canonical URL resolution, channel `/playlists`, channel `/videos`) → that creator is marked failed for this run; Pass 2 is skipped; an error line is printed; the next creator processes normally.
- **Unexpected exception inside one creator** → caught by the per-creator error boundary; full traceback to `errors.log`; one-line summary to stderr; the run continues with the next creator.

## Exit codes

- `0` — success (including runs where individual videos or creators failed; per-creator failures are *not* a run-level failure).
- `2` — startup precondition failure: missing/invalid `config.toml`, missing `ffmpeg`, or `--creator` with an unknown slug.

## Implementation status

The tool is being built in PRD-sized increments. See `.agents/prd/` for the full specs and `.agents/tasks/` for the implementation task lists.

| PRD | Scope | Status |
| --- | --- | --- |
| 1 | Project foundation, config, CLI skeleton, logging | ✅ Implemented |
| 2 | Pass 1 — manifest discovery | ✅ Implemented |
| 3 | Pass 2 — media download pipeline | ✅ Implemented |
| 4 | Normalized `metadata.json` layer | ✅ Implemented |
| 5 | `--refresh-metadata` + upgrade detection | ✅ Implemented |
| 6 | `--upgrade` — execute pending upgrades | ✅ Implemented |
| 7 | `--dry-run` — preview without side effects | ✅ Implemented |
| 8 | `--audit` / `--repair` — archive consistency check | ⏳ Not yet implemented |

The `--audit` and `--repair` flags are parseable today but do not yet have behavior wired up.

## Design notes

- **Stdlib-only package.** `archive.py` is the CLI entrypoint; per-PRD logic lives in the `youtube_archive/` package (`config.py`, `logging_setup.py`, `process.py`, `errors.py`, `utils.py`, `manifests.py`, `downloads.py`, `metadata.py`, `refresh.py`, `upgrade.py`, `dry_run.py`). Stdlib only — `tomllib`, `argparse`, `logging`, `subprocess`, `pathlib`, `hashlib`, `json`, `urllib.parse`. No `pip install` step. The project started as a single file and grew into a package once it crossed five PRDs of complexity.
- **yt-dlp via subprocess.** The script invokes the `yt-dlp` CLI rather than `import yt_dlp` — the CLI surface is more stable across releases.
- **Serial downloads.** One yt-dlp invocation completes before the next starts. Slower than parallel but produces linear logs and avoids hitting YouTube rate limits.
- **`archive.txt` is the source of truth.** Only yt-dlp writes to it (via `--download-archive`). The script reads it for filtering but never mutates it directly — that's what guarantees a failed download is naturally re-attempted on the next run.
- **Atomic JSON writes.** Every curated file (`creator.json`, `metadata.json`) is written via tempfile + `os.replace()` so a partial write is never observable on disk.
- **No retries on top of yt-dlp.** yt-dlp's built-in `--retries 10` handles transient network errors. The script doesn't layer a second retry loop.

## Project structure

```
.
├── archive.py              # CLI entrypoint (main, build_parser, dispatch)
├── youtube_archive/        # per-PRD modules (stdlib only)
│   ├── config.py           # PRD-01: config loading, ffmpeg check, env setup
│   ├── logging_setup.py    # PRD-01: UTC log formatter + per-creator loggers
│   ├── process.py          # PRD-01: subprocess helpers (yt-dlp capture/stream)
│   ├── errors.py           # PRD-01: error types + per-creator failure boundary
│   ├── utils.py            # PRD-01: timestamps, atomic IO, value helpers
│   ├── manifests.py        # PRD-02: pass 1 (manifest discovery)
│   ├── downloads.py        # PRD-03: pass 2 (media download)
│   ├── metadata.py         # PRD-04: pass 4 (normalized metadata)
│   ├── refresh.py          # PRD-05: --refresh-metadata + upgrade detection
│   ├── upgrade.py          # PRD-06: --upgrade execution + .pre-upgrade rollback
│   └── dry_run.py          # PRD-07: --dry-run preview orchestration
├── config.example.toml     # documented sample config (committed)
├── config.toml             # your config (gitignored)
├── pyproject.toml          # uv project metadata (stdlib only; no dependencies)
├── README.md               # this file
├── .gitignore
├── .agents/
│   ├── prd-overview.md     # PRD decomposition
│   ├── prd/                # one PRD spec per file
│   ├── tasks/              # one task list per PRD
│   ├── skills/             # generation-time tooling
│   └── youtube-archive-plan.md   # design doc this whole thing flows from
└── data/                   # all archived content (gitignored)
```
