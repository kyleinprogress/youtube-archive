## Relevant Files

- `archive.py` - `--upgrade` code path (startup recovery sweep, target selection, per-video upgrade flow, `.pre-upgrade` rename/restore/cleanup, identical-format no-op, rollback, `history` append, per-creator + grand-total console summary) is added to the single-file orchestrator from PRDs 1–5.
- `data/<slug>/videos/<ID>/<filename>.pre-upgrade` - Temporary rollback files created per upgrade attempt. All sidecars (media file, info.json, thumbnail, subtitles, anything else in the folder except `metadata.json` / `metadata.json.tmp` / existing `.pre-upgrade`) are renamed to `<filename>.pre-upgrade` before yt-dlp runs. Deleted on success; restored on failure.
- `data/<slug>/videos/<ID>/metadata.json` - PRD 6 mutates `downloaded`, sets `upgrade_available = null`, and appends one `{type: "upgrade", observed_at, from, to}` history entry on success. All other fields stay byte-identical.
- `data/<slug>/archive.txt` - **Never touched.** The video ID is already there; the upgrade just refreshes the bytes behind it.
- `data/<slug>/logs/upgrade.log` - **New per-creator log file** introduced by this PRD. Created empty by PRD 1's helper (extended here to expose a fifth log file).
- `data/<slug>/logs/download.log` - yt-dlp's own output for the re-download streams here, framed by `==> upgrading <ID>` / `<-- done|FAILED` markers.
- `data/<slug>/logs/errors.log` - Per-video failure detail (last 5 lines of yt-dlp stderr) appended here on rollback.

### Notes

- Stdlib only: `subprocess`, `json`, `pathlib`, `os` (`os.rename`, `os.replace`, `os.unlink`, `os.path.getsize`), `shutil` not strictly needed here, `datetime`.
- Reuse PRD 1's `get_creator_loggers(slug)` (now extended to return a fifth logger for `upgrade.log`), `creator_scope(slug)`, the subprocess streaming helper.
- Reuse PRD 4's `write_json_atomic(path, obj)` for the `metadata.json` rewrite.
- Reuse PRD 3 §4.5's format-resolution + command-builder logic for the yt-dlp invocation. **CRITICAL difference:** the upgrade invocation must NOT pass `--download-archive` — the ID is already in archive.txt and would otherwise be skipped. Worth a code comment.
- Reuse PRD 4 §4.2 #6's filesize fallback chain: `info.json.filesize` → `info.json.filesize_approx` → `os.path.getsize(<media_file>)`.
- Downloads are strictly **serial** — one yt-dlp invocation completes before the next starts.
- The whole upgrade is NOT one atomic filesystem transaction, but each individual `rename`/`replace` is atomic, and the worst-case interrupted state is handled by the startup recovery sweep.
- No automated test suite is in scope; verification is the success-metrics walkthrough in parent task 6.0.

## Instructions for Completing Tasks

**IMPORTANT:** As you complete each task, you must check it off in this markdown file by changing `- [ ]` to `- [x]`. This helps track progress and ensures you don't skip any steps.

Example:
- `- [ ] 1.1 Read file` → `- [x] 1.1 Read file` (after completing)

Update the file after completing each sub-task, not just after completing an entire parent task.

## Tasks

- [ ] 0.0 Create feature branch
  - [ ] 0.1 Confirm the PRD 05 feature branch has been merged into `main` (or is otherwise reflected there); resolve any outstanding state first
  - [ ] 0.2 Create and checkout a new branch: `git checkout -b feature/upgrade-execution`

- [ ] 1.0 Wire `--upgrade` as a standalone code path and add the fifth per-creator log file
  - [ ] 1.1 Extend PRD 1's `get_creator_loggers(slug)` to return a fifth logger backed by `data/<slug>/logs/upgrade.log`; touch the file in PRD 1's directory-setup step so it exists empty after the first PRD 6 run
  - [ ] 1.2 Add `--upgrade` handling to the main CLI dispatcher so it skips Pass 1 (PRD 2), Pass 2 (PRD 3), PRD 4, and PRD 5 entirely
  - [ ] 1.3 Allow composition with `--creator <slug>` to scope to one creator; allow composition with `--dry-run` (PRD 7 territory; ensure `--upgrade` checks the flag)
  - [ ] 1.4 `--upgrade` MUST NOT pre-flight a `--refresh-metadata` — trust the existing `upgrade_available` state. If the user wants fresh state, they run `--refresh-metadata` first
  - [ ] 1.5 Wrap each creator's upgrade work in `creator_scope(slug)` so uncaught exceptions log to `errors.log` and the run moves on to the next creator

