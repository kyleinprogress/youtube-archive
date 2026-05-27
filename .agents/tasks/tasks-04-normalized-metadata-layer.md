## Relevant Files

- `archive.py` - PRD 4 logic (metadata.json schema, first-creation path, re-run path, orphan/corruption/rebuild handling, atomic write helper, Pass-4 phase logging) is added to the single-file orchestrator from PRDs 1–3.
- `data/<slug>/videos/<ID>/metadata.json` - The curated, normalized metadata file written by this PRD. Schema is owned here; PRDs 5 and 6 mutate specific sub-fields but the shape and field order live in this PRD.
- `data/<slug>/videos/<ID>/<ID>.info.json` - Raw yt-dlp metadata read at first creation; produced by PRD 3 §4.6. PRD 4 reads this only on first creation and treats `metadata.json` as the source of truth on subsequent runs.
- `data/<slug>/archive.txt` - PRD 4 reads this once at the top of its pass to enumerate work. Never written by PRD 4.
- `data/<slug>/logs/download.log` - Pass-4 phase markers and per-video INFO/WARN lines append here (same log file PRD 3 uses, by design).

### Notes

- Stdlib only: `json`, `hashlib`, `pathlib`, `datetime`, `os` (`os.replace`, `os.path.getsize`).
- Reuse PRD 1's `get_creator_loggers(slug)` (for `download_log`) and `creator_scope(slug)` (for the per-creator exception boundary).
- The in-memory `playlist_membership` dict returned by PRD 2 §4.7 #28 is the source for the `playlists` field. PRD 4 must NOT re-parse manifest JSON from disk.
- Field order in the emitted JSON MUST match the schema example in PRD 4 §4.2 #5 exactly so `git diff` / `diff -u` produce meaningful diffs across runs. Use `json.dumps(..., indent=2, ensure_ascii=False, sort_keys=False)` and build the dict in the documented order.
- All writes go through a single `write_json_atomic(path, obj)` helper (tempfile sibling + `os.replace`). Factor this so PRDs 2 and 5/6 can also call it.
- PRD 4 never touches `current.*`, `last_metadata_check` (after first creation), `availability`, `removed_detected_at`, `upgrade_available`, or `history` on a re-run. Those are PRD 5's territory.
- PRD 4 does not invoke yt-dlp. No network calls.
- No automated test suite is in scope; verification is the success-metrics walkthrough in parent task 6.0.

## Instructions for Completing Tasks

**IMPORTANT:** As you complete each task, you must check it off in this markdown file by changing `- [ ]` to `- [x]`. This helps track progress and ensures you don't skip any steps.

Example:
- `- [ ] 1.1 Read file` → `- [x] 1.1 Read file` (after completing)

Update the file after completing each sub-task, not just after completing an entire parent task.

## Tasks

- [ ] 0.0 Create feature branch
  - [ ] 0.1 Confirm the PRD 03 feature branch has been merged into `main` (or is otherwise reflected there); resolve any outstanding state first
  - [ ] 0.2 Create and checkout a new branch: `git checkout -b feature/normalized-metadata-layer`

- [ ] 1.0 Wire PRD 4 into the default run flow
  - [ ] 1.1 Implement `run_pass_four(creator, candidate_set)` and invoke it after `run_pass_two` completes for each creator in the default-invocation path
  - [ ] 1.2 Skip PRD 4 entirely for any creator whose Pass 1 set `channel_level_failed = True`; existing `metadata.json` files for that creator remain untouched on disk
  - [ ] 1.3 Skip PRD 4 entirely when any of `--manifests-only`, `--refresh-metadata`, `--upgrade`, or `--audit` is set on the CLI (PRD 5 / 6 / 8 manage their own writes; PRD 4 only runs in the default flow)
  - [ ] 1.4 Run PRD 4 even when PRD 3 downloaded zero new videos this run — playlist memberships for already-archived videos still need to stay in sync with the latest manifests
  - [ ] 1.5 Wrap the per-creator PRD 4 work inside `creator_scope(slug)` from PRD 1; any uncaught exception is logged to `errors.log` and processing continues with the next creator

