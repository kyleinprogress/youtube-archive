## Relevant Files

- `archive.py` - All Pass 1 logic (canonical resolution, `creator.json` write, channel `/playlists` discovery, per-playlist fetches, uploads snapshot, candidate-set assembly, `--manifests-only` wiring) is added to the single-file orchestrator from PRD 1.
- `data/<slug>/creator.json` - Curated channel-level snapshot written each run. Schema is owned by this PRD; do not change without updating the documented fields.
- `data/<slug>/manifests/channel/playlists_index_latest.json` - Raw verbatim yt-dlp output of `<canonical_url>/playlists`. Overwritten each run.
- `data/<slug>/manifests/channel/playlists_index_<YYYY-MM-DD>.json` - Same-day timestamped copy of the playlists index. Same-day reruns overwrite.
- `data/<slug>/manifests/channel/uploads_latest.json` - Raw verbatim yt-dlp output of `<canonical_url>/videos`. Overwritten each run.
- `data/<slug>/manifests/channel/uploads_<YYYY-MM-DD>.json` - Same-day timestamped copy of the uploads tab.
- `data/<slug>/manifests/playlists/<PLAYLIST_ID>_latest.json` - One per playlist in the final set; raw yt-dlp output. Overwritten each successful run; never deleted when a playlist disappears.
- `data/<slug>/manifests/playlists/<PLAYLIST_ID>_<YYYY-MM-DD>.json` - Same-day timestamped copy of each per-playlist manifest.
- `data/<slug>/logs/manifests.log` - Pass 1 activity log (INFO progress lines, WARN per-playlist failures). Created empty by PRD 1; this PRD writes into it.
- `data/<slug>/logs/errors.log` - Full yt-dlp stderr on any per-playlist or channel-level failure. Created empty by PRD 1; this PRD appends to it.

### Notes

