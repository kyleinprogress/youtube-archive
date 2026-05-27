# YouTube Archive — PRD Overview

This document decomposes [`youtube-archive-plan.md`](./youtube-archive-plan.md) into discrete PRDs that can be built and verified in sequence. Each PRD below lists its scope, dependencies on earlier PRDs, the key deliverables, and which verification items from the plan it owns.

The decomposition is designed so that:
- **Each PRD ships a usable slice.** After PRD 2 the tool can produce manifests; after PRD 4 it can archive videos end-to-end. Later PRDs add opt-in commands on top.
- **Cross-cutting concerns are placed in the earliest PRD that needs them.** Logging and error isolation belong in the foundation (PRD 1) because every pass relies on them; dry-run support lives in its own late PRD (PRD 7) because it must hook into every pass that already exists.
- **The central correctness point — playlist membership derived from Pass 1 manifests rather than per-video `info.json`** — is isolated into its own PRD (PRD 4) so it gets the attention it deserves.

---

## PRD 1 — Project Foundation & Config

**Scope:** Establish the skeleton everything else plugs into.

- `pyproject.toml` updates (name, description; confirm no runtime deps needed)
- `.gitignore` (excludes `data/`, `.venv/`, `__pycache__/`)
- `config.toml` schema + loader (`tomllib`, stdlib): `[defaults]` table, `[[creator]]` blocks with `slug`, `name`, `channel_url`, `exclude_playlists`, `extra_playlists`, and optional per-creator format overrides
- `archive.py` CLI skeleton with `argparse`: `--creator`, `--manifests-only`, `--refresh-metadata`, `--upgrade`, `--dry-run` (flags accepted; behavior wired in later PRDs)
- Per-creator directory creation (`data/<slug>/{videos,manifests/{playlists,channel},logs}`)
- Logging infrastructure: per-creator `download.log`, `manifests.log`, `errors.log` with subprocess output streaming (`subprocess.Popen` + thread/pipe wiring, stdlib only)
- Error-handling posture: single video failure does not abort creator; failing manifest aborts that creator's Pass 2 and moves to next creator
- Startup `ffmpeg` availability check (required for mkv merge + metadata embed)

**Dependencies:** none.

**Owns verification items:** #1 (ffmpeg check), #15 (failure isolation — the wiring; behavior gets exercised once later PRDs exist).

---

## PRD 2 — Manifest Discovery (Pass 1)

**Scope:** Read-only metadata pass — produce the canonical "what exists" snapshots.

- Discover channel playlists: `yt-dlp --flat-playlist --dump-single-json <channel>/playlists`
- Snapshot the discovery result to `manifests/channel/playlists_index_latest.json` and a timestamped copy
- Merge with `extra_playlists`, apply `exclude_playlists` filter to produce the final playlist set for the run
- Per-playlist manifests: `yt-dlp --flat-playlist --dump-single-json <playlist_url>` → `manifests/playlists/<PLAYLIST_ID>_latest.json` + timestamped copy
- Uploads manifest: `yt-dlp --flat-playlist --dump-single-json <channel>/videos` → `manifests/channel/uploads_latest.json` + timestamped copy
- Parse all manifests, return the unioned candidate video-ID set (consumed by PRD 3)
- `--manifests-only` flag fully functional after this PRD

**Dependencies:** PRD 1.

**Owns verification items:** #2 (playlist-discovery sanity check), #4 (manifest-only run), #7 (auto-discovery of new playlists / exclude behavior), #8 (`extra_playlists`).

---

## PRD 3 — Media Download Pipeline (Pass 2)