- [ ] 2.0 Implement the startup recovery sweep for `.pre-upgrade` leftovers
  - [ ] 2.1 BEFORE processing any upgrade targets, sweep each selected creator's `data/<slug>/videos/` tree for any folder containing one or more `*.pre-upgrade` files
  - [ ] 2.2 For each such folder, classify per PRD 6 §4.2 #7: **Case A (incomplete upgrade)** = canonical media file (e.g., `<ID>.<resolved merge_output_format>`) does not exist OR exists with size 0; **Case B (completed but cleanup interrupted)** = canonical media file exists with non-zero size AND fresh `<ID>.info.json` exists alongside; **Case C (orphan .pre-upgrade only)** = `.pre-upgrade` files exist but the parent folder's video ID isn't in `archive.txt`
  - [ ] 2.3 Case A action: restore every `<filename>.pre-upgrade` → `<filename>` via `os.rename`; delete any leftover `<ID>.<ext>.part` files; log `INFO startup recovery: restored <ID> from .pre-upgrade (canonical media missing)` to `upgrade.log`
  - [ ] 2.4 Case B action: log `WARN startup recovery: ambiguous .pre-upgrade leftovers for <ID> — both old and new media present. Skipping this video; manually delete either the .pre-upgrade files or the new files before re-running --upgrade.` to BOTH `upgrade.log` AND `errors.log`; skip this video in the current run; do NOT delete or restore anything
  - [ ] 2.5 Case C action: log a `WARN` to `upgrade.log` and leave the files alone — outside `--upgrade`'s scope to clean up arbitrary orphans (PRD 8 territory)
  - [ ] 2.6 The recovery sweep MUST complete for all selected creators before any new upgrade work begins

- [ ] 3.0 Implement target selection
  - [ ] 3.1 After recovery, walk every `data/<slug>/videos/<ID>/metadata.json` for each selected creator; parse JSON
  - [ ] 3.2 A video is an upgrade target if `metadata.json.upgrade_available` is non-null AND `metadata.json.availability == "available"` AND the canonical media file exists on disk
  - [ ] 3.3 For targets with `availability != "available"` (removed, private, members_only, age_restricted), log `INFO upgrade skipped: <ID> (availability: <state>)` to `upgrade.log` and skip — yt-dlp can't re-download them anyway
  - [ ] 3.4 Sort the eligible target list ascending by `upgrade_available.detected_at` (oldest detection first); ties broken by video ID for determinism
  - [ ] 3.5 If zero targets remain after filtering for a creator, log `INFO upgrade: 0 targets` to `upgrade.log` and emit the per-creator console summary with all zeros; proceed to the next creator
  - [ ] 3.6 At the start of the upgrade pass for a creator, emit `INFO upgrade pass starting (<T> targets after filtering)` to `upgrade.log`

