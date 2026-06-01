# YouTube Archive

A stdlib-only Python orchestrator that archives YouTube creators using `yt-dlp`. Each creator gets a self-contained directory tree containing the media files, raw `info.json` from yt-dlp, normalized `metadata.json` files, and timestamped manifest snapshots that preserve the channel's playlist structure over time.

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
   # Optional. Where archived media + metadata live. Defaults to ./data.
   # Point it at a mounted volume (e.g. a NAS) to archive elsewhere; a leading
   # ~ is expanded, relative paths stay relative to the current directory.
   # data_dir = "/Volumes/NAS/youtube-archive"

   # Optional. Local scratch dir for downloads before the finished file is moved
   # into data_dir. Defaults to a system-temp subdir. Keep it local when data_dir
   # is a network mount (see "Local staging" under Design notes).
   # staging_dir = "~/.cache/youtube-archive"

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

   See `config.example.toml` for the full schema, including the optional top-level `data_dir` and `staging_dir`, per-creator overrides, `extra_playlists`, and `exclude_playlists`.

`config.toml` is gitignored. Only `config.example.toml` is checked in.

## Deploy on TrueNAS (Docker)

A `Dockerfile` + `compose.yml` + GitHub Actions workflow are checked in to publish an image to GHCR (`ghcr.io/kyleinprogress/youtube-archive`) and run it as a TrueNAS custom App via the compose-include pattern.

