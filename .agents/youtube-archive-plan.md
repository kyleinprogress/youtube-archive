# YouTube Archive — v1 Build Plan

## Context

You want to archive specific YouTube creators with full metadata, with first-class preservation of **playlist membership** (a video can live in multiple playlists, but the media file shouldn't be duplicated). The design comes from `.agents/ChatGPT-YT-DLP Video Organization.md`: a two-pass `yt-dlp` pipeline orchestrated by a Python script, with each creator getting a self-contained subtree under `data/`. State is filesystem + JSON only — no SQLite, no UI, no Plex integration in v1.

Current repo state: fresh `uv init` skeleton (Python 3.14, no dependencies, no source files). `uv 0.11.16` and `yt-dlp 2026.03.17` are both installed system-wide. `ffmpeg` will need to be confirmed (required for `--merge-output-format mkv` and `--embed-metadata`).

Decisions confirmed:
- **Multi-creator from day one** (per-creator subtree)
- **Playlists auto-discovered from the channel** — config only specifies `channel_url`; if a creator adds a new playlist it gets picked up on the next run with no config change
- **Include**: normalized `metadata.json` per video + timestamped playlist manifest snapshots
- **Defer**: CSV indexes, SHA256 hashes, SQLite, scheduling/launchd
- **Trigger**: manual CLI (`uv run archive.py`)

## Approach

Single-file Python orchestrator (`archive.py`) that shells out to the `yt-dlp` CLI as a subprocess. The CLI is more stable and easier to debug than the `yt_dlp` library API. Stdlib only — `tomllib` is built into Python 3.14, so no Python dependencies are needed.

### Directory layout (built by the script)

```
youtube-archive/
├── pyproject.toml
├── archive.py
├── config.toml
└── data/
    └── <creator-slug>/
        ├── archive.txt                          # yt-dlp dedup state
        ├── creator.json                         # channel-level metadata snapshot
        ├── videos/
        │   └── <VIDEO_ID>/
        │       ├── <VIDEO_ID>.mkv               # merged media
        │       ├── <VIDEO_ID>.info.json         # raw yt-dlp dump
        │       ├── <VIDEO_ID>.webp              # thumbnail
        │       ├── <VIDEO_ID>.en.vtt            # subtitles (when available)
        │       └── metadata.json                # our normalized layer
        ├── manifests/
        │   ├── playlists/
        │   │   ├── <PLAYLIST_ID>_latest.json
        │   │   └── <PLAYLIST_ID>_2026-05-26.json
        │   └── channel/
        │       ├── playlists_index_latest.json   # which playlists existed
        │       ├── playlists_index_2026-05-26.json
        │       ├── uploads_latest.json           # all uploaded video IDs
        │       └── uploads_2026-05-26.json
        └── logs/
            ├── download.log
            ├── manifests.log
            └── errors.log
```

**Why `%(id)s` for filenames, not titles**: titles change post-publish; ID does not. The folder already disambiguates, and `info.json` retains the title for human-readable browsing.

### `config.toml` shape

Playlists are **auto-discovered** from the channel, so you never have to hand-maintain a playlist list. The only required field is `channel_url`. Two optional fields handle edge cases:
- `exclude_playlists` — skip specific playlist IDs (e.g. auto-generated "Liked Videos" or anything you explicitly don't want archived).
- `extra_playlists` — additional playlist URLs to include beyond what the channel's public `/playlists` tab exposes. Use this for private/unlisted playlists you have the link for, or collab playlists hosted on another channel.

A `[defaults]` table sets format selection / merge container for all creators, with per-creator overrides under `[[creator]]`. These pass straight through to yt-dlp's `-f` / `-S` / `--merge-output-format` flags ([format selection docs](https://github.com/yt-dlp/yt-dlp#format-selection)).

```toml
[defaults]
# yt-dlp -f : the actual format selector. "bv*+ba/b" = best video + best audio, fall back to best combined.
format = "bv*+ba/b"

# yt-dlp -S / --format-sort : ordered preference list. yt-dlp picks the best AVAILABLE match.
# Examples: "res:1080" caps at 1080p, "codec:av01" prefers AV1, "acodec:opus" prefers Opus audio.
format_sort = ["res:1080", "codec:av01", "acodec:opus"]

# yt-dlp --merge-output-format : container for the merged file.
merge_output_format = "mkv"

# Skip videos uploaded less than this many hours ago, giving YouTube time to finish
# encoding all format tiers (1080p, 4K, AV1 often arrive minutes-to-hours after publish).
# Skipped videos do NOT get added to archive.txt, so they're reconsidered on the next run.
# Set to 0 to disable.
min_upload_age_hours = 48

[[creator]]
slug = "creator-name"          # used as directory name; must be filesystem-safe
name = "Creator Display Name"
channel_url = "https://www.youtube.com/@creator"
exclude_playlists = []         # optional: list of playlist IDs to skip
extra_playlists = []           # optional: list of playlist URLs to also include

# Per-creator overrides (any of these are optional; omitted keys inherit from [defaults]):
# format = "bv*[height<=2160]+ba/b"     # archive this one in 4K
# format_sort = ["res:2160", "codec:av01"]
# merge_output_format = "mp4"
# min_upload_age_hours = 24             # tighter gate for a creator who uploads in final form
```

Format selection notes:
- Prefer `format_sort` (`-S`) over hand-writing complex `format` fallback chains. With sort keys, yt-dlp will *always* pick the best available match without you having to enumerate fallbacks — if AV1 1080p isn't available, it falls through to VP9 1080p, then H.264 1080p, then 720p of whatever, etc.
- Common sort keys: `res:N` (cap height), `codec:av01|vp9|h264`, `acodec:opus|aac`, `vcodec`, `fps`, `tbr` (total bitrate). Full list at the docs link above.
- The `format` selector is still useful as a coarse filter (e.g. only consider streams that have both video and audio components); `format_sort` then ranks within that filter.

### Pipeline (per creator)

**Pass 1 — Manifests (read-only metadata):**
1. **Discover playlists from the channel**: `yt-dlp --flat-playlist --dump-single-json https://www.youtube.com/@creator/playlists` → returns an entry list where each entry is a playlist. Extract playlist IDs/URLs.
2. **Merge with `extra_playlists`** and **apply `exclude_playlists`** filter to produce the final playlist set.
3. **Snapshot the discovery result itself** to `manifests/channel/playlists_index_latest.json` (+ timestamped copy). This preserves the historical record of *which playlists existed* on any given day — useful when a creator deletes/renames a playlist later. Note: this snapshots the *discovered* set; `extra_playlists` entries are tracked in the playlist set used for the run but not retroactively added to the discovery snapshot (they didn't come from discovery).
4. For each playlist in the final set → `yt-dlp --flat-playlist --dump-single-json <playlist_url>` → write to `manifests/playlists/<PLAYLIST_ID>_latest.json` AND a timestamped copy `<PLAYLIST_ID>_<YYYY-MM-DD>.json`.
5. For the channel uploads tab → `yt-dlp --flat-playlist --dump-single-json https://www.youtube.com/@creator/videos` → write to `manifests/channel/uploads_latest.json` (+ timestamped). This is the canonical "everything this creator has uploaded" list, independent of playlist membership.
6. Parse all manifests, union all video IDs into the candidate set for Pass 2.

**Pass 2 — Media (per unique video ID not in `archive.txt`):**

For each candidate ID, apply the **upload-age gate** before invoking yt-dlp:
1. Try to read the upload timestamp from the Pass 1 manifest entry (the flat-playlist dump often includes `timestamp` or `release_timestamp`).
2. If missing, fetch it once cheaply: `yt-dlp --skip-download --print "%(timestamp)s" <URL>`.
3. If `(now - upload_time) < min_upload_age_hours`: log to `download.log` as "gated — too recent", **do not** add to `archive.txt`, and skip. It'll be reconsidered on the next run.
4. Otherwise proceed.

Resolve effective format settings: start with `[defaults]`, then overlay any per-creator overrides. The script then constructs:

```bash
yt-dlp \
  -f "<resolved format>" \
  -S "<resolved format_sort, joined with commas>" \
  --merge-output-format <resolved merge_output_format> \
  --download-archive data/<creator>/archive.txt \
  --write-info-json \
  --write-thumbnail \
  --write-subs --write-auto-subs --sub-langs "en.*,en" \
  --convert-thumbnails webp \
  --embed-metadata --embed-thumbnail \
  -o "data/<creator>/videos/%(id)s/%(id)s.%(ext)s" \
  "https://www.youtube.com/watch?v=<ID>"
```
`-S` is omitted from the command entirely if `format_sort` is empty/unset (lets yt-dlp use its built-in default sort).

After a successful download, write `metadata.json` with normalized fields:
```json
{
  "video_id": "abc123",
  "source_creator": "creator-slug",
  "archived_at": "2026-05-26T10:30:00Z",
  "last_metadata_check": "2026-05-26T10:30:00Z",
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
Playlist membership is derived from the Pass 1 manifests, not the per-video `info.json` (the latter only reflects the URL the video was downloaded from, which loses info when a video belongs to multiple playlists — this is the central correctness point of the whole design). The `playlists` field is rewritten on every run from current manifests; historical playlist membership lives in the timestamped manifest snapshots.

**Pass 3 — Metadata refresh + upgrade detection (opt-in, `--refresh-metadata`):**
For every video already in `archive.txt`, fetch current metadata without re-downloading:
```bash
yt-dlp --skip-download --dump-json "https://www.youtube.com/watch?v=<ID>"
```
Then for each archived video:
- If the call succeeds and `title`, `thumbnail_url`, or `description_hash` differ from `metadata.json.current`: append the **previous** `current` block to `history` (with an `observed_at` timestamp), then overwrite `current` with the new values.
- If the call fails with "Video unavailable" / "Private video" / similar: set `availability` to `"removed"` or `"private"`, set `removed_detected_at` to now (only if not already set), and leave the archived media file untouched. The whole point of the archive is that we keep the file even after YouTube doesn't.
- **Upgrade detection**: from the same `--dump-json` payload, simulate format selection using the creator's current `format` + `format_sort` to identify which format would be picked *now*. If its `height`/`vcodec` is "better" than `metadata.json.downloaded` per the same sort keys, set `upgrade_available` to a record describing the better format. If it's the same or worse, set it to `null`.
- Always update `last_metadata_check`.

This pass is gated behind a flag because it costs one network round-trip per archived video — fine to run weekly/monthly, overkill for daily.

**`--upgrade` — perform pending upgrades (destructive, opt-in):**
Separate command that re-downloads any video whose `metadata.json` has a non-null `upgrade_available`. For each:
1. Move the existing media file aside (`<ID>.mkv.pre-upgrade` in the video folder, kept until the new download succeeds).
2. Re-download the video with the current creator's format settings.
3. On success: append a `{type: "upgrade", observed_at, from: <old downloaded>, to: <new downloaded>}` entry to `history`, update `downloaded`, clear `upgrade_available`, delete the `.pre-upgrade` file.
4. On failure: restore the original file and log to `errors.log`. The original is never lost.

Keeping detection (`--refresh-metadata`) and execution (`--upgrade`) as separate commands means automated/frequent metadata refreshes never inadvertently re-download large files.

**Re-uploaded videos** (creator deletes a video and re-uploads with a new ID): treated as a new video and archived fresh on the next Pass 2. YouTube doesn't expose any old↔new linkage, so v1 doesn't try to detect this automatically. Out of scope: heuristic title/duration matching to link the two. The old archived copy stays; the new one is added.

### CLI surface

- `uv run archive.py` — run all creators (Pass 1 manifests + Pass 2 media)
- `uv run archive.py --creator <slug>` — scope any run to one creator
- `uv run archive.py --manifests-only` — Pass 1 only, no downloads (cheap, safe to run often)
- `uv run archive.py --refresh-metadata` — Pass 3 only: walk already-archived videos, update `metadata.json` (title/thumbnail/description history, availability, **upgrade detection**). Read-only on disk media. Skips Pass 1 and Pass 2 by default.
- `uv run archive.py --upgrade` — destructive: re-download videos flagged with `upgrade_available`. Keeps a `.pre-upgrade` backup until the new download succeeds. Combine with `--creator <slug>` to scope.
- `uv run archive.py --dry-run` — list what would download, no side effects. Combinable with `--refresh-metadata` (shows pending metadata changes) or `--upgrade` (shows which files would be replaced and with what).

### Error handling posture

- A single failing video does not abort the creator. Log to `errors.log` with the video ID and yt-dlp stderr, continue.
- A failing manifest **does** abort that creator's Pass 2 (we'd be downloading with stale knowledge). Log and move on to the next creator.
- Subprocess output streamed to per-creator log files via `subprocess.Popen` + thread/pipe wiring (stdlib only).

## Files to create

| Path | Purpose |
|------|---------|
| `archive.py` | Orchestrator: config load, manifest pass, media pass, metadata writer, logging |
| `config.toml` | Creator + playlist definitions (you fill this in) |
| `.gitignore` | Exclude `data/`, `.venv/`, `__pycache__/` |
| `pyproject.toml` | Update name/description; no new dependencies needed |

`data/` is created on first run.

## Out of scope for v1 (explicit deferrals)

- CSV indexes (`videos.csv`, `playlists.csv`, `memberships.csv`) — derivable from JSON later
- SHA256 integrity hashes
- SQLite index
- launchd/cron scheduling
- Comments archival (`--write-comments`)
- Cookie/auth handling for members-only content
- Plex/Jellyfin integration
- **Linking re-uploaded videos** to their predecessors (no reliable signal from YouTube; would require heuristic matching)
- **Re-downloading updated thumbnails as separate files** — thumbnail URL changes are tracked in `metadata.json` history, but the on-disk `.webp` is not refreshed. The original is preserved; refresh is a manual op if you ever want it.

## Verification

1. **ffmpeg check**: confirm `ffmpeg -version` works on the system before first download (required for mkv merge + metadata embed). If missing: `brew install ffmpeg`.
2. **Playlist-discovery sanity check**: pick a creator with a small number of public playlists. Run `uv run archive.py --creator <slug> --manifests-only`. Confirm:
   - `data/<slug>/manifests/channel/playlists_index_latest.json` exists and lists every playlist visible on `https://www.youtube.com/@creator/playlists`
   - For each discovered playlist, `data/<slug>/manifests/playlists/<PLAYLIST_ID>_latest.json` was written
   - `data/<slug>/manifests/channel/uploads_latest.json` exists
3. **Smoke run on one creator with one short playlist** (5–10 videos):
   - First full run: `uv run archive.py --creator <slug>`. Confirm:
     - `data/<slug>/archive.txt` has one line per downloaded video
     - Each `data/<slug>/videos/<ID>/` contains `.mkv`, `.info.json`, `.webp`, `metadata.json`, and a subtitle file when available
     - `metadata.json` lists the correct playlist memberships
   - Second run immediately after: should be a no-op (no new downloads; manifests are overwritten in place, and same-day timestamped snapshots are also overwritten — that's fine).
4. **Manifest-only run**: `uv run archive.py --manifests-only`. Confirm no media is touched, only manifest files change.
5. **Dry run**: `uv run archive.py --dry-run` on a creator with new videos. Confirm output lists the IDs that *would* download and no files change on disk.
6. **Multi-playlist video**: pick a creator where you know a video appears in two of their playlists. Confirm only one media folder exists for that video and its `metadata.json` lists both playlist memberships.
7. **Auto-discovery of new playlists**: add `exclude_playlists = ["<some_id>"]` to skip one playlist, run, confirm it's absent. Remove the exclude, re-run, confirm the playlist's manifest now appears and any new video IDs from it get downloaded.
8. **`extra_playlists`**: add an arbitrary playlist URL (e.g. one from a different channel) to `extra_playlists`, run `--manifests-only`, confirm its manifest appears alongside the discovered ones and its video IDs are eligible for Pass 2.
9. **Metadata refresh — title change**: after a smoke run, manually edit `current.title` in one video's `metadata.json` to a wrong value, then run `--refresh-metadata`. Confirm the script detects the diff against YouTube's actual title, appends the wrong value to `history`, and restores `current.title` to the real title. Confirms the diff/history logic works.
10. **Metadata refresh — unavailable video**: pick a known-deleted video ID and add it to `archive.txt` by hand (and create a placeholder video folder + `metadata.json`). Run `--refresh-metadata`. Confirm `availability` flips to `"removed"` and `removed_detected_at` is populated, and the placeholder folder is **not** deleted.
11. **Format selection — global default**: with `[defaults]` set to `res:1080` + `codec:av01`, run on a creator who has a known 1080p AV1 video available. Inspect the downloaded `.mkv` with `ffprobe -v error -show_entries stream=codec_name,height` and confirm codec/resolution match.
12. **Format selection — per-creator override**: add `format_sort = ["res:2160", "codec:av01"]` under one creator block. Re-run for a video known to be available in 4K. Confirm that creator's video is 2160p while others stayed at 1080p.
13. **Upload-age gate**: set `min_upload_age_hours = 999999` temporarily, run on a creator with recent uploads. Confirm new videos are logged as gated and **not** added to `archive.txt`. Drop back to a normal value, re-run, confirm they now download.
14. **Upgrade detection**: manually edit one video's `metadata.json.downloaded` to a deliberately worse format (e.g. `height: 360`, `vcodec: "avc1.42001E"`). Run `--refresh-metadata`. Confirm `upgrade_available` is populated with the actual better format. Run `--dry-run --upgrade`; confirm it lists this video as a target without touching the file. Then run `--upgrade`; confirm the file is replaced, `history` has an upgrade entry, `upgrade_available` is back to null, and no `.pre-upgrade` leftovers remain.
15. **Failure isolation**: temporarily set `channel_url` to a nonsense URL for one creator; confirm the run logs the failure under that creator and continues to the next creator.