- [ ] 4.0 Implement the per-video upgrade flow
  - [ ] 4.1 For each target in detected_at order, capture state: read the existing `metadata.json` into memory; snapshot the current `downloaded` block as `from_downloaded` and the current `upgrade_available` value
  - [ ] 4.2 Move sidecars to `.pre-upgrade`: enumerate every file in the video's folder EXCEPT `metadata.json`, `metadata.json.tmp`, and any existing `*.pre-upgrade`; rename each `<filename>` → `<filename>.pre-upgrade` via `os.rename`. If any rename fails, restore any already-renamed files in this video and treat the whole video as failed (rollback path 5.0)
  - [ ] 4.3 Invoke yt-dlp with the same command shape as PRD 3 §4.16 (`-f`, `-S`, `--merge-output-format`, sidecar flags, `--no-progress`, `-o "data/<slug>/videos/%(id)s/%(id)s.%(ext)s"`, watch URL) but DELIBERATELY **omit `--download-archive`** (worth a code comment when constructing the command). Resolve format settings the same way PRD 3 does — per-creator overrides shadow `[defaults]`
  - [ ] 4.4 Stream yt-dlp stdout/stderr verbatim to `download.log` via the PRD 1 streaming helper, framed by `INFO ==> upgrading <ID>` before and `INFO <-- done: <ID>` (exit 0) or `INFO <-- FAILED: <ID>` (non-zero exit) after
  - [ ] 4.5 On yt-dlp success, verify the canonical media file exists post-merge AND the fresh `<ID>.info.json` exists; if either is missing, treat as a failure and go to the rollback path
  - [ ] 4.6 Read the fresh `<ID>.info.json` and build a `new_downloaded` block using PRD 4 §4.2's field mapping (`format_id`, `height`, `vcodec`, `acodec`, `filesize` with the three-step fallback chain)
  - [ ] 4.7 **Identical-format check** (no-op upgrade): if `new_downloaded.format_id == from_downloaded.format_id`, treat as a stale-upgrade-flag scenario — do NOT append a history entry, set `upgrade_available = null`, leave `downloaded` exactly as it was, delete the `.pre-upgrade` files, log `INFO upgrade no-op: <ID> (format_id unchanged: <format_id>; upgrade_available cleared)` to `upgrade.log`, count this as a no-op in the summary, continue to next target
  - [ ] 4.8 On a real upgrade (format_id changed): atomically rewrite `metadata.json` via PRD 4's `write_json_atomic(...)` with `downloaded` replaced by `new_downloaded`, `upgrade_available` set to `null`, and one new entry appended to `history`: `{"type": "upgrade", "observed_at": <now UTC ISO>, "from": <from_downloaded full block>, "to": <new_downloaded full block>}`. Field order in the history entry: `type`, `observed_at`, `from`, `to`. Preserve every other field byte-identically — `current.*`, `last_metadata_check`, `availability`, `removed_detected_at`, `playlists`, `archived_at`, `video_id`, `source_creator`, `schema_version`
  - [ ] 4.9 Delete every `<filename>.pre-upgrade` in the video's folder via `os.unlink`
  - [ ] 4.10 Log `INFO upgrade succeeded: <ID> (<old format_id> -> <new format_id>, height <old> -> <new>)` to `upgrade.log`
  - [ ] 4.11 Concurrency: strictly serial — one yt-dlp invocation completes (success, no-op, or failure) before the next starts

- [ ] 5.0 Implement failure handling, rollback, and console summary
  - [ ] 5.1 Detect per-video failures: yt-dlp exit non-zero; OR exit 0 but canonical media file missing post-run; OR exit 0 but fresh `<ID>.info.json` missing; OR the atomic `metadata.json` write itself raises; OR sidecar restoration during rollback raises
  - [ ] 5.2 On failure, restore sidecars: for each `<filename>.pre-upgrade` present, rename back to `<filename>` via `os.rename`. If the destination exists (yt-dlp wrote a partial new file), delete the partial first, then restore. Also delete any `<ID>.<ext>.part` files yt-dlp left behind
  - [ ] 5.3 Leave `metadata.json` untouched on failure — don't write anything; `upgrade_available` stays populated so the next `--upgrade` retries naturally
  - [ ] 5.4 Log `WARN upgrade failed: <ID> — <one-line reason>; rollback completed` to `upgrade.log`; log `ERROR upgrade failed: <ID> — <last 5 lines of yt-dlp stderr if available>` to `errors.log`; continue to the next target
  - [ ] 5.5 On a rollback that itself fails (corrupted `.pre-upgrade` files, disk full, etc.): log `ERROR upgrade rollback FAILED: <ID> — manual intervention required; check <folder path>` to `errors.log`; do NOT modify `metadata.json`; continue to the next target
  - [ ] 5.6 The `creator_scope` boundary catches any uncaught exception during a creator's `--upgrade` work, logs to `errors.log`, and continues with the next creator
  - [ ] 5.7 At end-of-pass for a creator, log `INFO upgrade complete: <S> succeeded, <N> no-op, <F> failed, <K> skipped` to `upgrade.log`
  - [ ] 5.8 Print one console line per creator: `<slug>: upgrade — <T> targets, <S> succeeded, <N> no-op, <F> failed, <K> skipped`. Always print, even with all zeros
  - [ ] 5.9 After all creators complete, print one final summary line: `Total: <S> succeeded, <N> no-op, <F> failed, <K> skipped across <C> creators`. If `--creator` was used, `<C>` is `1`
  - [ ] 5.10 Per-video console output during the run is silent; details go to `upgrade.log` and `download.log`