- [ ] 2.0 Implement the metadata.json schema, atomic write helper, and description hash
  - [ ] 2.1 Define a module-level `write_json_atomic(path, obj)` helper: write `json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False)` to `<path>.tmp` followed by a trailing `\n`, then `os.replace()` onto the final path
  - [ ] 2.2 Refactor PRD 2's atomic `creator.json` write to call `write_json_atomic` (per PRD 4 §7's DRY note) so all curated-JSON writes go through a single code path
  - [ ] 2.3 Implement `description_hash(description)` returning `"sha256:" + hashlib.sha256(description.encode("utf-8")).hexdigest()`
  - [ ] 2.4 When `info.json.description` is `null` or absent, hash empty bytes (`b""`) so the result is the well-known constant `"sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"`
  - [ ] 2.5 Do NOT normalize whitespace, line endings, or casing in `description` before hashing — raw UTF-8 bytes only
  - [ ] 2.6 Build a `build_metadata_dict(...)` helper that returns the schema dict in the documented field order: `schema_version`, `video_id`, `source_creator`, `archived_at`, `last_metadata_check`, `availability`, `removed_detected_at`, `current` (then `title`, `thumbnail_url`, `description_hash`), `downloaded` (then `format_id`, `height`, `vcodec`, `acodec`, `filesize`), `upgrade_available`, `history`, `playlists`

- [ ] 3.0 Implement the first-creation path
  - [ ] 3.1 Read `data/<slug>/archive.txt` once into an in-memory list of video IDs by parsing each `youtube <VIDEO_ID>` line; tolerate empty lines and `#` comments
  - [ ] 3.2 For each ID whose `data/<slug>/videos/<ID>/metadata.json` does NOT exist, attempt the first-creation path; first verify the merged media file exists (e.g., `<ID>.<resolved merge_output_format>`); if missing, follow the missing-media path (4.4) and skip
  - [ ] 3.3 Verify `<ID>.info.json` exists alongside the media file; if missing, follow the corruption path (4.4) and skip
  - [ ] 3.4 Read and parse `info.json` via `json.load`
  - [ ] 3.5 Build `current.title` from `info.json`'s top-level `title`; `current.thumbnail_url` from `info.json`'s top-level `thumbnail` (NOT an entry in the `thumbnails` array); `current.description_hash` via `description_hash(info_json.get("description"))`
  - [ ] 3.6 Build `downloaded.format_id`, `height`, `vcodec` (use `"none"` if audio-only), `acodec` (use `"none"` if video-only) from `info.json`; if `height` is missing/null, store `null`
  - [ ] 3.7 Compute `downloaded.filesize` via the three-step fallback: `info.json["filesize"]` → `info.json["filesize_approx"]` → `os.path.getsize(<media_file>)` (the final fallback always succeeds because media presence was verified in 3.2)
  - [ ] 3.8 Set `archived_at` and `last_metadata_check` both to `datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')` on first creation; set `availability = "available"`, `removed_detected_at = null`, `upgrade_available = null`, `history = []`, `schema_version = 1`, `video_id = <ID>`, `source_creator = <slug>`
  - [ ] 3.9 Set `playlists` to the membership array for this video ID from PRD 2's `candidate_set["playlist_membership"]` lookup; empty list `[]` if the ID is not keyed in that dict
  - [ ] 3.10 Call `write_json_atomic(<folder>/metadata.json, built_dict)` and log `INFO metadata.json created: <VIDEO_ID>` to `download.log`

- [ ] 4.0 Implement the re-run path and orphan / corruption / rebuild handling
  - [ ] 4.1 For each ID whose `metadata.json` already exists, read the existing file via `json.load`, replace ONLY the `playlists` field with the freshly-computed membership array from PRD 2, leave every other field byte-identical, and call `write_json_atomic` to rewrite
  - [ ] 4.2 If the new `playlists` array differs from the existing one (different IDs, different indices, or different order), log `INFO metadata.json playlists updated: <VIDEO_ID> (N entries)` to `download.log`; if identical, write the file anyway but log nothing
  - [ ] 4.3 Orphan path: if `archive.txt` contains a video ID but `data/<slug>/videos/<ID>/` does NOT exist, skip the ID **silently** — no log line, no error (PRD 8 `--audit` is the proper surfacing mechanism)
  - [ ] 4.4 Corruption path: if the folder exists but BOTH `info.json` and `metadata.json` are missing, log one `WARN metadata.json skipped: <VIDEO_ID> (info.json missing — folder corruption?)` to `download.log` and continue
  - [ ] 4.5 Missing-media path: if the folder exists but the canonical media file is missing (whether or not `info.json` exists), log one `WARN metadata.json skipped: <VIDEO_ID> (media file missing)` to `download.log` and skip writing
  - [ ] 4.6 Rebuild path: if `info.json` exists but `metadata.json` is missing (e.g., user manually deleted it), follow the first-creation path (3.0) but log `INFO metadata.json rebuilt: <VIDEO_ID> (was missing)` to `download.log` instead of "created"
  - [ ] 4.7 Never delete `metadata.json` files for IDs no longer in `archive.txt` — leave stale files harmlessly on disk

