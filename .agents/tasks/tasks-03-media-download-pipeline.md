## Relevant Files

- `archive.py` - Pass 2 logic (channel-level failure gate, archive.txt filter, live/age classification, format resolution, per-video yt-dlp invocation, summary logging) is added to the single-file orchestrator from PRDs 1 and 2.
- `data/<slug>/archive.txt` - yt-dlp's native download archive. **Only yt-dlp writes to this file** (via `--download-archive`). The script reads it once into an in-memory set for filtering.
- `data/<slug>/videos/<ID>/<ID>.mkv` (or configured container) - Merged media file produced by yt-dlp.
- `data/<slug>/videos/<ID>/<ID>.info.json` - Raw video metadata produced by `--write-info-json`. PRD 4 will later normalize this into `metadata.json`.
- `data/<slug>/videos/<ID>/<ID>.webp` - Thumbnail produced by `--write-thumbnail --convert-thumbnails webp`.
- `data/<slug>/videos/<ID>/<ID>.en.vtt` / `<ID>.en.auto.vtt` - Subtitle files when available (creator-provided or auto-generated English). May be absent when neither exists; not an error.
- `data/<slug>/videos/<ID>/<ID>.part` - In-flight partial file when a download is interrupted. Left on disk untouched; yt-dlp's `--continue` resumes on the next run.
- `data/<slug>/logs/download.log` - Pass 2 activity log: filter summary, per-video `==> downloading` / `<-- done|FAILED` pairs, gated `WARN` lines, end-of-pass summary. Created empty by PRD 1.
- `data/<slug>/logs/errors.log` - Per-video failure detail (last 5 lines of yt-dlp stderr). Created empty by PRD 1.
- `config.example.toml` - Update to document the format-selector vs. `format_sort` distinction (sort keys *rank*, they don't *cap*; to cap resolution use `[height<=1080]` inside `format` itself).

### Notes

- Stdlib only: `subprocess` (via PRD 1's streaming helper), `pathlib`, `datetime`, `re` if needed for stderr tail extraction.
- Reuse PRD 1's `get_creator_loggers(slug)` (for `download_log` + `errors_log`) and `creator_scope(slug)` (for the per-creator exception boundary).
- yt-dlp is invoked as a subprocess; never `import yt_dlp`. Target version `2026.03.17`. Trust yt-dlp's built-in `--retries 10`; do not add a second retry loop.
- Downloads are strictly **serial**: one yt-dlp invocation must complete before the next starts. No threads, no `asyncio`, no `--max-downloads` chains.
- The script never writes to `archive.txt` from Python — yt-dlp updates it natively, and only on successful downloads. This is what guarantees failed downloads are naturally re-attempted on the next run.
- The candidate data structure produced by PRD 2 §4.7 is the input to this PRD; per-video classification reads `timestamp`, `live_status`, and `release_timestamp` from the manifest entries already in memory. Do not re-read manifest JSON from disk for ordinary operation.
- No automated test suite is in scope. Verification is the success-metrics walkthrough captured in parent task 5.0.
- Run the script with `uv run archive.py [--creator <slug>]` to exercise the default end-to-end flow.

## Instructions for Completing Tasks

**IMPORTANT:** As you complete each task, you must check it off in this markdown file by changing `- [ ]` to `- [x]`. This helps track progress and ensures you don't skip any steps.

Example:
- `- [ ] 1.1 Read file` → `- [x] 1.1 Read file` (after completing)

Update the file after completing each sub-task, not just after completing an entire parent task.

## Tasks

- [x] 0.0 Create feature branch
  - [x] 0.1 Confirm the PRD 02 feature branch has been merged into `main` (or is otherwise reflected there) before starting; resolve any outstanding state first
  - [x] 0.2 Create and checkout a new branch: `git checkout -b feature/media-download-pipeline`

- [x] 1.0 Wire Pass 2 into the default run flow with the channel-level failure gate
  - [x] 1.1 Implement `run_pass_two(creator, candidate_set)` and invoke it inside the PRD 1 `creator_scope(slug)` context manager so any uncaught exception is logged to that creator's `errors.log` and the run moves to the next creator
  - [x] 1.2 As the first step inside `run_pass_two`, check `candidate_set["channel_level_failed"]`; if `True`, emit `INFO Pass 2 skipped: channel-level failure in Pass 1` to `download.log`, print `<slug>: skipping Pass 2 due to Pass 1 channel-level failure` to stderr, and return early — no yt-dlp invocations
  - [x] 1.3 In the main loop, after Pass 1 returns the candidate-set structure for a creator and `--manifests-only` is NOT set, call `run_pass_two(creator, candidate_set)`
  - [x] 1.4 Process creators sequentially in the order they appear in `config.toml`; the existing `==> <slug>` console header from PRD 1 precedes both passes so the user can see exactly where the run is
  - [x] 1.5 Ensure unexpected exceptions inside `run_pass_two` propagate to `creator_scope` (do not swallow them with a bare `except Exception` here); successful runs return exit code `0`

- [x] 2.0 Build the eligible work list (archive.txt filter + live/age classification + oldest-first sort)
  - [x] 2.1 Read `data/<slug>/archive.txt` once at the start of Pass 2 into an in-memory set: parse each line as `youtube <VIDEO_ID>` and collect the IDs. Treat a missing file as an empty set
  - [x] 2.2 Partition the candidate video IDs: any ID already in the archive set is "already-archived" and skipped (it never reaches classification or yt-dlp); the rest continue to step 2.3
  - [x] 2.3 Classify each remaining candidate by reading `live_status` from its manifest entry: `"is_live"` → gated (live in progress); `"is_upcoming"` → gated (upcoming premiere); `"was_live"`, `"not_live"`, `None`, or missing → continue to 2.4
  - [x] 2.4 If the manifest entry has a `release_timestamp` and it is in the future relative to `datetime.now(timezone.utc)`, classify as gated (future release) regardless of `live_status`. Capture the ISO 8601 release timestamp for the logging line
  - [x] 2.5 Compute upload age from the manifest's `timestamp` (preferred) or `release_timestamp` (fallback). If `(now - upload_time).total_seconds() / 3600 < min_upload_age_hours`, classify as gated (too recent) and record the actual age in hours. When `min_upload_age_hours == 0`, skip the age check entirely
  - [x] 2.6 Implement the fallback fetch path: if the manifest entry lacks BOTH `timestamp` and `live_status`, run `yt-dlp --skip-download --no-warnings --print "%(timestamp)s|%(live_status)s|%(release_timestamp)s" "https://www.youtube.com/watch?v=<ID>"` once, parse the three pipe-delimited values (missing values come back as the literal string `NA`), and feed them back into 2.3–2.5
  - [x] 2.7 On fallback-fetch failure (non-zero exit, unparseable output), emit `WARN could not determine eligibility for <ID>, treating as eligible` to `download.log` and classify as eligible — the user's goal is to archive, so err on the side of attempting
  - [x] 2.8 Sort the eligible list **ascending by `timestamp`** (oldest first); entries with missing/unparseable timestamps sort to the end of the list
  - [x] 2.9 Return four buckets to the caller: `already_archived`, `eligible_sorted`, `gated_live_or_premiere`, `gated_too_recent`. Gated entries should carry the specific reason + relevant value (release timestamp, age in hours) so step 4.3 can emit the documented WARN lines verbatim

- [x] 3.0 Resolve format settings and invoke yt-dlp serially per eligible video
  - [x] 3.1 Resolve effective per-creator settings by overlaying per-creator overrides on top of `[defaults]` for `format`, `format_sort`, `merge_output_format`, and `min_upload_age_hours`. Override semantics: if a key is present in the `[[creator]]` block — even as an empty list or `0` — it shadows the `[defaults]` value entirely; no list-merging
  - [x] 3.2 Build the yt-dlp command as a Python list of args (NOT a shell string) with the documented args: `-f <format>`, `--merge-output-format <merge_output_format>`, `--download-archive data/<slug>/archive.txt`, `--write-info-json`, `--write-thumbnail`, `--write-subs`, `--write-auto-subs`, `--sub-langs "en.*,en"`, `--convert-thumbnails webp`, `--embed-metadata`, `--embed-thumbnail`, `--no-progress`, `-o "data/<slug>/videos/%(id)s/%(id)s.%(ext)s"`, and the watch URL `"https://www.youtube.com/watch?v=<VIDEO_ID>"`
  - [x] 3.3 Insert `-S "<comma-joined keys>"` into the args **only if** the resolved `format_sort` is a non-empty list; if empty or unset, omit `-S` entirely so yt-dlp uses its built-in default sort
  - [x] 3.4 For each eligible video in the sorted order from 2.8, invoke the command via `subprocess.Popen` with `stdout=PIPE, stderr=STDOUT` and stream raw yt-dlp output verbatim to `download.log` using the PRD 1 streaming helper
  - [x] 3.5 Run downloads strictly serially — one invocation must reach an exit code before the next is started. No threading, no `asyncio`, no `--max-downloads`
  - [x] 3.6 Rely on yt-dlp's default `--continue` for `.part` resume; do not add any custom partial-file handling
  - [x] 3.7 Never write to `archive.txt` from Python — yt-dlp updates it natively via `--download-archive` and only on successful downloads. This is what makes a failed download naturally retry on the next run
  - [x] 3.8 Update `config.example.toml` to document the `format` vs. `format_sort` distinction: `format_sort` keys *rank* available streams, they do not *cap* resolution; to cap, use `[height<=1080]` inside the `format` selector itself

- [x] 4.0 Implement Pass 2 logging, per-video failure routing, and per-creator console summary
  - [x] 4.1 At the start of Pass 2 (after the channel-level gate passes), emit `INFO Pass 2 starting: <total candidate count>` to `download.log`
  - [x] 4.2 After classification, emit `INFO Pass 2 filtering: <already_archived> already in archive.txt, <eligible> eligible, <gated_live> gated (live/premiere), <gated_age> gated (too recent)` to `download.log`
  - [x] 4.3 For each gated video, emit exactly one WARN line to `download.log` matching the bucket: `WARN gated (live stream in progress): <ID>`, `WARN gated (upcoming premiere): <ID>`, `WARN gated (future release: <ISO timestamp>): <ID>`, or `WARN gated (upload age <N>h < min_upload_age_hours=<M>): <ID>`
  - [x] 4.4 For each eligible video, write `INFO ==> downloading <ID>` to `download.log` before invoking yt-dlp, and `INFO <-- done: <ID>` (exit 0) or `INFO <-- FAILED: <ID>` (non-zero exit) immediately after
  - [x] 4.5 On non-zero yt-dlp exit, additionally append `ERROR download failed: <ID> — <last 5 lines of yt-dlp stderr>` to `errors.log`; do NOT add the ID to `archive.txt` (yt-dlp already enforces this natively); continue to the next eligible video
  - [x] 4.6 At the end of Pass 2 for a creator, emit `INFO Pass 2 complete: <downloaded> downloaded, <gated> gated, <failed> failed` to `download.log`
  - [x] 4.7 Print exactly one console summary line per creator at the end of Pass 2: `<slug>: Pass 2 — <downloaded> downloaded, <gated> gated, <failed> failed`. Always print this line — even when all counts are zero (per the resolved open question in PRD §9.5). Same convention applies to future per-creator summary lines in PRDs 5–8
  - [x] 4.8 Keep per-video activity confined to `download.log` — do not echo per-video progress to stdout; the console gets only the existing `==> <slug>` header (from PRD 1) and the single end-of-Pass-2 summary line

- [ ] 5.0 Verify against PRD success metrics
  - [ ] 5.1 End-to-end smoke run: pick a creator with one short playlist (5–10 videos) and run `uv run archive.py --creator <slug>`. Confirm every eligible video downloaded; `archive.txt` has one `youtube <ID>` line per download; each `data/<slug>/videos/<ID>/` contains `<ID>.mkv`, `<ID>.info.json`, `<ID>.webp`, and a `.vtt` file if subtitles exist
  - [ ] 5.2 Idempotent second run: re-run the same command immediately. Confirm zero downloads, `download.log` shows `Pass 2 filtering: N already in archive.txt, 0 eligible, ...`, no new files appear under `videos/`
  - [ ] 5.3 Channel-level failure gate: temporarily change one creator's `channel_url` to a bogus value so Pass 1 sets `channel_level_failed = True`. Confirm Pass 2 logs `Pass 2 skipped: channel-level failure in Pass 1`, no yt-dlp invocation runs for that creator, and the next creator still processes normally
  - [ ] 5.4 Format default applied: with `[defaults] format_sort = ["res:1080", "codec:av01"]`, download a known-available 1080p AV1 video. Run `ffprobe -v error -show_entries stream=codec_name,height` on the merged file and confirm `av1` codec and `1080` height
  - [ ] 5.5 Per-creator format override: add `format_sort = ["res:2160", "codec:av01"]` to one creator block. Re-run against a video known to be available in 4K. Confirm ffprobe shows 2160p for that creator while other creators remain at 1080p
  - [ ] 5.6 Upload-age gate: set `min_upload_age_hours = 999999` temporarily. Run against a creator with recent uploads. Confirm new videos appear as `WARN gated (upload age <N>h < min_upload_age_hours=999999)` and are NOT in `archive.txt`. Reset to `48`, re-run, confirm they download
  - [ ] 5.7 Live/premiere gate: point a creator's `extra_playlists` at a playlist containing an upcoming-premiere video. Confirm Pass 2 logs `WARN gated (upcoming premiere): <ID>` and the ID is NOT in `archive.txt`
  - [ ] 5.8 Per-video failure isolation: include a known-deleted video in `extra_playlists`. Confirm `errors.log` has `ERROR download failed: <ID> — ...`, the other videos in the same playlist still download, the run exits `0`, and the failed video is NOT in `archive.txt`
  - [ ] 5.9 Oldest-first ordering: pick a creator with ~10 candidate videos. Kill the process after 3 downloads complete. Inspect the 3 downloaded videos' `info.json` `timestamp` fields and confirm they are the 3 oldest in the candidate set
  - [ ] 5.10 Multi-creator end-to-end: configure 2 brand-new creators. Run `uv run archive.py`. Confirm creator 1's `==> <slug>` header and Pass-2 summary appear entirely before creator 2's, both produce populated subtrees, and both have populated `archive.txt` files
  - [ ] 5.11 Resume from interrupted download: start a download and kill the process mid-`.part`. Re-run. Confirm `download.log` shows yt-dlp's `[download] Resuming download at byte N` line and the final merged file is complete
