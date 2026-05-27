# YT-DLP Video Organization

**Created:** 2026/5/26 10:22:13  

## Prompt:
5/26/2026, 10:22:13 AM

What's the best way to download videos from Youtube (using yt-dlp) and properly organize them so you can tell if they were included in a playlist?

THere's a creator I want to archive and a lot of the videos are also included in a playlist...so I'd like to be able to save that metadata as well as the info for each video.

## Response:
5/26/2026, 10:22:13 AM

The cleanest approach is:

1. Download videos with full metadata sidecars (`.info.json`, thumbnails, subtitles, comments if wanted)
2. Preserve playlist structure in filenames/directories
3. Maintain a local index/database so videos can belong to multiple playlists without duplication
4. Use yt-dlp’s archive functionality so re-runs are incremental

The key design decision is this:

- A video can appear in multiple playlists
- You do not want duplicate media files
- But you *do* want playlist membership preserved

That means you should separate:
- the canonical video storage
- the playlist organization metadata

A good structure looks like this:

```text
Archive/
├── channels/
│   └── CreatorName/
│       ├── videos/
│       │   ├── VIDEO_ID/
│       │   │   ├── video.mkv
│       │   │   ├── info.json
│       │   │   ├── thumbnail.webp
│       │   │   └── subtitles.en.vtt
│       │   └── ...
│       │
│       ├── playlists/
│       │   ├── Playlist Name/
│       │   │   └── playlist.json
│       │   └── ...
│       │
│       └── indexes/
│           ├── videos.csv
│           └── playlists.csv
```

The important part is using the YouTube `video_id` as the canonical storage key.

Example:
```text
dQw4w9WgXcQ/
```

That ID is permanent and avoids filename chaos when creators rename videos.

For the actual yt-dlp command, something like this works well:

```bash
yt-dlp \
  --download-archive archive.txt \
  --write-info-json \
  --write-thumbnail \
  --write-subs \
  --write-auto-subs \
  --embed-metadata \
  --embed-thumbnail \
  --all-subs \
  --convert-thumbnails webp \
  --merge-output-format mkv \
  -o "%(uploader)s/videos/%(id)s/%(title)s [%(id)s].%(ext)s" \
  URL
```

Important flags:

- `--write-info-json`
  Saves all metadata from YouTube

- `--download-archive archive.txt`
  Prevents re-downloading already archived videos

- `%(id)s`
  Critical for stable organization

- `mkv`
  Best archival container

The `.info.json` file already includes playlist metadata when downloaded from a playlist URL.

Example fields:
```json
"playlist": "My Playlist",
"playlist_id": "PL12345",
"playlist_index": 7
```

But here's the catch:

If you download the same video later from another playlist, the new playlist info overwrites the old metadata if stored in the same folder.

So the better strategy is:

## Separate playlist metadata collection

Instead of relying solely on the per-video JSON, also export playlist manifests.

Example:

```bash
yt-dlp \
  --flat-playlist \
  --dump-single-json \
  PLAYLIST_URL > playlist.json
```

That gives you:
- playlist title
- order
- included video IDs
- timestamps
- uploader
- etc.

Then your local system knows:

```text
VIDEO_ID X belongs to:
- Playlist A
- Playlist B
- Playlist C
```

without duplicating files.

This is much closer to how Plex/Jellyfin/music libraries work internally.

For long-term archival, I would strongly recommend:

## Recommended archival stack

### Media
- `mkv`
- highest available quality
- original audio preserved

### Metadata
- `.info.json`
- playlist manifests
- thumbnails
- subtitles

### Indexing
At minimum:
- SQLite DB
or
- CSV exports

### Deduplication
Use:
```bash
--download-archive
```

and canonical `video_id` folders.

---

A very good advanced approach is:

## Two-pass ingestion

### Pass 1 — scrape playlists/channels

Collect:
- playlist manifests
- channel metadata
- video lists

No media yet.

### Pass 2 — download unique videos

Feed unique IDs into yt-dlp.

This scales much better for large creators.

---

If you want, I can also help you build:

- a fully automated archival script
- SQLite schema for playlist/video relationships
- a Docker container workflow
- a metadata browser UI
- integration with Plex/Jellyfin
- incremental nightly sync jobs
- a filesystem layout optimized for ZFS/unRAID/TrueNAS

Given your homelab setup, this is very achievable and worth doing correctly upfront.

## Prompt:
5/26/2026, 10:26:30 AM

Right now, I just want to make sure I can archive and save the data...so not worried about UI, plex, etc...

Would it make more sense to build a simple python script (using UV) to do the daily download with the 2 pass model? Or is that better suited for something like bash/shell?

## Response:
5/26/2026, 10:26:31 AM

I’d treat this as a small ingestion pipeline, not a one-off download command. I’ll frame the trade-off and give you a practical starting structure you can run with `uv`.

Use **Python with `uv`**.

Bash is fine for a single command, but your use case has state:

- creator URL list
- playlist manifests
- unique video IDs
- deduping
- retry logging
- daily runs
- metadata validation
- future SQLite/indexing

That crosses the line where Python is cleaner.