**Scope:** Turn the candidate ID set into archived media files. Does **not** yet write the normalized `metadata.json` (that's PRD 4).

- Filter candidate IDs against `archive.txt` (skip already-archived)
- Upload-age gate: read `timestamp` / `release_timestamp` from Pass 1 manifest entry; fall back to one `yt-dlp --skip-download --print "%(timestamp)s"` call if missing. If `(now - upload_time) < min_upload_age_hours`, log to `download.log` as gated and skip (do **not** add to `archive.txt`)
- Format resolution: merge `[defaults]` with per-creator overrides (`format`, `format_sort`, `merge_output_format`, `min_upload_age_hours`)
- Construct and invoke the full yt-dlp command per video (with `-f`, `-S` only if `format_sort` set, `--merge-output-format`, `--download-archive`, `--write-info-json`, `--write-thumbnail`, `--write-subs --write-auto-subs --sub-langs "en.*,en"`, `--convert-thumbnails webp`, `--embed-metadata --embed-thumbnail`, `-o "data/<creator>/videos/%(id)s/%(id)s.%(ext)s"`)
- Stream output to `download.log`; on failure log to `errors.log` and continue
- Default `uv run archive.py` invocation (no flags) runs Pass 1 + Pass 2 end-to-end

**Dependencies:** PRD 1, PRD 2.

**Owns verification items:** #3 (smoke run on a small creator — partial; the `metadata.json` assertions move to PRD 4), #11 (global default format selection), #12 (per-creator format override), #13 (upload-age gate).

---

## PRD 4 — Normalized Metadata Layer

**Scope:** After each successful Pass 2 download, write the normalized `metadata.json` with playlist memberships derived from the Pass 1 manifests — **not** from per-video `info.json`. This is the central correctness point of the whole design.

- Write `metadata.json` with the full schema documented in the plan: `video_id`, `source_creator`, `archived_at`, `last_metadata_check`, `availability`, `removed_detected_at`, `current` (title, thumbnail_url, description_hash), `downloaded` (format_id, height, vcodec, acodec, filesize), `upgrade_available: null`, `history: []`, `playlists: [{id, title, index}]`
- Build a `video_id → [playlist memberships]` lookup once per run from the Pass 1 manifests in memory
- On every run, **rewrite** the `playlists` field from current manifests (historical membership lives in timestamped manifest snapshots, not in `metadata.json`)
- Pull `current.title`, `current.thumbnail_url`, and the hashed description from the raw `info.json` yt-dlp wrote next to the media
- Populate `downloaded` from the format info in `info.json` (`format_id`, `height`, `vcodec`, `acodec`, `filesize`)

**Dependencies:** PRD 2 (manifest data), PRD 3 (download produces `info.json`).

**Owns verification items:** #3 (the `metadata.json` assertions specifically), #6 (multi-playlist video — proves the central correctness point).

---

## PRD 5 — Metadata Refresh & Upgrade Detection (Pass 3)

**Scope:** Opt-in `--refresh-metadata` command. Read-only on disk media; cheap enough to run weekly/monthly.

- For every video in `archive.txt`, call `yt-dlp --skip-download --dump-json <URL>`
- Diff against `metadata.json.current`. If `title`, `thumbnail_url`, or `description_hash` differ: append the previous `current` block to `history` with an `observed_at` timestamp, then overwrite `current`
- Availability tracking: on "Video unavailable" / "Private video" / similar errors, set `availability` to `"removed"` or `"private"`, set `removed_detected_at` to now (only if not already set), leave the media file untouched
- **Upgrade detection**: from the same `--dump-json` payload, simulate format selection using the creator's current `format` + `format_sort`. If the chosen format is "better" than `metadata.json.downloaded` per the same sort keys, populate `upgrade_available` with a descriptor of the better format. Otherwise set it to `null`
- Always update `last_metadata_check`
- `--refresh-metadata` skips Pass 1 and Pass 2 by default

**Dependencies:** PRD 4 (writes the structure this pass reads/mutates).

**Owns verification items:** #9 (title-change history), #10 (unavailable video flagged), #14 (upgrade detection — the detection half).

---

## PRD 6 — Upgrade Execution

**Scope:** Opt-in destructive `--upgrade` command. Re-downloads videos with non-null `upgrade_available`. Strictly separate from PRD 5 so frequent metadata refreshes never trigger re-downloads.

- Walk all `metadata.json` files; collect those with non-null `upgrade_available`
- For each: move existing media file to `<ID>.<ext>.pre-upgrade` in the same folder
- Re-download with the creator's current format settings
- On success: append `{type: "upgrade", observed_at, from: <old downloaded>, to: <new downloaded>}` to `history`, overwrite `downloaded`, set `upgrade_available: null`, delete the `.pre-upgrade` file
- On failure: restore the original file from `.pre-upgrade`, log to `errors.log`. The original is never lost
- Combinable with `--creator <slug>` to scope

**Dependencies:** PRD 3 (download mechanics), PRD 5 (populates `upgrade_available`).

**Owns verification items:** #14 (the execution half — `--upgrade` replaces file, history entry, no `.pre-upgrade` leftovers).

---

## PRD 7 — Dry-Run Mode

**Scope:** `--dry-run` support across every pass. Implemented last because it must hook into every command path the earlier PRDs created.

- Default mode: list candidate video IDs that *would* be downloaded; no files change
- With `--refresh-metadata`: list videos whose `metadata.json` *would* be updated and show the proposed diff
- With `--upgrade`: list videos that would be replaced and the new format that would be picked; no files moved
- Output goes to stdout (human-readable), nothing is written to disk and nothing is logged to per-creator log files

**Dependencies:** PRD 3, PRD 5, PRD 6 (must already exist to be dry-runnable).

**Owns verification items:** #5 (dry-run on a creator with new videos), and the `--dry-run` portions of #14.

---

## PRD 8 — Archive Consistency Audit

**Scope:** A read-only health check that cross-references `archive.txt` against on-disk state and flags discrepancies. Complements PRD 3's "`archive.txt` is the sole source of truth" posture by giving a safe way to detect when that source of truth has drifted from reality (e.g., a video folder was manually deleted, a disk-corruption event, or an interrupted run left an incomplete folder). Intended as a weekly/monthly maintenance command.

- New CLI flag `--audit` (added to PRD 1's flag list when this PRD is implemented). When passed, the script runs only the audit and exits — no Pass 1, no Pass 2, no metadata refresh, no downloads.
- For each creator (or scoped via `--creator`), read `archive.txt` and walk `data/<slug>/videos/`. Report four classes of discrepancy:
  - **Orphan in `archive.txt`**: ID is in `archive.txt` but `videos/<ID>/` doesn't exist (or its media file is missing). The normal run would skip it forever; this is what `--repair` targets.
  - **Orphan on disk**: `videos/<ID>/` exists but the ID is not in `archive.txt`. Likely a partial download from an interrupted run, or a manual addition.
  - **Incomplete folder**: `videos/<ID>/` exists with `<ID>.info.json` but no merged media file (or only a `.part` file). Indicates an interrupted download that yt-dlp's `--continue` should pick up next run.
  - **Missing sidecars** (only if PRD 4 has shipped): `videos/<ID>/` exists with media but no `metadata.json`.
- Output: human-readable summary to stdout + a structured report to `data/<slug>/logs/audit.log`. No files modified by default.
- Optional `--repair` flag (combinable with `--audit`): removes orphan-in-archive entries from `archive.txt` so the next normal run re-downloads them. Other discrepancies are surfaced but not auto-fixed in v1 (orphan-on-disk and missing-sidecars require user judgment).

**Dependencies:** PRD 3 (`archive.txt` and video folder layout). Audit checks for PRD 4 artifacts (`metadata.json`) are conditionally added once PRD 4 has shipped.

**Owns verification items:** none from the original plan (new feature). Verification will be specified in the PRD itself.

---

## Summary table

| PRD | Title | Depends on | Ships when done |
|-----|-------|------------|-----------------|
| 1 | Project Foundation & Config | — | Config loads; CLI parses flags; logging + dirs ready |
| 2 | Manifest Discovery (Pass 1) | 1 | `--manifests-only` works end-to-end |
| 3 | Media Download Pipeline (Pass 2) | 1, 2 | Default run archives media (no normalized metadata yet) |
| 4 | Normalized Metadata Layer | 2, 3 | `metadata.json` written with correct playlist memberships |
| 5 | Metadata Refresh & Upgrade Detection | 4 | `--refresh-metadata` works |
| 6 | Upgrade Execution | 3, 5 | `--upgrade` works |
| 7 | Dry-Run Mode | 3, 5, 6 | `--dry-run` works in every mode |
| 8 | Archive Consistency Audit | 3 (4 optional) | `--audit` reports archive.txt / on-disk drift; `--repair` prunes stale archive entries |
