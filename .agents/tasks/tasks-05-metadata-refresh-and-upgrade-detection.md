## Relevant Files

- `archive.py` - `--refresh-metadata` code path (target enumeration, per-video yt-dlp refresh call, metadata-drift detection, availability tracking, upgrade detection, atomic writes, per-creator + grand-total console summary) is added to the single-file orchestrator from PRDs 1–4.
- `data/<slug>/videos/<ID>/metadata.json` - PRD 5 mutates `current.*`, `last_metadata_check`, `availability`, `removed_detected_at`, `upgrade_available`, and appends to `history`. Other fields (`playlists`, `downloaded.*`, `archived_at`, `schema_version`, `video_id`, `source_creator`) MUST stay byte-identical.
- `data/<slug>/archive.txt` - PRD 5 reads this to enumerate the work list. Never written.
- `data/<slug>/logs/refresh.log` - **New per-creator log file** introduced by this PRD. Created empty by PRD 1's helper (extended here to expose a fourth log file).
- `data/<slug>/logs/errors.log` - Transient errors (network blips, parse failures) get a one-line `ERROR refresh failed: <ID> — <last 5 lines of stderr>` here.

### Notes

- Stdlib only: `subprocess`, `json`, `hashlib`, `datetime`, `pathlib`.
- Reuse PRD 1's `get_creator_loggers(slug)` (now extended to return a fourth logger for `refresh.log`), `creator_scope(slug)`, and the subprocess streaming helper (but capture stdout into a buffer here — `--dump-json` emits a single large JSON document that the parser needs in one piece).
- Reuse PRD 4's `write_json_atomic(path, obj)` and `description_hash(description)` helpers.
- Reuse PRD 3 §4.5's format-resolution logic (per-creator overrides shadowing `[defaults]`) — refactor into a shared helper if PRD 3 didn't already.
- yt-dlp is invoked as a subprocess; never `import yt_dlp`. Target version `2026.03.17`. Trust yt-dlp's built-in `--retries 10`; do not layer additional retries. Consider a per-video `timeout=60` on `subprocess.run` to prevent a hung yt-dlp from blocking the entire refresh.
- Refresh calls are strictly **serial** — one yt-dlp invocation completes before the next starts. Matches PRD 3's rationale.
- PRD 5 does NOT consult `channel_level_failed` from PRD 2 — it doesn't run Pass 1 in the first place. Each video's refresh stands alone.
- No automated test suite is in scope; verification is the success-metrics walkthrough in parent task 6.0.

## Instructions for Completing Tasks

**IMPORTANT:** As you complete each task, you must check it off in this markdown file by changing `- [ ]` to `- [x]`. This helps track progress and ensures you don't skip any steps.

Example:
- `- [ ] 1.1 Read file` → `- [x] 1.1 Read file` (after completing)

Update the file after completing each sub-task, not just after completing an entire parent task.

## Tasks

- [x] 0.0 Create feature branch
  - [x] 0.1 Confirm the PRD 04 feature branch has been merged into `main` (or is otherwise reflected there); resolve any outstanding state first
  - [x] 0.2 Create and checkout a new branch: `git checkout -b feature/metadata-refresh-and-upgrade-detection`

- [x] 1.0 Wire `--refresh-metadata` as a standalone code path and add the fourth per-creator log file
  - [x] 1.1 Extend PRD 1's `get_creator_loggers(slug)` to return a fourth logger backed by `data/<slug>/logs/refresh.log`; also extend PRD 1's directory-setup step to touch `refresh.log` so it exists as an empty file after the first PRD 5 run
  - [x] 1.2 Add `--refresh-metadata` handling to the main CLI dispatcher so it skips Pass 1 (PRD 2), Pass 2 (PRD 3), and PRD 4 entirely — no manifest discovery, no downloads, no playlist refresh
  - [x] 1.3 Allow composition with `--creator <slug>` to scope the refresh to one creator; without `--creator`, iterate all creators in `config.toml` order
  - [x] 1.4 Implement `run_refresh(creator)` invoked inside `creator_scope(slug)` so unexpected exceptions log to `errors.log` and the run moves to the next creator
  - [x] 1.5 At the top of each creator's refresh, emit `INFO refresh starting (<N> videos in archive.txt)` to `refresh.log` where `<N>` is the count of IDs parsed from `archive.txt`