- Stdlib only — `subprocess` (via PRD 1's streaming helper, but capture stdout to a buffer here because we need the JSON), `json`, `urllib.parse` (for `list=` extraction), `pathlib`, `datetime`, `os.replace`.
- Use the PRD 1 helpers: `get_creator_loggers(slug)` for `manifests_log` + `errors_log`, and the `creator_scope(slug)` context manager so any uncaught exception is logged and the run moves to the next creator.
- yt-dlp is invoked as a subprocess; never `import yt_dlp`. Target version is `2026.03.17`. Do not layer custom retries — trust yt-dlp's built-in `--retries 10`.
- All raw manifest files (`playlists_index_*.json`, `uploads_*.json`, per-playlist `_latest`/`_dated`) are verbatim passthrough — do not parse, trim, or normalize before writing. Only `creator.json` follows our own schema.
- All JSON writes go through a tempfile + `os.replace()` so a partial file is never observable on disk.
- ISO 8601 UTC with `Z` suffix for `snapshot_taken_at` and log line timestamps; `%Y-%m-%d` (UTC) for snapshot filename suffixes.
- No automated test suite is in scope; verification is the success-metrics walkthrough captured in parent task 6.0.
- Run the script with `uv run archive.py --manifests-only` to exercise Pass 1 in isolation.

## Instructions for Completing Tasks

**IMPORTANT:** As you complete each task, you must check it off in this markdown file by changing `- [ ]` to `- [x]`. This helps track progress and ensures you don't skip any steps.

Example:
- `- [ ] 1.1 Read file` → `- [x] 1.1 Read file` (after completing)

Update the file after completing each sub-task, not just after completing an entire parent task.

## Tasks

- [ ] 0.0 Create feature branch
  - [ ] 0.1 Confirm the PRD 01 feature branch has been merged into `main` (or is otherwise reflected in `main`) before starting; resolve any outstanding state first
  - [ ] 0.2 Create and checkout a new branch: `git checkout -b feature/manifest-discovery`

- [ ] 1.0 Implement canonical channel URL resolution and `creator.json` snapshot
  - [ ] 1.1 Add a helper `resolve_canonical_channel(config_channel_url)` that runs `yt-dlp --skip-download --no-warnings --playlist-items 0 --dump-single-json "<config_channel_url>"`, captures stdout as bytes, and parses it as JSON
  - [ ] 1.2 Extract `channel_id`, `channel`, `uploader_id`, `description`, `channel_follower_count`, and `thumbnails` from the parsed response; build `canonical_url = f"https://www.youtube.com/channel/{channel_id}"`
  - [ ] 1.3 On subprocess non-zero exit, JSON parse error, or missing/empty `channel_id`, raise a tagged exception that the caller surfaces as a channel-level failure during the `canonical-resolution` phase (see 5.4)
  - [ ] 1.4 Implement `write_creator_json(slug, config_slug, config_channel_url, canonical_url, resolution_payload)` that assembles the documented schema: `config_slug`, `config_channel_url`, `canonical_url`, `channel_id`, `handle` (the `uploader_id`, including the `@`), `title` (the `channel` field), `description`, `subscriber_count` (from `channel_follower_count`), `avatar_url` (first entry in `thumbnails` by convention), `banner_url` (banner thumbnail entry; `null` if not present), `snapshot_taken_at`
  - [ ] 1.5 Allow `subscriber_count`, `avatar_url`, and `banner_url` to be `null` when the upstream response omits them; all other documented fields must be present
  - [ ] 1.6 Format `snapshot_taken_at` as `datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')`
  - [ ] 1.7 Write `data/<slug>/creator.json` atomically (write to `creator.json.tmp` in the same directory, then `os.replace()` onto `creator.json`)
  - [ ] 1.8 Before overwriting, if a prior `creator.json` exists, compare its `handle` against the new one; if they differ, emit exactly one `INFO` line to `manifests.log`: `handle renamed: <old> -> <new> (canonical URL unchanged)`. Always proceed with the overwrite — no on-disk history is kept in v1

- [ ] 2.0 Implement channel `/playlists` discovery snapshot and final playlist set computation
  - [ ] 2.1 Run `yt-dlp --flat-playlist --dump-single-json "<canonical_url>/playlists"`, capture stdout as bytes
  - [ ] 2.2 Write the raw bytes verbatim to `data/<slug>/manifests/channel/playlists_index_latest.json` using atomic tempfile + `os.replace()`
  - [ ] 2.3 Write the same raw bytes to `data/<slug>/manifests/channel/playlists_index_<YYYY-MM-DD>.json` where the date is the current UTC date; same-day reruns overwrite intentionally
  - [ ] 2.4 Parse the JSON and extract the list of playlist IDs from the `entries` array (each entry's `id` field — IDs that start with `PL`, `OL`, `LL`, etc. all flow through; do not filter by prefix)
  - [ ] 2.5 On non-zero exit, JSON parse error, or missing `entries` array, raise a tagged exception that the caller treats as a channel-level failure during the `playlists-discovery` phase (see 5.4)
  - [ ] 2.6 For each entry in the creator's `extra_playlists` config list, parse the URL with `urllib.parse.urlparse` and read the `list` query parameter via `parse_qs`; if absent, emit `WARN extra_playlists entry has no list= param, skipping: <url>` to `manifests.log` and skip
  - [ ] 2.7 Union the discovered set with the extracted `extra_playlists` IDs, deduplicating by playlist ID; on a duplicate, emit one `INFO extra_playlists entry already discovered: <id>` to `manifests.log`
  - [ ] 2.8 Apply the `exclude_playlists` config filter — remove any ID present in the creator's exclude list
  - [ ] 2.9 Emit `INFO final playlist set: N (discovered: D, extra: E, excluded: X)` to `manifests.log` where N is the final set size and D/E/X are the input counts before deduplication/exclusion
  - [ ] 2.10 Do **not** retroactively rewrite `playlists_index_<date>.json` to include `extra_playlists` IDs — that snapshot records what discovery returned, not the run's working set

- [ ] 3.0 Implement per-playlist manifest fetches and per-playlist failure isolation
  - [ ] 3.1 For each playlist ID in the final set, run `yt-dlp --flat-playlist --dump-single-json "https://www.youtube.com/playlist?list=<PLAYLIST_ID>"` and capture stdout as bytes
  - [ ] 3.2 Atomically write the raw bytes verbatim to `data/<slug>/manifests/playlists/<PLAYLIST_ID>_latest.json` and `data/<slug>/manifests/playlists/<PLAYLIST_ID>_<YYYY-MM-DD>.json`
  - [ ] 3.3 Parse the JSON; capture the playlist's `title` and iterate `entries`, collecting each entry's `id` (video ID) and `index` (1-based position in the playlist)
  - [ ] 3.4 Maintain an in-memory `playlist_membership: dict[str, list[dict]]` keyed by video ID; for each video, append `{"id": playlist_id, "title": playlist_title, "index": index}` to its list
  - [ ] 3.5 Append each successfully-fetched playlist's video IDs to a per-creator `candidate_video_ids` list, deduplicating but preserving first-seen order across all manifest sources
  - [ ] 3.6 On per-playlist failure (subprocess non-zero exit OR JSON parse error OR missing `entries`): emit `WARN playlist fetch failed: <id> — <one-line error>` to `manifests.log` AND `ERROR playlist <id>: <full yt-dlp stderr>` to `errors.log`; **do not** overwrite `<PLAYLIST_ID>_latest.json` or write `<PLAYLIST_ID>_<date>.json`; previous run's files remain on disk untouched; continue to the next playlist; do not set `channel_level_failed`
  - [ ] 3.7 Treat empty playlists (zero `entries`) as success — write the manifest, contribute zero video IDs, emit the normal `INFO playlist fetched: <id> (0 videos)` line
  - [ ] 3.8 Emit one `INFO playlist fetched: <id> (N videos)` line to `manifests.log` per successful playlist

- [ ] 4.0 Implement channel uploads tab snapshot and candidate video-ID set assembly
  - [ ] 4.1 After per-playlist fetches complete, run `yt-dlp --flat-playlist --dump-single-json "<canonical_url>/videos"` and capture stdout as bytes
  - [ ] 4.2 Atomically write the raw bytes verbatim to `data/<slug>/manifests/channel/uploads_latest.json` and `data/<slug>/manifests/channel/uploads_<YYYY-MM-DD>.json`
  - [ ] 4.3 Parse the JSON and append every entry's `id` (video ID) to `candidate_video_ids`, preserving first-seen order and deduplicating against IDs already added from per-playlist manifests
  - [ ] 4.4 On uploads-tab failure (non-zero exit / parse error / missing `entries`), raise a tagged exception that the caller treats as a channel-level failure during the `uploads` phase (see 5.4); the partial candidate set is still returned with `channel_level_failed = True`
  - [ ] 4.5 Ensure videos that appeared only in the uploads tab have an empty list (`[]`) in `playlist_membership` so PRD 4 can rely on every candidate ID being keyed in that dict
  - [ ] 4.6 Emit `INFO uploads manifest fetched: N videos` to `manifests.log`
  - [ ] 4.7 Assemble and return the per-creator structure exactly as documented in PRD §4.7: `{"slug": ..., "canonical_url": ..., "candidate_video_ids": [...], "channel_level_failed": bool, "playlist_membership": {...}}`

- [ ] 5.0 Wire `--manifests-only` mode, Pass 1 logging, and channel-level failure routing
  - [ ] 5.1 Implement `run_pass_one(creator)` that performs the per-creator sequence (1.0 → 4.0) inside the PRD 1 `creator_scope(slug)` context manager
  - [ ] 5.2 Emit `INFO Pass 1 starting` at the top of each creator's Pass 1 and `INFO canonical URL resolved: <canonical_url>` immediately after step 1.2 succeeds
  - [ ] 5.3 Emit `INFO playlists discovered: N` after §4.3 completes and `INFO Pass 1 complete: M candidate video IDs` at the end of a creator's Pass 1
  - [ ] 5.4 On any channel-level failure (canonical-resolution, playlists-discovery, or uploads): emit a single `ERROR` line to BOTH `manifests.log` AND `errors.log` naming the failure phase and the captured yt-dlp stderr; set `channel_level_failed = True`; print one stderr line: `error: <slug>: channel-level failure during <phase> — see data/<slug>/logs/errors.log`; skip remaining steps that depend on the failed phase per PRD §4.9 (canonical fails → skip everything including `creator.json`; playlists-discovery fails → still attempt `extra_playlists` per-playlist fetches and the uploads tab; uploads fails → keep whatever per-playlist data succeeded); continue to the next creator
  - [ ] 5.5 Verify `creator_scope` catches any unexpected `Exception` inside Pass 1, routes it like a channel-level failure (log + mark failed + next creator), and that `KeyboardInterrupt` / `SystemExit` still abort the whole run
  - [ ] 5.6 When `--manifests-only` is passed: after Pass 1 completes for every selected creator, exit with code `0` without invoking Pass 2 — even if some creators had channel-level failures
  - [ ] 5.7 When `--manifests-only` is **not** passed: after Pass 1 finishes for each creator, hand the candidate-set structure to a Pass 2 entry point (a stub function that no-ops for now; PRD 3 fills in the real behavior). PRD 3 will check `channel_level_failed` and refuse downloads for that creator if it's `True`

- [ ] 6.0 Verify against PRD success metrics
  - [ ] 6.1 Canonical resolution: point one creator at each of the four URL forms in turn (`@handle`, `/channel/UC...`, `/c/custom`, `/user/legacy`) — all four runs produce an identical `canonical_url` and `channel_id` in `creator.json`
  - [ ] 6.2 `python -m json.tool data/<slug>/creator.json` parses cleanly and every documented required field is present (allowing `subscriber_count`, `avatar_url`, `banner_url` to be `null`)
  - [ ] 6.3 `data/<slug>/manifests/channel/playlists_index_latest.json` exists and lists every playlist visible on `https://www.youtube.com/@<creator>/playlists`; a same-day `playlists_index_<date>.json` exists alongside
  - [ ] 6.4 `ls data/<slug>/manifests/playlists/` shows two files per playlist (`<ID>_latest.json` + `<ID>_<date>.json`); spot-check that the JSON parses and contains an `entries` array
  - [ ] 6.5 `data/<slug>/manifests/channel/uploads_latest.json` exists, and a same-day `uploads_<date>.json` exists alongside
  - [ ] 6.6 `extra_playlists` works — add an arbitrary public YouTube playlist URL (even from another channel) to the creator's `extra_playlists`, rerun, and confirm the playlist's manifest appears under `manifests/playlists/`
  - [ ] 6.7 `exclude_playlists` works — add a playlist ID to `exclude_playlists`, rerun, and confirm a new same-day manifest is NOT written for that playlist; the previous run's `_latest.json` and any prior dated copies remain on disk
  - [ ] 6.8 `uv run archive.py --manifests-only` produces no files under `data/<slug>/videos/` and does not create or append to `data/<slug>/archive.txt`
  - [ ] 6.9 Per-playlist failure isolation — temporarily change one `extra_playlists` entry to a known-deleted playlist ID, rerun, and confirm: `manifests.log` has a `WARN`, `errors.log` has an `ERROR`, the other playlists still get their manifests written, the candidate set is still returned, and the run exits `0`
  - [ ] 6.10 Channel-level failure isolation — temporarily set one creator's `channel_url` to a known-bad URL, rerun, and confirm: `errors.log` has an `ERROR` for that creator, no manifest files are written for that creator, no `creator.json` is created, the next creator in the config still processes normally, and the run exits `0`
  - [ ] 6.11 Handle rename detection — manually edit `data/<slug>/creator.json`'s `handle` field, rerun with `--manifests-only`, and confirm `manifests.log` contains the documented `handle renamed: <old> -> <new> (canonical URL unchanged)` `INFO` line and `creator.json` is overwritten with the correct handle
  - [ ] 6.12 Empty channel playlist list — point a creator at a channel with no public playlists, rerun, and confirm the run completes successfully, `playlists_index_latest.json` is written with an empty `entries` array, and the uploads manifest is still produced