The container runs **supercronic** for the daily sync and a long-running **`--serve`** for the web UI. Downloads stage to a tmpfs `/staging` (in-RAM scratch), then only the finished file is moved to the bind-mounted `/data` dataset — see [Local staging](#design-notes).

1. **Tag a release** so the GitHub Actions workflow publishes an image. Tags matching `v*` push `latest`, `{{version}}`, `{{major}}.{{minor}}`, and `sha-…` to GHCR. A weekly scheduled rebuild picks up new `yt-dlp` releases.

   ```sh
   git tag v0.1.0 && git push --tags
   ```

2. **On the NAS**, create an app directory matching your include pattern (e.g. `/mnt/Hellcat/NVMe/Docker/youtube-archive/`) and drop in the three files you need:

   ```sh
   cd /mnt/Hellcat/NVMe/Docker/youtube-archive
   curl -fsSL https://raw.githubusercontent.com/kyleinprogress/Youtube-Archive/main/compose.yml -o compose.yml
   curl -fsSL https://raw.githubusercontent.com/kyleinprogress/Youtube-Archive/main/.env.example -o .env
   curl -fsSL https://raw.githubusercontent.com/kyleinprogress/Youtube-Archive/main/config.example.toml -o config.toml
   ```

3. **Edit `.env`** — set `PUID`/`PGID` to the uid/gid that owns your media dataset (`id <your-user>` on TrueNAS), `DATA_DIR` to the host path of that dataset, `ARCHIVE_CRON` if you want a non-default schedule.

4. **Edit `config.toml`** — set `data_dir = "/data"` and `staging_dir = "/staging"` (these match the container's bind mounts), then add your `[[creator]]` entries.

5. **Include the compose file** from your parent Apps compose:

   ```yaml
   include:
     - /mnt/Hellcat/NVMe/Docker/youtube-archive/compose.yml
   ```

6. **Bring it up.** TrueNAS Apps will pull the image and start the container. `docker logs youtube-archive` shows the supercronic heartbeat and each run's progress. Per-creator file logs continue to land under `<DATA_DIR>/<slug>/logs/`. The web UI is at `http://<truenas-ip>:8765/`.

To run an ad-hoc command (dry-run, audit, one creator):

```sh
docker exec youtube-archive python archive.py --dry-run
docker exec youtube-archive python archive.py --audit
docker exec youtube-archive python archive.py --creator the-stock-pot
```

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

### `--audit` — check archive consistency

```sh
uv run archive.py --audit
uv run archive.py --audit --creator creator-slug
uv run archive.py --audit --repair                 # prune orphan-in-archive entries
uv run archive.py --audit --dry-run --repair       # preview the prune
```

Read-only health check that cross-references `archive.txt` against on-disk state per creator and reports five discrepancy classes, in operational-priority order:

- **Class A — Orphan in archive.txt.** ID listed in `archive.txt` but the video folder, canonical media file, or non-zero size is missing. The next normal sync would skip re-downloading because `archive.txt` says "done", so this video is silently lost. *Primary concern; target of `--repair`.*
- **Class B — Orphan on disk.** Video folder exists with media present, but the ID isn't in `archive.txt`. Likely a partial run or manual addition.
- **Class C — Incomplete folder.** Folder is in a mid-state (`info.json` missing, or `.part` resume marker alongside complete media).
- **Class D — Invalid `metadata.json`.** Missing, unparseable, wrong `schema_version`, or `video_id` field doesn't match the folder name. Report-only — these self-heal during a normal sync via PRD 4's rebuild path.
- **Class E — Malformed `archive.txt` line.** Non-empty, non-comment line that doesn't match `youtube <11-char-video-id>`. Reported with line number.

`--audit` makes no network calls. Output goes to stdout (and `data/<slug>/logs/audit.log`).

`--audit --repair` is the only writable action: for each Class A entry, it copies `archive.txt` to `data/<slug>/archive.txt.pre-repair-<YYYYMMDDTHHMMSSZ>` (preserving mtime), then atomically rewrites `archive.txt` excluding the orphan IDs. Class B/C/D/E are never auto-fixed. `--repair` without `--audit` exits 2 with `error: --repair requires --audit`.

### `--serve` — build the browse index and serve the local web UI

```sh
uv run archive.py --serve                  # http://127.0.0.1:8765
uv run archive.py --serve --port 9000      # custom port
uv run archive.py --serve --dry-run        # print build summary; skip the write + server
```

Walks the configured data dir (see `data_dir` below) to regenerate `index.json` (the per-video / per-creator manifest that `index.html` reads), then serves the **data dir** over HTTP on `127.0.0.1` so the browser can fetch `index.json` and the media files. Runs until `Ctrl-C`.

- **Self-contained under the data dir.** `index.json` is written into the data dir and `index.html` is copied in beside it, so the served site works whether data lives in `./data` or on a mounted NAS path outside the repo. Media paths in `index.json` are relative to the data dir.
- **Bound to `127.0.0.1` only.** No `--host` knob; the server is unreachable from the LAN by design. The data dir contains channel descriptions and other state that shouldn't be served publicly.
- **Reads `config.toml`.** `--serve` loads config only to learn where the data dir is; it doesn't need `ffmpeg` or a `--creator`.
- **Always rebuilds.** `--serve` regenerates `index.json` first so the UI never shows stale data. To only rebuild (e.g. from a cron job), use `uv run build_index.py --data-dir <path>` — a thin shim around the same code that builds and exits.
- **Stdlib only.** Uses `http.server.ThreadingHTTPServer` with `SimpleHTTPRequestHandler`. Enough for one-person local browsing.

### `--help`

```sh
uv run archive.py --help
```

Lists all supported flags. The full set is `--creator`, `--manifests-only`, `--refresh-metadata`, `--upgrade`, `--audit`, `--repair`, `--dry-run`, `--serve`, `--port` — all wired up.

## Output layout

For each creator with slug `<slug>`:

```
data/<slug>/
├── creator.json                            # curated channel-level snapshot
├── archive.txt                             # yt-dlp's "already downloaded" log (one line per video)
├── archive.txt.pre-repair-YYYYMMDDTHHMMSSZ # backup written by --audit --repair (one per repair run; not auto-pruned)
├── unavailable.txt                         # permanently-gone videos, skipped on future runs (video_id<TAB>reason<TAB>flagged_at; delete a line to re-enable)
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
    ├── audit.log                           # --audit / --repair activity
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
- **Video permanently unavailable** (removed by uploader, channel terminated, policy takedown, or "no longer available") → recorded to `unavailable.txt` and reported as `unavailable` rather than `failed`; skipped on all future runs so it stops re-failing each time. Delete its line in `unavailable.txt` to retry. Private videos and bare "Video unavailable" are treated as ordinary failures (retried), since they can be transient or reversible.
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
| 8 | `--audit` / `--repair` — archive consistency check | ✅ Implemented |

All PRDs from the original roadmap are implemented.

## Design notes

- **Stdlib-only package.** `archive.py` is the CLI entrypoint; per-PRD logic lives in the `youtube_archive/` package (`config.py`, `logging_setup.py`, `process.py`, `errors.py`, `utils.py`, `manifests.py`, `downloads.py`, `metadata.py`, `refresh.py`, `upgrade.py`, `dry_run.py`, `audit.py`, `web_ui.py`). Stdlib only — `tomllib`, `argparse`, `logging`, `subprocess`, `pathlib`, `hashlib`, `json`, `urllib.parse`, `http.server`. No `pip install` step. The project started as a single file and grew into a package once it crossed five PRDs of complexity.
- **yt-dlp via subprocess.** The script invokes the `yt-dlp` CLI rather than `import yt_dlp` — the CLI surface is more stable across releases.
- **Serial downloads.** One yt-dlp invocation completes before the next starts. Slower than parallel but produces linear logs and avoids hitting YouTube rate limits.
- **Local staging, then move.** yt-dlp downloads, merges, and embeds in a local scratch dir (`staging_dir`, default a system-temp subdir) and only the finished file is moved into `data_dir` (via yt-dlp `-P temp:`/`-P home:` with a relative output template). When `data_dir` is a network mount (SMB/NFS), the OS can fail `close()` on flush *after* a complete transfer (`[Errno 9] Bad file descriptor`, `[Errno 22] Invalid argument`) — staging keeps yt-dlp's long-held write handle on local disk so only a single sequential file move crosses the network, which is far more reliable and trivially retryable.
- **`archive.txt` is the source of truth for completed downloads.** Only yt-dlp writes to it (via `--download-archive`). The script reads it for filtering but never mutates it directly — so an ordinary failed download is naturally re-attempted on the next run. The one exception is a *permanently* unavailable video, which the script records in `unavailable.txt` (a separate file it does own) so it stops re-failing every run.
- **Atomic JSON writes.** Every curated file (`creator.json`, `metadata.json`) is written via tempfile + `os.replace()` so a partial write is never observable on disk.
- **No generic retry loop on top of yt-dlp.** yt-dlp's built-in `--retries 10` handles transient network errors. The script only layers targeted one-shot re-invocations for two specific recoverable cases: a stale resume (HTTP 416 → clear partials, restart) and a transient destination-volume I/O error (yt-dlp's `Unable to download video: [Errno N] …`, e.g. `Bad file descriptor`, `Invalid argument`, `Input/output error` → retry).

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
│   ├── dry_run.py          # PRD-07: --dry-run preview orchestration
│   ├── audit.py            # PRD-08: --audit consistency check + --repair
│   └── web_ui.py           # --serve: build index.json + local browse UI
├── build_index.py          # thin shim around web_ui.cli_build_main (for cron / scripts)
├── index.html              # browse UI served by --serve
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