- [x] 2.0 Implement the per-video yt-dlp refresh call and outcome classification
  - [x] 2.1 Read `data/<slug>/archive.txt` once into a list of video IDs (in archive.txt order; no sort) and walk them serially
  - [x] 2.2 Resolve the effective per-creator format settings (`format`, `format_sort`) using the same precedence as PRD 3 §4.5; reuse the helper
  - [x] 2.3 For each ID, invoke `yt-dlp --skip-download --no-warnings -f "<format>" [-S "<format_sort joined by commas>"] --dump-json "https://www.youtube.com/watch?v=<ID>"`; omit `-S` entirely when the resolved `format_sort` is empty
  - [x] 2.4 Capture stdout + stderr via `subprocess.run(..., capture_output=True, text=True, timeout=60)`; treat `TimeoutExpired` as a transient error
  - [x] 2.5 Classify the outcome based on exit code and stderr substring matching: **available** (exit 0 + parseable JSON); **removed** (non-zero + stderr contains `"Video unavailable"`, `"This video has been removed"`, `"This video is no longer available"`, or `"has been terminated"`); **private** (non-zero + `"Private video"` or `"This video is private"`); **members_only** (non-zero + `"members-only"`, `"members only"`, or `"requires a Channel Membership"`); **age_restricted** (non-zero + `"Sign in to confirm your age"` or `"age-restricted"`); **transient_error** (non-zero + none of the above, OR exit 0 with unparseable JSON)
  - [x] 2.6 Handle orphan / corruption cases before invocation: if `data/<slug>/videos/<ID>/` does not exist, skip silently (no log line); if the folder exists but `metadata.json` is missing, log `WARN refresh skipped: <ID> (no metadata.json; run a normal sync first)` to `refresh.log` and skip
  - [x] 2.7 On `transient_error`: log `ERROR refresh failed: <ID> — <last 5 lines of stderr>` to `errors.log`; log `WARN refresh transient error: <ID>` to `refresh.log`; leave `availability`, `removed_detected_at`, `current.*`, `upgrade_available`, AND `last_metadata_check` ALL unchanged; continue to the next video
  - [x] 2.8 The media file on disk MUST NOT be touched in any availability-change scenario — the archive's purpose is to preserve files YouTube no longer serves

- [x] 3.0 Implement metadata-drift detection and `history` writes
  - [x] 3.1 On a successful (available) refresh, compute the candidate new `current` block: `title` from parsed JSON's `title`, `thumbnail_url` from parsed JSON's `thumbnail` (top-level), `description_hash` via the existing PRD 4 `description_hash(...)` helper applied to parsed JSON's `description`
  - [x] 3.2 Compute `changed_fields` as the alphabetically-sorted subset of `{"title", "thumbnail_url", "description_hash"}` whose new value differs from the existing `metadata.json.current` value
  - [x] 3.3 If `changed_fields` is non-empty, append ONE entry to `metadata.json.history` with the documented schema: `{"type": "metadata_change", "previous_observed_until": <existing last_metadata_check>, "changed_fields": [...], "previous": {"title": ..., "thumbnail_url": ..., "description_hash": ...}}`. `previous` carries the FULL prior `current` block even when only one field changed
  - [x] 3.4 Replace `metadata.json.current` with the new values (all three fields together)
  - [x] 3.5 Bundle correlated changes: if multiple fields changed in the same refresh, emit ONE history entry listing all of them in `changed_fields` — NOT one entry per field
  - [x] 3.6 If `changed_fields` is empty (nothing changed), do NOT append a history entry and do NOT modify `current`
  - [x] 3.7 Log `INFO metadata_change: <ID> (changed: <comma-joined field names>)` to `refresh.log` only when `changed_fields` is non-empty
  - [x] 3.8 Skip §3.0 entirely when the new availability state is anything other than `"available"` (yt-dlp gave us no payload to diff against)