- [ ] 5.0 Implement PRD 4 logging and phase markers
  - [ ] 5.1 At the start of each creator's PRD 4 pass, emit `INFO PRD 4 metadata pass starting` to `download.log`
  - [ ] 5.2 Maintain per-creator counters: `<C>` created, `<R>` rebuilt, `<U>` playlists-updated, `<S>` skipped (orphan + missing-media + corruption combined)
  - [ ] 5.3 At the end of each creator's PRD 4 pass, emit `INFO PRD 4 metadata pass complete: <C> created, <R> rebuilt, <U> playlists-updated, <S> skipped` to `download.log`
  - [ ] 5.4 Do NOT write to `manifests.log`. Do NOT write to `errors.log` for the documented WARN/skip cases (those are warnings, not errors); only `creator_scope` writes errors.log for uncaught exceptions
  - [ ] 5.5 Console output for PRD 4 is silent — no new lines beyond what PRD 3 already prints; PRD 4 work is folded into the same per-creator console block

- [ ] 6.0 Verify against PRD success metrics
  - [ ] 6.1 First-creation produces a complete file: after a fresh `uv run archive.py --creator <slug>` against a 5-video playlist, every `data/<slug>/videos/<ID>/metadata.json` parses cleanly via `python -m json.tool` and every documented required field is present with the documented type
  - [ ] 6.2 Multi-playlist correctness: for a video known to appear in 2 of the creator's playlists, exactly one folder exists and its `metadata.json.playlists` array has 2 entries with the correct `id`, `title`, `index` for each
  - [ ] 6.3 Single-playlist video: a video in only 1 playlist has 1 entry in `playlists`
  - [ ] 6.4 Uploads-only video: a video appearing only in the uploads manifest (no playlists) has `playlists: []`
  - [ ] 6.5 `description_hash` format: an arbitrary archived video's `current.description_hash` matches the regex `^sha256:[0-9a-f]{64}$`; independently recomputing `sha256(info.json["description"].encode("utf-8")).hexdigest()` matches the stored value
  - [ ] 6.6 Idempotent re-runs: a second `uv run archive.py --creator <slug>` immediately after the first leaves every `metadata.json` byte-identical to before (mtime updates, content does not); no `INFO ... playlists updated` lines appear
  - [ ] 6.7 Playlist add detection: add a previously-archived video to a new playlist on YouTube, re-run, confirm the video's `metadata.json.playlists` array now includes the new entry and `download.log` has `INFO metadata.json playlists updated: <ID> (N entries)`
  - [ ] 6.8 Playlist remove detection: remove a previously-archived video from one of its playlists on YouTube, re-run, confirm the `playlists` array shrinks by one entry
  - [ ] 6.9 Playlist rename propagation: rename a playlist on YouTube, re-run, confirm every affected `metadata.json.playlists[i].title` reflects the new name (same `id`)
  - [ ] 6.10 PRD 5 territory untouched: manually edit a `metadata.json` to set `availability: "removed"`, `removed_detected_at: "2025-01-01T00:00:00Z"`, append a fake `history` entry, and change `current.title` to `"manually edited"`; run normally; confirm all four edits are exactly as left and only `playlists` may have changed
  - [ ] 6.11 Orphan silence: add a fake line `youtube zzz999fakeID` to `archive.txt`, re-run, confirm zero log lines mention the fake ID and the run exits cleanly
  - [ ] 6.12 Corrupted folder warning: delete `info.json` from one video's folder, re-run, confirm one `WARN metadata.json skipped: <ID> (info.json missing — folder corruption?)` appears in `download.log` and the run continues
  - [ ] 6.13 Channel-level failure skip: force a Pass 1 channel-level failure for one creator and confirm no `metadata.json` files are written or updated for that creator on this run (existing files byte-identical to before)
  - [ ] 6.14 Manifests-only mode is a no-op for PRD 4: run `uv run archive.py --manifests-only` and confirm no `metadata.json` files anywhere are modified (mtime check)
  - [ ] 6.15 Multi-playlist video — disk verification: for a multi-playlist video, `ls data/<slug>/videos/<ID>/` shows exactly one media file; there is no second copy of the video under another folder