- [ ] 6.0 Verify against PRD success metrics
  - [ ] 6.1 Successful upgrade: archive a video at 1080p; manually edit its `metadata.json.upgrade_available` to a 4K format descriptor (or set `format_sort = ["res:2160"]` and run `--refresh-metadata` first); run `--upgrade --creator <slug>`; confirm the on-disk media file is now 4K (`ffprobe` shows `height=2160`), `metadata.json.downloaded.height == 2160`, `upgrade_available == null`, one new `history` entry with `type: "upgrade"`, `from.height: 1080`, `to.height: 2160`, no `.pre-upgrade` files remain, `current.*` fields unchanged
  - [ ] 6.2 Identical-format no-op: manually populate `upgrade_available` with a descriptor whose `format_id` matches `downloaded.format_id`; run `--upgrade`; confirm `upgrade_available == null`, `history` unchanged (no new entry), `downloaded` unchanged, no `.pre-upgrade` files remain, `INFO upgrade no-op: ...` line in `upgrade.log`
  - [ ] 6.3 Failed upgrade restoration: snapshot all sidecar sha256 hashes pre-run; force yt-dlp to fail (e.g., unplug network mid-run or pass an impossible filter); confirm after the run the original media + all sidecars are byte-identical (sha256 match), `metadata.json` unchanged (`upgrade_available` still populated), `upgrade.log` has the failure line with `; rollback completed`, `errors.log` has the `ERROR upgrade failed` entry, no `.pre-upgrade` leftovers
  - [ ] 6.4 Sidecar atomic unit: during an upgrade, observe partway through (insert a debugger or sleep) that the folder contains `<ID>.<ext>.pre-upgrade`, `<ID>.info.json.pre-upgrade`, `<ID>.webp.pre-upgrade`, and `<ID>.*.vtt.pre-upgrade` (when present) alongside the new in-progress files; after success all `.pre-upgrade` files are gone
  - [ ] 6.5 Startup recovery — Case A: start an upgrade, kill the process mid-download (before yt-dlp produces the canonical media file); re-run `--upgrade`; confirm startup recovery restores the `.pre-upgrade` files, deletes the `.part`, the original video is back, the recovery log line appears, and the video is re-attempted in the normal flow
  - [ ] 6.6 Startup recovery — Case B: create both `<ID>.<ext>.pre-upgrade` and a synthetic `<ID>.<ext>` of non-zero size + a fresh-looking `<ID>.info.json` in a video folder; run `--upgrade`; confirm the script logs the ambiguous WARN, skips the video, and does NOT delete or restore anything
  - [ ] 6.7 No `--download-archive` regression: run `--upgrade` for a real target; confirm yt-dlp actually downloads (does not skip with "already in archive"); check `download.log` for actual progress/merge output
  - [ ] 6.8 `current.*` boundary preserved: snapshot `current.title`, `current.thumbnail_url`, `current.description_hash` before an upgrade; run `--upgrade`; confirm all three are byte-identical after, even when the new `info.json` carries different values
  - [ ] 6.9 Multi-target ordering: three upgrade targets with `detected_at` of yesterday, last week, last month; run `--upgrade`; confirm `upgrade.log` shows the last-month target processed first, then last-week, then yesterday
  - [ ] 6.10 Unavailable video skipped: hand-edit a video to `availability: "removed"` with `upgrade_available` populated; run `--upgrade`; confirm `INFO upgrade skipped: <ID> (availability: removed)` in `upgrade.log`, no yt-dlp invocation, no `.pre-upgrade` files
  - [ ] 6.11 `--creator` scoping: with multiple creators each having upgrade targets, run `--upgrade --creator <slug>`; confirm only the named creator's videos are upgraded (mtime check on other creators' files)
  - [ ] 6.12 No targets case: run `--upgrade` against a creator with zero non-null `upgrade_available`; confirm one console line with all zeros, no yt-dlp calls, exit code 0
  - [ ] 6.13 Console summary aggregation: three creators with mixed outcomes (A: 2 succeeded; B: 1 failed + 1 no-op; C: 0 targets); confirm the final total line reads `Total: 2 succeeded, 1 no-op, 1 failed, 0 skipped across 3 creators`
  - [ ] 6.14 Default invocation doesn't trigger PRD 6: run `uv run archive.py` (no `--upgrade`); confirm `upgrade.log` is not appended to and no `.pre-upgrade` files appear anywhere
