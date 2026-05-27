# PRD 06 — Upgrade Execution

## 1. Introduction / Overview

This PRD adds the `--upgrade` command — the destructive sibling of PRD 5's read-only `--refresh-metadata`. For every archived video whose `metadata.json.upgrade_available` is non-null, PRD 6 re-downloads the video at the creator's currently-configured format settings, replaces the on-disk media (and sidecar files) atomically, appends an `{type: "upgrade", ...}` entry to `history`, and clears `upgrade_available`.

The hard requirement of this PRD is that **the original media file is never lost**. Every per-video upgrade uses a `.pre-upgrade` rename of the existing files as a rollback point. If the re-download fails or is interrupted, the originals are restored. If it succeeds, the `.pre-upgrade` files are deleted.

PRD 6 is strictly separated from PRD 5 so that frequent automated metadata refreshes (cheap; weekly) never trigger expensive re-downloads. The user decides when to act on detected upgrades.

See [`youtube-archive-plan.md`](../youtube-archive-plan.md) §"`--upgrade` — perform pending upgrades" and [`prd-overview.md`](../prd-overview.md). PRD 6 owns the execution half of verification item #14.

## 2. Goals

1. `uv run archive.py --upgrade` walks all `metadata.json` files for selected creators, identifies the set with non-null `upgrade_available`, and re-downloads each in sequence at current format settings.
2. Each per-video upgrade is atomic from the user's perspective: either the new media + sidecars are in place AND `metadata.json` reflects the new `downloaded` block AND a history entry was appended, OR everything is reverted to the prior state.
3. A failed or interrupted upgrade for one video never destroys the original. The `.pre-upgrade` files are the safety net that guarantees this.
4. A second `--upgrade` invocation after an interrupted run recovers cleanly: stale `.pre-upgrade` files are inspected and either restored (if the upgrade was incomplete) or cleaned up (if the upgrade completed but cleanup didn't).
5. The operator gets a per-creator and final summary so a hands-off run gives a clear "what changed" report.

## 3. User Stories

- **As the developer**, when I run `uv run archive.py --refresh-metadata` weekly and see `12 upgrades available` in the summary, I want to run `uv run archive.py --upgrade` at my leisure to actually pull the better versions — without worrying that a failed upgrade will leave me without the original.
- **As the developer**, when a 4K upgrade fails halfway because of a network drop, I want my next `--upgrade` run to detect the leftover `.pre-upgrade` files and recover automatically.
- **As the developer**, if I change a creator's `format_sort` to prefer 4K and run `--upgrade`, I want every 1080p-archived video that has a 4K version available to get upgraded — not just videos that match the original config.
- **As the developer**, I want to scope an upgrade run with `--upgrade --creator <slug>` when I only have time/bandwidth to upgrade one creator.

## 4. Functional Requirements

### 4.1 When PRD 6 runs

1. PRD 6 MUST run only when `--upgrade` is passed on the CLI.
2. `--upgrade` MUST skip Pass 1 (PRD 2), Pass 2 (PRD 3), PRD 4, and PRD 5. The script does NOT discover manifests, does NOT walk `archive.txt` for new downloads, does NOT update `playlists` membership, and does NOT refresh `current.*` fields.
3. `--upgrade` MAY be combined with `--creator <slug>` to scope to one creator.
4. `--upgrade` does NOT pre-flight a refresh — it trusts the existing `upgrade_available` state from the last `--refresh-metadata` run. If the user wants fresh state, they run `--refresh-metadata` first. Per Q3 = A.
5. `--upgrade` is composable with `--dry-run` (PRD 7's territory). When dry-run is in effect, PRD 6 lists what *would* be upgraded and exits without touching any files.

### 4.2 Startup recovery (stale `.pre-upgrade` cleanup)

6. **Before** processing any upgrade targets, PRD 6 MUST sweep each selected creator's `data/<slug>/videos/` tree for `.pre-upgrade` files left behind by a prior interrupted run.
7. For each video folder containing any `.pre-upgrade` files, classify and act:
    - **Case A — incomplete upgrade**: the canonical media file (e.g., `<ID>.mkv` per the creator's resolved `merge_output_format`) does NOT exist (or exists with size 0). Action: restore every `<filename>.pre-upgrade` → `<filename>` by renaming. Delete any `<ID>.<ext>.part` leftovers. Log to `upgrade.log` (new file, see §4.10): `INFO startup recovery: restored <ID> from .pre-upgrade (canonical media missing)`.
    - **Case B — completed upgrade with cleanup interrupted**: the canonical media file exists AND has non-zero size AND a fresh `<ID>.info.json` exists alongside (i.e., yt-dlp finished but the script was killed before deleting `.pre-upgrade` files OR before writing `metadata.json`). Action: log to `upgrade.log` and `errors.log` as **ambiguous**: `WARN startup recovery: ambiguous .pre-upgrade leftovers for <ID> — both old and new media present. Skipping this video; manually delete either the .pre-upgrade files or the new files before re-running --upgrade.`  Skip this video in the current run. Do NOT delete or restore anything.
    - **Case C — orphan `.pre-upgrade` only**: `.pre-upgrade` files exist but the parent folder's video ID isn't in `archive.txt`. Action: log a `WARN` and leave alone — outside `--upgrade`'s scope to clean up arbitrary orphans (PRD 8 territory).
8. The recovery sweep MUST complete for all selected creators before any new upgrade work begins. This guarantees the run starts from a clean state.

### 4.3 Target selection

9. After recovery, PRD 6 MUST walk every `data/<slug>/videos/<ID>/metadata.json` for each selected creator (or just the one named by `--creator`).
10. A video is an **upgrade target** if its `metadata.json.upgrade_available` is non-null AND `metadata.json.availability == "available"` AND the canonical media file exists on disk.
11. Targets with `availability != "available"` (removed, private, members-only, age-restricted) MUST be skipped — yt-dlp won't be able to re-download them anyway. Log one `INFO` per skipped video: `INFO upgrade skipped: <VIDEO_ID> (availability: <state>)`.
12. Targets MUST be processed in **detected_at ascending order** (oldest detection first), per creator. Matches PRD 3's "interrupted-run-leaves-contiguous-progress" principle: if you give up midway, the longest-pending upgrades are the ones already done.
13. If no targets remain after filtering, the script logs one `INFO` line per creator (`upgrade: 0 targets`) and proceeds to the next creator (or to the final summary if scoped to one creator).

### 4.4 Per-video upgrade flow

14. For each target video, in order:
    1. **Capture state**: read the existing `metadata.json` into memory. Snapshot the current `downloaded` block as `from_downloaded` and the current `upgrade_available` value.
    2. **Move sidecars to `.pre-upgrade`** (§4.5).
    3. **Invoke yt-dlp** with the same command shape as PRD 3 §4.16 (`-f`, `-S`, `--merge-output-format`, sidecar flags, `--no-progress`, output template), EXCEPT: do NOT pass `--download-archive`. Rationale: `archive.txt` already contains this ID, so passing `--download-archive` would make yt-dlp refuse to re-download. We want a fresh download here.
    4. **On yt-dlp success** (exit 0, canonical media file exists post-merge, fresh `info.json` exists):
        - Read the fresh `<ID>.info.json`.
        - Build the new `downloaded` block from it using the same field mapping as PRD 4 §4.2 (`format_id`, `height`, `vcodec`, `acodec`, `filesize` with `filesize_approx` fallback, then `os.path.getsize` final fallback).
        - **Identical-format check**: if `new_downloaded.format_id == from_downloaded.format_id`, treat as a no-op upgrade (§4.6). Otherwise proceed.
        - Atomically rewrite `metadata.json` (§4.8):
            - Replace `downloaded` with `new_downloaded`.
            - Set `upgrade_available` to `null`.
            - Append one entry to `history`: `{type: "upgrade", observed_at: <now UTC ISO>, from: <from_downloaded>, to: <new_downloaded>}`.
            - Leave every other field untouched — `current.*`, `last_metadata_check`, `availability`, `removed_detected_at`, `playlists`, `archived_at`, `video_id`, `source_creator`, `schema_version`. Per Q2 = A.
        - **Delete `.pre-upgrade` files** (§4.5 #19).
        - Log `INFO upgrade succeeded: <VIDEO_ID> (<old format_id> -> <new format_id>, height <old> -> <new>)` to `upgrade.log`.
    5. **On yt-dlp failure** (non-zero exit, OR canonical media file missing after exit, OR `info.json` missing): execute the rollback path (§4.7).
15. After each video completes (success, no-op, or rollback), proceed to the next target. No video failure aborts the run.
16. **Concurrency**: serial only. One yt-dlp invocation completes (success or failure) before the next starts. Matches PRD 3 and PRD 5 for the same rate-limit and log-readability reasons.

### 4.5 Sidecar handling

17. **Sidecar definition**: every file in `data/<slug>/videos/<ID>/` EXCEPT `metadata.json`, `metadata.json.tmp`, and any existing `*.pre-upgrade` file. This catches the media file, `<ID>.info.json`, `<ID>.webp`, `<ID>.*.vtt`, and any other artifacts yt-dlp may have produced (`.description`, `.live_chat.json`, etc.). Per Q1 = A — treat the whole folder's contents as one atomic unit.
18. **Pre-upgrade rename**: for each sidecar file `<filename>`, rename to `<filename>.pre-upgrade`. Use `os.rename` (atomic on the same filesystem). If any rename fails, restore any already-renamed files in this video and treat the whole video as failed (rollback path).
19. **Successful cleanup**: after a successful (or no-op) upgrade, delete every `<filename>.pre-upgrade` in the video's folder. Use `os.unlink`.
20. **Failure restoration** (rollback): for each `.pre-upgrade` file present, rename `<filename>.pre-upgrade` → `<filename>`. If the destination exists (because yt-dlp wrote a partial new file), delete the partial first, then restore. Also delete any `<ID>.<ext>.part` files yt-dlp may have left behind.
21. The `metadata.json` file is NEVER moved to `.pre-upgrade`. It's modified in-place atomically via PRD 4 §4.7's tmp-then-rename pattern; if the rename fails, the prior `metadata.json` is untouched.

### 4.6 Identical-format edge case (no-op upgrade)

22. If yt-dlp succeeds but `new_downloaded.format_id == from_downloaded.format_id`, treat this as a stale-upgrade-flag scenario (the `upgrade_available` descriptor was set days/weeks ago and the better format is no longer applicable).
23. In this case:
    - Do NOT append a history entry (nothing actually changed).
    - Set `upgrade_available` to `null` (clear the stale flag).
    - Leave `downloaded` exactly as it was.
    - Delete the `.pre-upgrade` files (the new download produced the same content; keeping `.pre-upgrade` would just waste disk).
    - Log `INFO upgrade no-op: <VIDEO_ID> (format_id unchanged: <format_id>; upgrade_available cleared)`.
24. The no-op outcome counts as a successful upgrade for purposes of the summary (it cleared the stale flag), but is reported as a separate counter (see §4.9).

### 4.7 Failure handling and rollback

25. A per-video failure is any of:
    - yt-dlp exit code is non-zero.
    - yt-dlp exited 0 but the expected canonical media file does NOT exist post-run (sanity check against silent failures).
    - yt-dlp exited 0 but the fresh `<ID>.info.json` is missing.
    - The atomic `metadata.json` write itself raises an exception.
    - Sidecar restoration during rollback raises an exception (escalated to creator-scope error boundary).
26. On any per-video failure:
    1. **Rollback the sidecars**: restore every `.pre-upgrade` file to its original name (§4.5 #20). Delete any partial new files (`.part`, partial info.json, etc.) that yt-dlp left behind.
    2. **Leave `metadata.json` untouched**: don't write anything. `upgrade_available` stays populated so the next `--upgrade` run will retry naturally.
    3. **Log to `upgrade.log`**: `WARN upgrade failed: <VIDEO_ID> — <one-line reason>; rollback completed`.
    4. **Log to `errors.log`**: `ERROR upgrade failed: <VIDEO_ID> — <last 5 lines of yt-dlp stderr if available>`.
    5. **Continue** to the next target.
27. If the rollback itself fails (e.g., a `.pre-upgrade` file got corrupted or the disk filled up), this is an exceptional state:
    - Log `ERROR upgrade rollback FAILED: <VIDEO_ID> — manual intervention required; check <folder path>` to `errors.log`.
    - Do NOT modify `metadata.json`.
    - Continue to the next target (other videos shouldn't suffer for one's broken state).
28. The PRD 1 `creator_scope` boundary catches any uncaught exception during a creator's `--upgrade` work, logs to that creator's `errors.log`, and continues to the next creator. PRD 6's per-video error handling is the inner layer; `creator_scope` is the outer safety net.

### 4.8 `metadata.json` write semantics

29. `metadata.json` writes follow PRD 4 §4.7's atomic pattern: write to `metadata.json.tmp`, then `os.replace()`. Field order matches PRD 4 §4.2 #5 exactly. Trailing newline. `indent=2`, `ensure_ascii=False`, `sort_keys=False`.
30. The new history entry MUST be appended to the END of the `history` array (chronological order, oldest first). Existing entries are preserved unchanged.
31. The history entry's field order MUST be: `type`, `observed_at`, `from`, `to`. Matches the convention from PRD 5 §4.39 (`type` first, then timestamp, then payload).
32. `from` and `to` each carry the FULL `downloaded` block (5 fields: `format_id`, `height`, `vcodec`, `acodec`, `filesize`) — never a partial / changed-only block. Same rationale as PRD 5's full `previous` block: uniform schema, robust to readers that pluck specific fields.

### 4.9 Console summary

33. After each creator's upgrade work completes, the script MUST print one line to stdout:
    ```
    <slug>: upgrade — <T> targets, <S> succeeded, <N> no-op, <F> failed, <K> skipped
    ```
    Where:
    - `<T>` = total targets (videos with non-null `upgrade_available` at start of this creator's run, including ones skipped for availability)
    - `<S>` = successful upgrades (format_id changed)
    - `<N>` = no-op upgrades (format_id unchanged; stale flag cleared)
    - `<F>` = failed upgrades (rolled back)
    - `<K>` = skipped (unavailable, missing media, ambiguous .pre-upgrade leftovers from recovery)
34. After all creators complete, the script MUST print one final summary line:
    ```
    Total: <S> succeeded, <N> no-op, <F> failed, <K> skipped across <C> creators
    ```
35. If only one creator was processed (via `--creator`), the final total line still prints with `<C> = 1` for uniformity.
36. Per-video console output is silent during the run; details go to `upgrade.log` and `download.log` (yt-dlp's own output stream).

### 4.10 Logging

37. PRD 6 MUST create and write to `data/<slug>/logs/upgrade.log` — a new per-creator log file. PRD 1's `get_creator_loggers` helper (extended in PRD 5 for `refresh.log`) MUST be extended further to expose this fifth log file.
38. yt-dlp's own output during the per-video re-download streams to `download.log` (the existing PRD 3 log) via the PRD 1 subprocess helper, framed by `==> upgrading <VIDEO_ID>` / `<-- done: <VIDEO_ID>` (or `<-- FAILED`) markers to distinguish from normal PRD 3 download activity.
39. Phase lines in `upgrade.log`:
    - At start: `INFO upgrade pass starting (<T> targets after filtering)`
    - One per startup-recovery action (§4.2 #7): `INFO startup recovery: ...` or `WARN startup recovery: ambiguous ...`
    - One per video, one of:
      - `INFO upgrade succeeded: <VIDEO_ID> (<old format_id> -> <new format_id>, height <old> -> <new>)`
      - `INFO upgrade no-op: <VIDEO_ID> (format_id unchanged: <format_id>; upgrade_available cleared)`
      - `WARN upgrade failed: <VIDEO_ID> — <reason>; rollback completed`
      - `INFO upgrade skipped: <VIDEO_ID> (availability: <state>)`
    - At end: `INFO upgrade complete: <S> succeeded, <N> no-op, <F> failed, <K> skipped`
40. Real failures (per §4.25) also write a one-line `ERROR upgrade failed: <VIDEO_ID> — ...` to `errors.log` with yt-dlp's last 5 stderr lines.
41. PRD 6 does NOT touch `manifests.log` or `refresh.log`.

## 5. Non-Goals (Out of Scope)

- **Updating `current.*` from the new `info.json`** post-upgrade. PRD 5's job; per Q2 = A. PRD 6 only touches `downloaded`, `upgrade_available`, and `history`.
- **Updating `playlists` membership.** PRD 4's job; requires Pass 1 manifests, which `--upgrade` skips.
- **Pre-flighting a refresh** before upgrading (`--refresh-metadata` first). Per Q3 = A. If you want fresh state, run `--refresh-metadata` yourself, then `--upgrade`.
- **Parallel upgrades.** Serial only; matches PRD 3 / PRD 5.
- **Bandwidth throttling** (`--limit-rate`). Same answer as PRD 3.
- **`--upgrade --force <video_id>`** to upgrade a specific video even if `upgrade_available` is null. Out of scope for v1; if you really want to re-download, manually remove the line from `archive.txt` and run a normal sync.
- **Disk-space pre-check** before each upgrade. A 4K upgrade can be 5× the size of 1080p, and `.pre-upgrade` is kept until success — peak usage is 6× the original briefly. v1 trusts that the user has space; if not, yt-dlp's own write will fail naturally and we'll roll back. Add a pre-check if it becomes a real pain point.
- **Auto-resolving ambiguous startup-recovery state** (Case B in §4.7). Manual intervention required.
- **A separate `--rollback <video_id>` command** to undo a recent upgrade by restoring from history. History records the prior `downloaded` block but the prior *media file* is gone after success — true rollback would require keeping `.pre-upgrade` around longer, which we deliberately don't.
- **A `--max-upgrades N` cap per run.** Not in v1.
- **Updating `archive.txt`** at all. The video ID is already in `archive.txt`; the upgrade just refreshes the bytes behind it. `archive.txt` doesn't track format — only ID.

## 6. Design Considerations

- **Why no `--download-archive` on the upgrade invocation (§4.14 #3).** yt-dlp's `--download-archive` is a "skip if already archived" gate. The video IS already archived — that's the whole point. Passing `--download-archive` would cause yt-dlp to skip the download entirely. We omit it for the upgrade and rely on the surrounding script logic to enforce the source-of-truth invariants instead.
- **Why a per-video `--upgrade` doesn't pre-refresh.** Three reasons: (1) network cost — doubling round-trips per video; (2) clean boundaries — refresh is a deliberate user action, not a hidden side effect of upgrade; (3) the identical-format check (§4.6) is the safety net for stale `upgrade_available` flags.
- **Why ambiguous startup recovery (Case B) is skip-and-warn, not auto-resolved.** The dangerous window between "yt-dlp finishes" and "metadata.json is written" is small (a few ms typically) but real. If we encounter both the new media AND `.pre-upgrade` files, we genuinely don't know whether the new download was good or whether yt-dlp produced garbage that got partially written. Asking the user to resolve manually is safer than guessing.
- **Why oldest-detection-first ordering for targets.** If interrupted, you finish the longest-pending upgrades first — the ones the user has been waiting on the longest. Same principle as PRD 3's "oldest-upload-first."
- **Why `from` and `to` carry full `downloaded` blocks.** Same reasoning as PRD 5's `previous` block: uniform schema, easy programmatic access, history is the only place to learn what the prior state was once it's been overwritten.
- **Why `metadata.json` writes don't use a `.pre-upgrade` rename.** `metadata.json` writes use the tmp-then-replace pattern already established by PRD 4 §4.7 — that's already atomic. The `.pre-upgrade` rename is for the media + sidecar files, which can't use tmp-then-replace because they're large and yt-dlp wants to write directly to the final path.
- **Why all sidecars get treated as one atomic unit (Q1 = A).** Partial rollback (e.g., media restored but new info.json kept) creates inconsistent state that downstream tooling (PRD 4 reading info.json fields, PRD 8 audit) would silently mis-handle. Treating the whole folder as one unit is operationally bulletproof.

## 7. Technical Considerations

- **PRD dependencies:**
  - PRD 1: per-creator loggers (extended for `upgrade.log` per §4.37), `creator_scope` error boundary, subprocess streaming helper.
  - PRD 3: yt-dlp command shape and per-creator format resolution (PRD 3 §4.5). PRD 6 reuses the same command construction code path — ideally factored into a helper after PRD 3 ships.
  - PRD 4: `metadata.json` schema and atomic write pattern.
  - PRD 5: populates `upgrade_available`. PRD 6 reads it but doesn't write back to the field in a way PRD 5 would care about (just clears it on success).
- **Sidecar enumeration:** use `Path(folder).iterdir()` and filter. Don't try to predict sidecar extensions; just exclude `metadata.json`, `metadata.json.tmp`, and `*.pre-upgrade`. This catches future yt-dlp output formats automatically.
- **`os.rename` vs `os.replace`:**
  - For `.pre-upgrade` moves: `os.rename` is fine (source must not be replaced by an existing destination).
  - For `metadata.json` atomic writes: `os.replace` (overrides destination atomically). Same as PRD 4.
- **Atomicity guarantees:** Filesystem rename is atomic on the same filesystem (POSIX guarantee, also true on APFS/macOS). The whole upgrade is NOT one atomic transaction (we make multiple filesystem operations), but each individual rename is, and the worst-case interrupted state is handled by §4.2's startup recovery.
- **Filesize fallback chain** for the new `downloaded` block (§4.14 #4): `info.json.filesize` → `info.json.filesize_approx` → `os.path.getsize(<media_file>)`. Same as PRD 4 §4.2 #6.
- **`upgrade.log` path:** `data/<slug>/logs/upgrade.log`. Plain text, append-only, no rotation — same conventions as the other log files from PRD 1.
- **Don't pass `--download-archive`:** worth a code comment when constructing the yt-dlp command. Easy to add accidentally when copying from PRD 3's command builder.

## 8. Success Metrics

This PRD is done when **all** of the following are true:

1. **Successful upgrade (verification item #14, execution half).** Set up: archive a video at 1080p. Manually edit its `metadata.json.upgrade_available` to a 4K format descriptor (or set `format_sort = ["res:2160"]` in the creator config and run `--refresh-metadata` first). Run `uv run archive.py --upgrade --creator <slug>`. Confirm:
    - The on-disk media file is now 4K (`ffprobe` shows `height=2160`).
    - `metadata.json.downloaded.height` is `2160`.
    - `metadata.json.upgrade_available` is `null`.
    - `metadata.json.history` has one new entry with `type: "upgrade"`, `from.height: 1080`, `to.height: 2160`.
    - No `.pre-upgrade` files remain in the folder.
    - `upgrade.log` has the success line.
    - `current.*` fields are unchanged (PRD 5's domain).
2. **Identical-format no-op.** Manually populate `upgrade_available` with a descriptor whose `format_id` matches `downloaded.format_id`. Run `--upgrade`. Confirm:
    - `upgrade_available` is now `null`.
    - `history` is unchanged (no new entry).
    - `downloaded` block is unchanged.
    - No `.pre-upgrade` files remain.
    - `upgrade.log` has the no-op line.
3. **Failed upgrade restoration.** Set up: a real upgrade target. Force yt-dlp to fail (e.g., briefly unplug network mid-run, or use a `min_filesize` flag that's impossibly large). Confirm after the run:
    - The original media file is back in place with byte-identical content (sha256 match against pre-run snapshot).
    - All sidecars are back in place with byte-identical content.
    - `metadata.json` is unchanged (`upgrade_available` still populated).
    - `upgrade.log` has the failure line, rollback noted as complete.
    - `errors.log` has the `ERROR upgrade failed` entry.
    - No `.pre-upgrade` leftovers.
4. **Sidecar atomic unit.** During an upgrade, manually verify partway through (use a debugger or insert a sleep) that the folder contains `<ID>.mkv.pre-upgrade`, `<ID>.info.json.pre-upgrade`, `<ID>.webp.pre-upgrade`, and any `.vtt.pre-upgrade` files alongside the in-progress new downloads. After a successful run, all `.pre-upgrade` files are gone.
5. **Startup recovery — Case A.** Set up: start an upgrade, kill the process mid-download (before yt-dlp produces the canonical media file). The folder now contains `<ID>.mkv.pre-upgrade` etc. and `<ID>.mkv.part`. Re-run `--upgrade`. Confirm: startup recovery restores the `.pre-upgrade` files, deletes the `.part`, and the original video is back. The recovery log line appears. The video is then re-attempted in the normal flow.
6. **Startup recovery — Case B (ambiguous).** Set up: create both `<ID>.mkv.pre-upgrade` and a synthetic `<ID>.mkv` of non-zero size in a video folder. Run `--upgrade`. Confirm the script logs the ambiguous WARN, skips the video, and does NOT delete or restore anything.
7. **No `--download-archive` regression.** Run `--upgrade` for a real target. Confirm yt-dlp actually downloads (doesn't skip with "already in archive"). Check `download.log` for the actual progress / merge output, not a skip notice.
8. **`current.*` boundary preserved (Q2 = A).** Note `metadata.json.current.title`, `current.thumbnail_url`, `current.description_hash` before an upgrade. Run `--upgrade`. Confirm all three are byte-identical after — even if the new `info.json` has a different title than what was in `current.title`.
9. **Multi-target ordering.** Set up: three upgrade targets with `upgrade_available.detected_at` of yesterday, last week, and last month. Run `--upgrade`. Confirm `upgrade.log` shows the last-month target processed first, then last-week, then yesterday.
10. **Unavailable video skipped.** Set up: a video with `availability: "removed"` and `upgrade_available` populated (an edge case but valid state). Run `--upgrade`. Confirm: log shows `INFO upgrade skipped: <ID> (availability: removed)`, no yt-dlp invocation happens, no `.pre-upgrade` files created.
11. **`--creator` scoping.** With multiple creators in config, each with upgrade targets, run `--upgrade --creator <slug>`. Confirm only the named creator's videos are upgraded (other creators' files have unchanged mtimes).
12. **No targets case.** Run `--upgrade` against a creator with zero non-null `upgrade_available` fields. Confirm one console line (`<slug>: upgrade — 0 targets, 0 succeeded, 0 no-op, 0 failed, 0 skipped`), no yt-dlp calls, exit code 0.
13. **Console summary aggregation.** Three creators, mixed outcomes: creator A has 2 successful, creator B has 1 failed + 1 no-op, creator C has 0 targets. Confirm the final total line reads `Total: 2 succeeded, 1 no-op, 1 failed, 0 skipped across 3 creators`.
14. **Default invocation doesn't trigger PRD 6.** Run `uv run archive.py` (no `--upgrade`). Confirm `upgrade.log` is not appended to and no `.pre-upgrade` files appear.

These map onto verification item **#14** (the execution half: `--upgrade` replaces file, history entry, no `.pre-upgrade` leftovers) from the plan.

## 9. Open Questions

1. **Should we keep `.pre-upgrade` files around for N hours/days after success** as a manual-rollback safety net? Currently deleted immediately. A `--keep-backups` flag could preserve them, but disk cost is real for 4K upgrades. Defer.
2. **Should Case B (ambiguous recovery) auto-resolve** if the new `info.json` exists and matches `upgrade_available.format_id`? We rejected the auto-resolve in §6 design considerations, but if Case B becomes a frequent operational issue we might revisit.
3. **A `--retry-failed` flag** that re-attempts only previously-failed upgrades? Currently you just re-run `--upgrade`; failed ones still have `upgrade_available` populated, so they get retried naturally. Probably not needed.
4. **Should `history.upgrade` entries include the reason for upgrade** (e.g., "config-driven: format_sort changed to res:2160" vs "tier-rolled-out: new AV1 1080p available")? Currently no — we don't have that signal. The diff from `from` to `to` is enough for forensics.
5. **Should we record the time-on-disk of the prior media** in the history entry (e.g., `from.downloaded_at`)? Could compute from `archived_at` minus the most recent prior upgrade entry's `observed_at`. Not in v1.
6. **Should `--upgrade` print a count of targets BEFORE starting** ("about to upgrade 12 videos across 3 creators; this may take a while")? Useful for "did I really want to do this?" moments. Currently it just starts. Add a confirmation prompt? Skipped for v1 (cron-friendliness).