Use Python as the orchestrator, but still call `yt-dlp` as a subprocess. I would **not** start with the Python `yt_dlp` library API unless you need tighter integration. The CLI is stable and easier to debug. yt-dlp supports `--download-archive`, `--write-info-json`, `--dump-single-json`, and playlist extraction flags directly. ([GitHub](https://github.com/yt-dlp/yt-dlp?utm_source=chatgpt.com))

Recommended pattern:

```text
youtube-archive/
├── pyproject.toml
├── archive.py
├── config.toml
├── data/
│   ├── archive.txt
│   ├── manifests/
│   │   ├── playlists/
│   │   └── channels/
│   ├── logs/
│   └── videos/
│       └── VIDEO_ID/
```

Daily flow:

```text
1. Read config.toml
2. Fetch playlist/channel manifests only
3. Extract all video IDs
4. Write playlist membership index
5. Download only unique videos
6. Save each video's .info.json, thumbnail, subtitles, media
7. Use archive.txt to skip already-downloaded videos
```

For the metadata pass:

```bash
yt-dlp \
  --flat-playlist \
  --dump-single-json \
  "PLAYLIST_URL" \
  > data/manifests/playlists/PLAYLIST_ID.json
```

For the media pass:

```bash
yt-dlp \
  --download-archive data/archive.txt \
  --write-info-json \
  --write-thumbnail \
  --write-subs \
  --write-auto-subs \
  --embed-metadata \
  --embed-thumbnail \
  --merge-output-format mkv \
  -o "data/videos/%(id)s/%(title)s [%(id)s].%(ext)s" \
  "https://www.youtube.com/watch?v=VIDEO_ID"
```

I’d avoid playlist-folder downloads as the primary storage. They look nice at first, but they create duplication and messy rename issues. Store by `VIDEO_ID`, then keep playlist membership as JSON/CSV.

Minimal `config.toml`:

```toml
[archive]
root = "data"

[creator]
name = "Creator Name"
channel_url = "https://www.youtube.com/@creator"

playlists = [
  "https://www.youtube.com/playlist?list=PLxxxx",
  "https://www.youtube.com/playlist?list=PLyyyy"
]
```

My recommendation:

```text
Python + uv = correct choice
Bash = okay for testing commands
SQLite = optional later
yt-dlp CLI = use this first
```

The main trap is assuming the per-video `.info.json` is enough. It is not, because playlist membership is contextual. Save playlist manifests separately.

## Prompt:
5/26/2026, 10:30:37 AM

Can you update the structure to include a directory underneath 'data' for the creator? That way, I can replicate the same structure (archive.txt, videos, etc...) for each creator I want to archive?

## Response:
5/26/2026, 10:30:38 AM

Yes. That’s the better long-term structure.

You want each creator to be a self-contained archive boundary with:
- its own archive state
- manifests
- logs
- videos
- metadata
- future SQLite DB if needed

I’d structure it like this:

```text
youtube-archive/
├── pyproject.toml
├── archive.py
├── config.toml
└── data/
    ├── creator-name/
    │   ├── archive.txt
    │   ├── creator.json
    │   ├── videos/
    │   │   ├── VIDEO_ID/
    │   │   │   ├── video.mkv
    │   │   │   ├── info.json
    │   │   │   ├── thumbnail.webp
    │   │   │   ├── subtitles.en.vtt
    │   │   │   └── metadata.json
    │   │   └── ...
    │   │
    │   ├── manifests/
    │   │   ├── playlists/
    │   │   │   ├── PLAYLIST_ID.json
    │   │   │   └── ...
    │   │   │
    │   │   ├── channel/
    │   │   │   └── uploads.json
    │   │   │
    │   │   └── indexes/
    │   │       ├── videos.csv
    │   │       ├── playlists.csv
    │   │       └── memberships.csv
    │   │
    │   ├── logs/
    │   │   ├── download.log
    │   │   ├── errors.log
    │   │   └── manifests.log
    │   │
    │   └── temp/
    │
    └── another-creator/
```

A few important design choices here:

# 1. Separate `info.json` vs your own `metadata.json`

Keep the raw yt-dlp dump untouched:

```text
info.json
```

Then create your own normalized metadata:

```text
metadata.json
```

Example:

```json
{
  "video_id": "abc123",
  "playlists": [
    {
      "id": "PLxyz",
      "title": "Best Videos",
      "index": 7
    }
  ],
  "archived_at": "2026-05-26T20:00:00Z",
  "source_creator": "creator-name"
}
```

That prevents future yt-dlp updates from breaking your custom metadata.

---

# 2. One `archive.txt` per creator

Very important.

Otherwise:
- cross-channel duplicate IDs
- weird management issues
- impossible selective re-runs

Per-creator isolation is cleaner operationally.

---

# 3. Keep playlist manifests forever

This matters more than people realize.

Creators:
- rename playlists
- reorder videos
- delete entries
- remove privated videos

Your manifest snapshots preserve historical state.

I would timestamp them:

```text
PLAYLIST_ID_2026-05-26.json
```

or keep:
- latest.json
- dated snapshots

depending on how archival you want to be.

---

# 4. Use VIDEO_ID folders, not titles

Correct:
```text
videos/dQw4w9WgXcQ/
```

Not:
```text
videos/My Cool Video/
```

Titles change constantly.

---

# 5. Consider adding hashes later

Eventually:

```text
sha256.txt
```

inside each video folder.

That gives you integrity verification for long-term storage.

Especially useful if you move to:
- ZFS cold storage
- remote replication
- tape/offsite
- glacier-like backups

---

# 6. Suggested future evolution

You are effectively building a lightweight digital preservation pipeline.

Your eventual progression probably becomes:

```text
Phase 1:
Filesystem + JSON

Phase 2:
SQLite index

Phase 3:
Search/UI

Phase 4:
Distributed archival + integrity checks
```

That progression scales naturally without redesigning the storage model later.

And importantly: this structure survives tooling changes. Even if yt-dlp vanished tomorrow, your archive remains understandable.



---
Powered by [ChatGPT Exporter](https://www.chatgptexporter.com)