- [x] 4.0 Implement availability tracking
  - [x] 4.1 Capture the prior `availability` value from the existing `metadata.json` before applying changes
  - [x] 4.2 Set new `availability` to the classified value from 2.5 (one of `available`, `removed`, `private`, `members_only`, `age_restricted`)
  - [x] 4.3 If new state is NOT `"available"` AND `removed_detected_at` is currently `null`, set `removed_detected_at` to the current UTC ISO 8601 timestamp; if it's already populated, leave it (preserves the original detection time)
  - [x] 4.4 If new state IS `"available"` AND prior state was not `"available"` (a resurrection), set `removed_detected_at` back to `null`
  - [x] 4.5 If new state differs from prior state, append one entry to `history`: `{"type": "availability_change", "previous_observed_until": <existing last_metadata_check>, "previous": <prior availability value>}`
  - [x] 4.6 Log `INFO availability_change: <ID> (<prior> -> <new>)` to `refresh.log` only on a real transition

- [x] 5.0 Implement upgrade detection, last_metadata_check, atomic writes, and console summary
  - [x] 5.1 When availability is `"available"`, read yt-dlp's selected format from the parsed JSON's top-level fields (`format_id`, `height`, `vcodec`, `acodec`, `filesize` with `filesize_approx` fallback) — these reflect the `-f` / `-S` selection that was applied on the command line
  - [x] 5.2 Compute the upgrade rule: an upgrade is available if `parsed.format_id != metadata.downloaded.format_id`. If so, populate `upgrade_available = {"detected_at": <now UTC ISO>, "format_id": ..., "height": ..., "vcodec": ..., "acodec": ..., "filesize": ...}`; always overwrite the prior `upgrade_available` value
  - [x] 5.3 If `parsed.format_id == metadata.downloaded.format_id`, set `upgrade_available = null` (this clears any stale upgrade flag that's no longer applicable)
  - [x] 5.4 Log `INFO upgrade_available: <ID> (height <old> -> <new>)` to `refresh.log` only on a transition from `null` to populated; log `INFO upgrade_cleared: <ID>` only on a transition from populated to `null`
  - [x] 5.5 Do NOT append a history entry for upgrade-detection state changes — `upgrade_available` is a current-state pointer; PRD 6 writes the `{type: "upgrade", ...}` history entry when an upgrade is actually executed
  - [x] 5.6 Update `last_metadata_check` to `datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')` on every **successful** refresh (any of the real-state classifications). MUST NOT update on a transient_error — `last_metadata_check` semantically means "last time we successfully observed this video's state"
  - [x] 5.7 Write `metadata.json` via PRD 4's `write_json_atomic(...)`; preserve the documented field order; preserve `playlists`, `downloaded.*`, `archived_at`, `schema_version`, `video_id`, `source_creator` byte-identically; new `history` entry's field order is `type`, then the timestamp field, then payload fields
  - [x] 5.8 At the end of each creator's refresh, emit `INFO refresh complete: <C> checked, <M> metadata changes, <A> availability changes, <U> upgrades available, <X> transient errors, <S> skipped` to `refresh.log`
  - [x] 5.9 Print one console line per creator: `<slug>: refresh — <C> checked, <M> metadata changes, <A> availability changes, <U> upgrades available, <X> transient errors, <S> skipped`. Always print, even on zero work
  - [x] 5.10 After all creators complete, print one final summary line: `Total: <M> metadata changes, <A> availability changes, <U> upgrades available, <X> transient errors across <N> creators` (where `<U>` is the cross-creator total of videos currently in the upgrade_available state, not just newly-flagged). If `--creator` was used, `<N>` is `1`

- [ ] 6.0 Verify against PRD success metrics
  - [ ] 6.1 Title-change history: edit one video's `metadata.json.current.title` to a wrong value (e.g., `"WRONG"`), run `--refresh-metadata`, confirm `current.title` is restored to YouTube's actual title, one new `metadata_change` entry exists in `history` with `changed_fields: ["title"]` and `previous.title == "WRONG"`, and `last_metadata_check` is updated
  - [ ] 6.2 No-op refresh: run `--refresh-metadata` twice in a row with no YouTube changes between runs. Confirm zero new history entries, `last_metadata_check` updated on both runs, no per-video lines in `refresh.log` (only start/end phase lines)
  - [ ] 6.3 Multi-field change in one refresh: hand-edit `current.title` AND `current.thumbnail_url` to wrong values; run refresh; confirm a SINGLE history entry with `changed_fields: ["thumbnail_url", "title"]` (alphabetically sorted), not two entries
  - [ ] 6.4 Unavailable video flagged: add `youtube <known-deleted-ID>` to `archive.txt`, create a placeholder folder with a minimal valid `metadata.json` (PRD 4 can generate it); run `--refresh-metadata`; confirm `availability` flips to `"removed"`, `removed_detected_at` is populated, one `availability_change` entry in `history` with `previous: "available"`, and the placeholder folder is NOT deleted
  - [ ] 6.5 Resurrection detection: hand-edit a video's `metadata.json` to `availability: "removed"` and `removed_detected_at: "2025-01-01T00:00:00Z"`; run refresh against the actually-available video; confirm `availability` flips back to `"available"`, `removed_detected_at` becomes `null`, one `availability_change` entry with `previous: "removed"`
  - [ ] 6.6 Upgrade detection — newly-flagged: hand-edit one video's `downloaded` to a deliberately worse format (e.g., `height: 360`, `vcodec: "avc1.42001E"`, `format_id: "18"`); run refresh; confirm `upgrade_available` is populated with a non-360p descriptor and one `INFO upgrade_available` line in `refresh.log`
  - [ ] 6.7 Upgrade detection — newly-cleared: starting from #6.6's state, hand-edit `metadata.json.downloaded` to match what `upgrade_available` currently points at; run refresh; confirm `upgrade_available` is now `null` and a `INFO upgrade_cleared` line appears
  - [ ] 6.8 Upgrade detection — config-driven: with `downloaded.height == 1080`, change the creator's `format_sort` in `config.toml` to `["res:2160"]`; run refresh; if a 4K version exists on YouTube, confirm `upgrade_available` is populated with `height: 2160`
  - [ ] 6.9 Transient error doesn't corrupt state: temporarily block network access (or use a `subprocess` mock) and run refresh; confirm every video gets `WARN refresh transient error`, `availability`/`current.*`/`upgrade_available`/`last_metadata_check` are ALL unchanged for every affected video, `errors.log` has the per-video `ERROR refresh failed` entries, the script exits cleanly with code 0
  - [ ] 6.10 Console summary: for a creator with 10 videos, 2 title changes, 1 newly-detected upgrade, confirm the per-creator line reads `<slug>: refresh — 10 checked, 2 metadata changes, 0 availability changes, 1 upgrades available, 0 transient errors, 0 skipped`
  - [ ] 6.11 Orphan silence: add a fake line `youtube zzz999fakeID` to `archive.txt`; run refresh; confirm zero console output and zero log lines mention the fake ID; the skip count in the summary increments by 1
  - [ ] 6.12 Missing-metadata warning: for a real archived video, delete its `metadata.json` (keep media); run refresh; confirm one `WARN refresh skipped: <ID> (no metadata.json; run a normal sync first)` line and the skip count increments
  - [ ] 6.13 Default invocation doesn't trigger PRD 5: run `uv run archive.py` (no flags) and confirm `refresh.log` mtime is unchanged
  - [ ] 6.14 `--creator` scoping: run `--refresh-metadata --creator <slug>` with multiple creators in config; confirm only the named creator's videos are refreshed (mtime check on other creators' `metadata.json` files)
  - [ ] 6.15 Media files never touched: compute sha256 hashes of every media file under one creator's `videos/` directory before and after a refresh of at least 5 videos; confirm every hash is identical
