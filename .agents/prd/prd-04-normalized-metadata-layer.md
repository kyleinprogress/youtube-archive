# PRD 04 — Normalized Metadata Layer

## 1. Introduction / Overview

This PRD adds the **normalized `metadata.json` file per archived video** — the layer that sits on top of yt-dlp's raw `info.json` and gives the rest of the system a stable, documented schema to read and write. The single most important responsibility of this PRD is to derive each video's `playlists` membership from the Pass 1 manifests (not from `info.json`), because a video can belong to multiple playlists and `info.json` only ever reflects the one URL the video was downloaded from. **This is the central correctness point of the entire design** ([overview](../prd-overview.md) §"PRD 4").

When PRD 4 ships, `uv run archive.py` produces archives that are fully self-describing: every `data/<slug>/videos/<ID>/` folder contains a `metadata.json` whose `playlists` array correctly lists every playlist (within that creator's tracked set) that contains the video, with current titles and 1-based playlist indices. Subsequent PRDs (5 refresh, 6 upgrade) build on this file.

PRD 4 deliberately does *not* fetch anything from YouTube. It reads only from Pass 1's in-memory data (PRD 2 §4.7), `info.json` files on disk (written by PRD 3 §4.6), and any existing `metadata.json` files from prior runs. PRD 5 owns the YouTube-side refresh.

## 2. Goals

1. After a normal run (`uv run archive.py`), every `data/<slug>/videos/<ID>/` folder contains a well-formed `metadata.json` with all required fields populated.
2. A video that appears in N playlists for a creator has exactly one media folder, one `metadata.json`, and an N-entry `playlists` array — proving the "playlist membership is derived, not duplicated" design.
3. On a re-run when no new videos are downloaded, `metadata.json` files are still updated for any video whose playlist membership has changed since the previous run (added to a new playlist, removed from a playlist, reordered within a playlist).
4. PRD 4's writes never clobber fields that PRD 5 will manage (`history`, `availability`, `removed_detected_at`, `upgrade_available`, `last_metadata_check`, `current.*`).
5. Orphans (IDs in `archive.txt` with no on-disk folder) cause zero log noise — PRD 8's `--audit` is the surfacing mechanism.

## 3. User Stories

- **As the developer**, when I open `data/<slug>/videos/<ID>/metadata.json` for a video that's in three of the creator's playlists, I want to see three entries in the `playlists` array — not one, and not three duplicated media folders.
- **As the developer**, when a creator reorders a playlist (moves an old video from position 12 to position 1), I want my next normal run to update the `index` in that video's `metadata.json` automatically, without needing a special flag.
- **As the developer**, when a creator removes a video from a playlist (but doesn't delete the video), I want the next run to remove that playlist from the video's `playlists` array — while the historical record stays preserved in the timestamped playlist manifest snapshots from PRD 2.
- **As the developer building PRD 5**, I want a guaranteed-stable schema in `metadata.json` so my refresh pass can read `current.title` and `current.description_hash` without worrying about field rename or shape changes.

## 4. Functional Requirements

### 4.1 When PRD 4 runs

1. PRD 4 MUST run as the **third phase** of the default `uv run archive.py` invocation, per creator: Pass 1 (PRD 2) → Pass 2 (PRD 3) → PRD 4 metadata pass.
2. PRD 4 MUST be skipped entirely for any creator whose Pass 1 set `channel_level_failed = True` (PRD 2 §4.7). Rationale: incomplete manifest data would cause us to wipe correct playlist memberships from existing `metadata.json` files. The previous run's `metadata.json` files remain untouched on disk.
3. PRD 4 MUST be skipped entirely under:
    - `--manifests-only` (no downloads, no metadata writes)
    - `--refresh-metadata` (PRD 5 manages its own writes)
    - `--upgrade` (PRD 6 manages its own writes)
    - `--audit` (PRD 8 is read-only)
4. PRD 4 runs even if PRD 3 downloaded zero new videos this run — playlist memberships for existing videos still need to stay in sync with the latest manifests.

### 4.2 The `metadata.json` schema

5. Every `metadata.json` MUST conform exactly to this shape:

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
    "description_hash": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
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

6. Field types and constraints:
    - `schema_version`: integer, always `1` in this PRD.
    - `video_id`: string, the YouTube video ID. MUST match the folder name and `info.json`'s `id` field.
    - `source_creator`: string, the creator's `slug` from `config.toml`.
    - `archived_at`: ISO 8601 UTC string with `Z` suffix. Written once at first metadata creation; never changes after.
    - `last_metadata_check`: ISO 8601 UTC string with `Z` suffix. PRD 4 sets this equal to `archived_at` on first creation. PRD 5 updates it on each refresh. PRD 4 does NOT update it on subsequent runs.
    - `availability`: one of `"available"`, `"removed"`, `"private"`, `"members_only"`, `"unknown"`. PRD 4 always sets `"available"` at first creation. PRD 5 mutates this field.
    - `removed_detected_at`: ISO 8601 UTC string or `null`. PRD 4 always sets `null` at first creation. PRD 5 mutates it.
    - `current.title`: string. Pulled from `info.json`'s top-level `title`.
    - `current.thumbnail_url`: string. Pulled from `info.json`'s top-level `thumbnail` field (the URL yt-dlp considers canonical, not an entry from the `thumbnails` array).
    - `current.description_hash`: string of the form `sha256:<64 hex chars>`. See §4.4 below.
    - `downloaded.format_id`: string. Pulled from `info.json`'s `format_id`.
    - `downloaded.height`: integer. Pulled from `info.json`'s `height`. If missing/null in `info.json`, store `null`.
    - `downloaded.vcodec`: string. Pulled from `info.json`'s `vcodec`. `"none"` if audio-only.
    - `downloaded.acodec`: string. Pulled from `info.json`'s `acodec`. `"none"` if video-only.
    - `downloaded.filesize`: integer (bytes). Pulled from `info.json`'s `filesize`; if null, fall back to `filesize_approx`; if both null, store the actual on-disk size of the merged media file via `os.path.getsize`.
    - `upgrade_available`: `null` at first creation. PRD 5 may populate it with a descriptor object.
    - `history`: empty array `[]` at first creation. PRD 5 / PRD 6 append entries.
    - `playlists`: array of `{id, title, index}` objects. See §4.5.

### 4.3 First-time creation path

7. For each video ID in `archive.txt` whose `data/<slug>/videos/<ID>/metadata.json` does NOT exist, PRD 4 MUST:
    1. Verify `data/<slug>/videos/<ID>/<ID>.info.json` exists. If missing, treat as a corruption case (§4.6 #14).
    2. Read and parse `info.json`.
    3. Build the full schema per §4.2 #5 using the data from `info.json` plus the in-memory playlist lookup from §4.5.
    4. Atomically write the result to `data/<slug>/videos/<ID>/metadata.json` per §4.7.
    5. Log one `INFO` line to `download.log`: `metadata.json created: <VIDEO_ID>`.
8. `archived_at` and `last_metadata_check` are both set to `datetime.now(timezone.utc)` at first creation, in the same `strftime('%Y-%m-%dT%H:%M:%SZ')` format used elsewhere in the project.
9. If the merged media file (e.g., `<ID>.mkv`) doesn't exist when PRD 4 walks the folder (folder exists but media missing — an "incomplete folder" case from PRD 8 §discrepancy classes), PRD 4 MUST skip writing `metadata.json` and log one `WARN` line: `metadata.json skipped: <VIDEO_ID> (media file missing)`. This stays distinct from the orphan case (§4.6) where the whole folder is absent.

### 4.4 Description hash computation

10. `current.description_hash` MUST be computed as `"sha256:" + sha256(description).hexdigest()`, where `description` is the raw string from `info.json`'s `description` field encoded as UTF-8 bytes.
11. If `info.json`'s `description` is `null` or absent, store `"sha256:" + sha256(b"").hexdigest()` (i.e., the SHA-256 of empty bytes, which is `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`). This gives PRD 5 a stable baseline to diff against if the description later becomes non-empty.
12. No normalization is performed — whitespace, line endings, and casing are all preserved in the hashed bytes.

### 4.5 Playlist membership derivation

13. **Before** walking any video, PRD 4 MUST obtain the per-creator playlist membership lookup from PRD 2's in-memory data structure (PRD 2 §4.7 #28, the `playlist_membership` dict keyed by video ID). PRD 4 does NOT re-parse manifest files from disk.
14. For each video being processed (whether first-creation or re-run), the `playlists` field of `metadata.json` MUST be set to the lookup result for that video ID — an array of `{id, title, index}` objects in the order they appear in the lookup. If the video ID is absent from the lookup (i.e., not currently in any tracked playlist), `playlists` is set to the empty array `[]`.
15. The `title` stored is the playlist's current title from the latest Pass 1 manifest. If a playlist was renamed since the last run, the new name appears here automatically.
16. The `index` is the video's 1-based position in the playlist (matching `info.json`'s `playlist_index` convention).
17. Videos that appear only in the uploads tab (no playlist memberships) end up with `playlists: []`. This is correct, not a bug.

### 4.6 Re-run behavior and orphan handling

18. For each video ID in `archive.txt` whose `metadata.json` already exists, PRD 4 MUST:
    1. Read the existing `metadata.json`.
    2. Replace ONLY the `playlists` field with the freshly-computed array from §4.5.
    3. Leave every other field — `archived_at`, `last_metadata_check`, `availability`, `removed_detected_at`, `current.*`, `downloaded.*`, `upgrade_available`, `history`, `schema_version`, `video_id`, `source_creator` — exactly as they were on disk.
    4. Atomically write back per §4.7.
    5. If the `playlists` array actually changed (different IDs, different indices, or different order), log one `INFO` line to `download.log`: `metadata.json playlists updated: <VIDEO_ID> (N entries)`. If it's identical to what was on disk, write the file anyway (cost is negligible) but log nothing — keeps logs clean on no-op runs.
19. **Orphan path**: if `archive.txt` contains a video ID but `data/<slug>/videos/<ID>/` does NOT exist, PRD 4 MUST skip the ID **silently** — no log line, no error. Per PRD 3 Q4=A, `archive.txt` is the source of truth; PRD 8 (`--audit`) is the proper place to surface orphans systematically.
20. **Corrupted folder path**: if the folder exists but `info.json` is missing AND `metadata.json` is also missing (so we can't create the schema and there's nothing to update), PRD 4 MUST log one `WARN` to `download.log`: `metadata.json skipped: <VIDEO_ID> (info.json missing — folder corruption?)`. Skip the ID and continue.
21. **Rebuild path**: if `info.json` exists but `metadata.json` is missing (e.g., user manually deleted `metadata.json`), PRD 4 follows the first-creation path (§4.3) and logs `metadata.json rebuilt: <VIDEO_ID> (was missing)` instead of "created".

### 4.7 Atomic write

22. Every `metadata.json` write MUST be atomic: write the JSON to a sibling `metadata.json.tmp`, then `os.replace()` it onto `metadata.json`. This prevents readers (including future tooling) from observing a half-written file if the process is interrupted mid-write.
23. JSON output MUST use `json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False)`. `sort_keys=False` preserves the documented field order in §4.2 #5 for readability; field order in the produced output MUST match the schema example exactly.
24. Files MUST end with a trailing newline (consistent with most editor conventions).

### 4.8 Logging

25. PRD 4 logs to `data/<slug>/logs/download.log` (the same log used by PRD 3). Per-creator phase markers:
    - At start of PRD 4 for a creator: `INFO PRD 4 metadata pass starting`
    - Per video, one of:
      - `INFO metadata.json created: <VIDEO_ID>`
      - `INFO metadata.json rebuilt: <VIDEO_ID> (was missing)`
      - `INFO metadata.json playlists updated: <VIDEO_ID> (N entries)` (only when membership changed)
      - `WARN metadata.json skipped: <VIDEO_ID> (media file missing)`
      - `WARN metadata.json skipped: <VIDEO_ID> (info.json missing — folder corruption?)`
      - (no line at all for orphans or no-op updates)
    - At end: `INFO PRD 4 metadata pass complete: <C> created, <R> rebuilt, <U> playlists-updated, <S> skipped`
26. PRD 4 does NOT write to `manifests.log` (despite reading manifest-derived data) or `errors.log` (its skips are warnings, not errors). Any unexpected exception is caught by `creator_scope` from PRD 1 §4.9 and routed to `errors.log` per the established pattern.
27. Console output for PRD 4 is silent — no new lines beyond what PRD 3 already prints. The end-of-creator summary from PRD 3 (§4.24) is unchanged; PRD 4 work is folded into the same per-creator console block.

## 5. Non-Goals (Out of Scope)

- **Fetching anything from YouTube.** PRD 4 reads only from local files and PRD 2's in-memory data. PRD 5 owns YouTube-side calls.
- **Mutating `current.*`, `history`, `availability`, `removed_detected_at`, `last_metadata_check`, or `upgrade_available`** on any existing `metadata.json`. Those are PRD 5's responsibility entirely. PRD 4 only sets them at first creation.
- **Re-reading `info.json` on re-runs.** `info.json` is yt-dlp output; we read it once at first creation and trust the cached values in `metadata.json` thereafter.
- **Hash normalization** for descriptions (whitespace stripping, casing). Raw bytes only.
- **Schema migration logic** for future `schema_version` bumps. v1 just writes `1` and reads `1`. Migration code is a future PRD if/when the schema changes.
- **Surfacing orphans** (archive.txt entries with no folder). That's PRD 8.
- **Pruning `metadata.json` files for IDs no longer in `archive.txt`.** Don't delete metadata for removed entries; the file is harmless and may carry useful history.
- **Computing or storing media file hashes** (SHA-256 of `.mkv` etc.). Plan defers this explicitly.
- **A standalone CLI command to force `metadata.json` regeneration.** If you need to regenerate, delete the file manually and the next normal run rebuilds it (§4.6 #21).

## 6. Design Considerations

- **Field ordering in the output.** The schema example in §4.2 #5 is also the canonical field order on disk. Use `json.dumps(..., sort_keys=False)` and build the dict in this exact order. Rationale: makes `metadata.json` files diff cleanly across runs (e.g., `git diff` or `diff -u` shows real changes, not key-reorder noise).
- **Why playlists are rewritten unconditionally on every run.** Cheaper to always write than to compute a diff and conditionally write. The `INFO` log line is conditional on actual change (per §4.18 #5) to keep logs clean. Disk I/O on a few hundred small JSON files per run is negligible.
- **Why we write the file even when `playlists` is unchanged.** Keeps the write path uniform — one code path that handles "create" and "update" alike. Avoids subtle bugs where "did we already write this?" tracking gets out of sync.
- **Empty array vs. missing field.** `playlists: []` and `history: []` are always present; never omit the field, never use `null`. This makes downstream code simpler (no `is None or []` checks needed).
- **`description_hash` for empty descriptions.** The hash of empty bytes is a well-known constant; using it as a baseline lets PRD 5 detect "creator added a description that wasn't there before" cleanly.
- **`source_creator` is the slug, not the display name.** The slug is filesystem-safe and immutable; the display name (`name` in config) can change without affecting the archive. If you later want the human-readable name when looking at a `metadata.json`, cross-reference with `data/<slug>/creator.json` (PRD 2 §4.2).

## 7. Technical Considerations

- **PRD dependencies:**
  - PRD 1: per-creator logger (`download.log`), `creator_scope` error boundary.
  - PRD 2: in-memory `playlist_membership` data structure (PRD 2 §4.7 #28) and `channel_level_failed` flag.
  - PRD 3: `archive.txt` (the canonical list of archived video IDs), `<ID>.info.json` written next to the media file, the merged media file itself (for the `filesize` fallback in §4.6 #6).
- **Reading `archive.txt`:** one open, line-by-line parse. Each line is `youtube <VIDEO_ID>` — split on whitespace, take the second token. Empty lines and comments (`# ...`) are tolerated and skipped (yt-dlp doesn't actually emit comments, but we should be lenient).
- **`info.json` reading:** `json.load(open(...))` is fine — these files are typically 20-200 KB. No streaming needed.
- **Hash implementation:** `hashlib.sha256(description.encode("utf-8")).hexdigest()`. Stdlib only.
- **Atomic write helper:** consider providing a `write_json_atomic(path, obj)` helper at module level for both PRD 4 and PRD 2 to use (PRD 2 §7 also calls for atomic writes). DRYs up the pattern.
- **Walking the archive:** iterate `archive.txt` rather than `os.listdir(data/<slug>/videos/)`. `archive.txt` is the source of truth (PRD 3 §4.2); listing the directory would find orphan-on-disk folders that aren't in `archive.txt`, which we deliberately don't process here.
- **Performance:** for a creator with 1000 archived videos, PRD 4 reads `archive.txt` once, builds the membership lookup once (already in memory from PRD 2), then does 1000 small file reads + 1000 small file writes. Should complete in seconds.

## 8. Success Metrics

This PRD is done when **all** of the following are true:

1. **First-creation produces a complete file.** After a fresh `uv run archive.py --creator <slug>` against a 5-video playlist, every video folder contains a `metadata.json`. `python -m json.tool` parses each cleanly. Every required field from §4.2 #5 is present and has the documented type.
2. **Multi-playlist correctness (verification item #6).** For a video known to appear in 2 playlists from the same creator, exactly one folder exists, and its `metadata.json`'s `playlists` array has 2 entries with the correct `id`, `title`, and `index` for each.
3. **Single-playlist video.** A video appearing in only 1 playlist has 1 entry in `playlists`.
4. **Uploads-only video.** A video appearing only in the uploads manifest (not in any playlist) has `playlists: []`.
5. **`description_hash` format.** For an arbitrary archived video, `current.description_hash` matches the regex `^sha256:[0-9a-f]{64}$`. Independently re-computing `sha256(info.json["description"].encode("utf-8")).hexdigest()` matches the stored value.
6. **Idempotent re-runs.** A second `uv run archive.py --creator <slug>` immediately after the first: every `metadata.json` is rewritten (file mtime updates) but its content is byte-identical to the previous version. No log lines about playlist updates (because nothing changed).
7. **Playlist add detection.** Add a previously-archived video to a new playlist on YouTube. Re-run. Confirm `metadata.json` for that video now has the new entry in `playlists`, and `download.log` contains `metadata.json playlists updated: <VIDEO_ID> (N entries)`.
8. **Playlist remove detection.** Remove a previously-archived video from one of its playlists on YouTube. Re-run. Confirm `metadata.json`'s `playlists` array shrinks by one entry.
9. **Playlist rename propagation.** Rename a playlist on YouTube. Re-run. Every `metadata.json` for videos in that playlist has the new `title` (with same `id`).
10. **PRD 5 territory is untouched.** Manually edit a `metadata.json` to set `availability: "removed"`, `removed_detected_at: "2025-01-01T00:00:00Z"`, append a fake `history` entry, and change `current.title` to `"manually edited"`. Run again normally. Confirm all four fields are exactly as left — only `playlists` may change.
11. **Orphan silence.** Add a fake line to `archive.txt` (`youtube zzz999fake`). Re-run. Confirm zero log lines mention the fake ID and the run exits cleanly.
12. **Corrupted folder warning.** Delete `info.json` from one video's folder. Re-run. Confirm one `WARN metadata.json skipped: <ID> (info.json missing — folder corruption?)` appears in `download.log` and the run continues.
13. **Channel-level failure skip.** Force a Pass 1 channel-level failure for one creator. Confirm no `metadata.json` files are written or updated for that creator on this run, and existing files are byte-identical to before.
14. **Manifests-only mode is a no-op.** Run `uv run archive.py --manifests-only`. Confirm no `metadata.json` files anywhere are modified (mtime check).
15. **Multi-playlist video — disk verification.** For a multi-playlist video, `ls data/<slug>/videos/<ID>/` shows exactly one media file. There is no second copy of the video under another folder.

These map onto verification items **#3** (the `metadata.json` assertions specifically) and **#6** (multi-playlist video — proves the central correctness point) from the plan.

## 9. Open Questions

1. **Should the merged media file path be stored in `metadata.json`?** Currently no — the media file is always `<ID>.<merge_output_format>` in the same folder, so the path is derivable. If a future PRD wants to support unusual layouts (e.g., relocated archive shards), adding `downloaded.media_path` would be the natural place. Defer.
2. **Should `metadata.json` track the on-disk media file's SHA-256 hash?** Plan defers explicitly. PRD 8 audit could compute it lazily. Out of scope here.
3. **What if a creator's slug changes in `config.toml`** (user renames a creator without renaming the directory)? `source_creator` in existing `metadata.json` files would mismatch. Currently unhandled. Probably the right answer is "don't do that" — but worth a thought.
4. **Should we record which playlists a video was *previously* in** within `metadata.json` itself (a `playlist_history` field) for queryability without parsing all the timestamped manifest snapshots? Currently no — the timestamped snapshots are the historical record. Add later if querying is painful.
5. **Should the trailing newline (§4.7 #24) be configurable?** No, just always emit one. Mentioned only to lock the convention.
