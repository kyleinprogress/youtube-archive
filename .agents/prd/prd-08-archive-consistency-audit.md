# PRD 08 — Archive Consistency Audit

## 1. Introduction / Overview

This PRD adds the `--audit` command — a read-only health check that cross-references `archive.txt` against on-disk state for each creator and surfaces five classes of discrepancy. It complements PRD 3's "`archive.txt` is the sole source of truth" posture by giving a safe, scheduled way to detect when that source of truth has drifted from reality.

The most operationally important discrepancy is **orphan-in-archive** — an ID listed in `archive.txt` whose video file is missing on disk. Once detected, the optional `--repair` flag prunes those entries from `archive.txt` so the next normal `uv run archive.py` invocation re-downloads them. Per the user's stated primary concern: missing media files are the failure mode that matters; other discrepancies (missing or stale `metadata.json`) self-heal during a normal sync via PRD 4's rebuild path (PRD 4 §4.6 #21).

This PRD does NOT discover new videos, refresh metadata, re-download anything, or compute file hashes. It's purely a disk-and-text health check.

See [`prd-overview.md`](../prd-overview.md). PRD 8 is a new addition to the roadmap (not in the original plan); it owns no verification items from the plan and instead defines its own.

## 2. Goals

1. `uv run archive.py --audit` for every selected creator produces a per-creator report on stdout summarizing every detected discrepancy, plus a final cross-creator total. The report is glance-able (summary line per class) and actionable (per-video detail).
2. The same report content is written to a new per-creator `data/<slug>/logs/audit.log` for forensic / historical inspection.
3. `--audit` is strictly read-only on `archive.txt`, `metadata.json`, and all media files. No filesystem mutations happen in audit-only mode.
4. `--audit --repair` removes orphan-in-archive entries from `archive.txt` after writing a timestamped backup. No other discrepancy class is auto-fixed.
5. A weekly cron-friendly invocation (`uv run archive.py --audit`) completes in seconds for a typical 1000-video-per-creator archive, with no network calls.

## 3. User Stories

- **As the developer**, I want to schedule a weekly `--audit` run that catches the case where I (or `rm -rf`) accidentally deleted a video folder — without me having to notice until months later when I look for the file.
- **As the developer**, when the audit reports `5 orphans in archive.txt`, I want `--audit --repair` to fix them in one command and leave me a backup of the prior `archive.txt` in case I want to undo.
- **As the developer**, when audit reports a malformed `archive.txt` line (e.g., from a botched manual edit), I want it flagged with the line number so I can fix it by hand.
- **As the developer**, when an audit finds `videos/<ID>/` folders that aren't in `archive.txt` (orphans on disk), I want them reported but NOT auto-deleted — those might be intentional manual archive additions.
- **As the developer running this in CI to validate config integrity**, I want `--audit --dry-run` to behave identically to `--audit` (both are already read-only); `--audit --dry-run --repair` to skip the `archive.txt` mutation per PRD 7.

## 4. Functional Requirements

### 4.1 When PRD 8 runs

1. PRD 8 MUST run only when `--audit` is passed on the CLI.
2. `--audit` MUST skip Pass 1, Pass 2, PRD 4, PRD 5, and PRD 6. Audit is a standalone read-only command.
3. `--audit` MAY be combined with `--creator <slug>` to scope to one creator.
4. `--audit` MAY be combined with `--repair` to enable the only writable action: pruning orphan-in-archive entries from `archive.txt`.
5. `--audit` MAY be combined with `--dry-run` (PRD 7 handles the suppression of `audit.log` writes and `--repair` mutations).
6. Audit is read-only with respect to network — no yt-dlp calls, no HTTP requests, no DNS lookups.

### 4.2 Discrepancy classes

The audit MUST detect and categorize these five classes, in this order of operational importance:

7. **Class A — Orphan in archive.txt** (the primary concern; target of `--repair`):
    - Video ID is listed in `archive.txt` AND any of these is true:
        - `data/<slug>/videos/<ID>/` does not exist as a directory.
        - The directory exists but the canonical media file `data/<slug>/videos/<ID>/<ID>.<ext>` is missing (where `<ext>` is the resolved `merge_output_format` for this creator from `config.toml`).
        - The directory exists but the canonical media file exists with size 0.
    - Rationale: archive.txt says we have this video; we don't. The next normal sync would skip re-downloading because of `archive.txt`, so this video is silently lost.
8. **Class B — Orphan on disk**:
    - A subdirectory of `data/<slug>/videos/` named `<ID>` exists AND `<ID>` is NOT in `archive.txt` AND the canonical media file is present (with non-zero size).
    - Rationale: there's archived media that the system doesn't know it has. Likely a partial run that didn't make it to `archive.txt`, or a manual addition.
9. **Class C — Incomplete folder**:
    - `data/<slug>/videos/<ID>/` exists AND `<ID>` IS in `archive.txt` AND the folder is in an unexpected mid-state:
        - Has `<ID>.info.json` but no canonical media file.
        - Has a `<ID>.<ext>.part` file (yt-dlp resume marker).
        - Has canonical media but NO `<ID>.info.json` (very unusual; manual mess).
    - Rationale: download was interrupted or partially manually deleted. Distinct from Class A because the folder isn't empty.
    - Note: a folder matching Class A's "media file missing" criterion that ALSO has `info.json` is classified as Class A (the more actionable diagnosis), not Class C.
10. **Class D — Invalid `metadata.json`** (only checked if the folder, media file, AND `info.json` all exist for an ID in `archive.txt`):
    - `metadata.json` is missing.
    - `metadata.json` exists but isn't parseable as JSON.
    - `metadata.json` parses but `schema_version` field is missing or is not `1` (PRD 4 §4.2 — the only valid value in this codebase).
    - `metadata.json` parses but its `video_id` field doesn't match the folder name.
    - Rationale: light validation per Q4=A. Doesn't validate every field's type — covers the high-value corruption cases without becoming a maintenance burden. These all self-heal during a normal sync (PRD 4's first-creation or rebuild path), so they're report-only.
11. **Class E — Malformed archive.txt line**:
    - A non-empty, non-comment (not starting with `#`) line in `archive.txt` that doesn't match the regex `^youtube [a-zA-Z0-9_-]{11}$` (yt-dlp's standard format: literal `youtube`, single space, 11-character video ID).
    - Rationale: corrupted state from manual edits or buggy writes. Reported with line number so the user can fix it.

### 4.3 `archive.txt` parsing and validation

12. The script MUST read `data/<slug>/archive.txt` line by line, preserving line numbers (1-indexed) for error reporting.
13. Empty lines and lines starting with `#` are tolerated and skipped (not flagged as malformed). This matches yt-dlp's own tolerant parsing.
14. Each non-empty, non-comment line MUST be validated against the format `youtube <11-char-video-id>`. Failures are Class E.
15. The set of valid video IDs extracted from `archive.txt` is the **expected set** used in §4.4.
16. If `archive.txt` does not exist for a creator, the audit MUST treat it as an empty expected set. This is not itself an error — a creator that's never been synced has no `archive.txt`.

### 4.4 Disk scan and discrepancy detection

17. The script MUST list `data/<slug>/videos/` and identify all subdirectories whose name matches a YouTube video ID format (regex `^[a-zA-Z0-9_-]{11}$`). Subdirectories with other names are ignored (they don't belong to PRD 3's layout).
18. The script MUST resolve the canonical media file extension by reading the creator's resolved `merge_output_format` from config (same precedence as PRD 3 §4.5).
19. For each ID in the expected set (from §4.3), classify per §4.2 #7–10. An ID may match Class A, then Class C if applicable, then Class D — but each ID is reported in at most ONE class, choosing the most severe (A > C > D, since A blocks future re-download entirely, C indicates active corruption, D is recoverable via sync).
20. For each video folder on disk NOT in the expected set, classify per §4.2 #8 (Class B).
21. The audit MUST NOT follow symlinks during scanning. If a video folder is a symlink, skip it with one `INFO` line in `audit.log` (`symlink skipped: <path>`).

### 4.5 `metadata.json` validation (Class D detail)

22. For each Class D candidate (ID in archive.txt, folder + media + info.json all present), the script MUST:
    1. Check if `metadata.json` exists. If not → Class D with reason `missing`.
    2. Read and `json.loads(...)`. On `JSONDecodeError` → Class D with reason `unparseable`.
    3. Check `parsed.get("schema_version") == 1`. If not → Class D with reason `schema_version=<actual>` (or `schema_version=missing`).
    4. Check `parsed.get("video_id") == <folder_name>`. If not → Class D with reason `video_id_mismatch (got <actual>, folder is <folder>)`.
    5. If all pass, no Class D entry.
23. No further field validation in v1 (per Q4=A). If a future PRD wants stricter checks, this is the place to add them.

### 4.6 `--repair` action

24. `--repair` MUST only act when combined with `--audit`. Passing `--repair` without `--audit` is a CLI usage error (exit code 2 with message `error: --repair requires --audit`).
25. `--repair` MUST only modify `archive.txt`. It MUST NOT delete files, move files, regenerate `metadata.json`, or take any action on Class B / C / D / E discrepancies.
26. For each Class A discrepancy detected, `--repair` MUST:
    1. Back up `archive.txt` per §4.7 (once per creator per `--repair` run, before any modifications).
    2. Rewrite `archive.txt` excluding the lines for the orphan IDs. Preserve original line ordering for remaining entries. Use atomic write (write to `archive.txt.tmp`, then `os.replace`).
    3. Log each pruned ID to `audit.log`: `INFO repair pruned: <VIDEO_ID> (orphan in archive.txt)`.
    4. Print one console line per pruned ID under the `--repair` section.
27. If there are zero Class A discrepancies for a creator, `--repair` is a no-op for that creator (no backup written, no modifications). Log: `INFO repair: 0 orphans to prune`.
28. `--repair` does NOT touch `archive.txt` files for creators whose audit produced zero Class A entries — keeps the backup-file proliferation minimal.

### 4.7 `archive.txt` backup

29. Before any modification to `archive.txt`, the script MUST write a backup to `data/<slug>/archive.txt.pre-repair-<YYYYMMDDTHHMMSSZ>` in the same directory.
30. The backup is a byte-for-byte copy of `archive.txt` immediately prior to modification (use `shutil.copy2` to preserve mtime).
31. If a backup file already exists for the exact same second (unlikely but possible), append a counter suffix: `.pre-repair-<timestamp>-2`, `-3`, etc. Don't overwrite existing backups.
32. The script MUST NOT auto-prune old backup files. They accumulate; the user manages disk space if it becomes an issue. (Open question §9.1.)
33. Log the backup path: `INFO repair backup: <path>` to `audit.log`.

### 4.8 Console output format

34. Per-creator section header:
    ```
    ==> <slug> (AUDIT)
    ```
    (Or `==> <slug> (AUDIT --repair)` when `--repair` is in effect.)
35. The per-creator body groups discrepancies by class, in the operational-priority order from §4.2 (A → B → C → D → E). Empty classes are omitted.
36. Output template (illustrative; exact text matches §4.2's class names):
    ```
    ==> <slug> (AUDIT)
      Orphan in archive.txt (Class A) — 2:
        abc123 — folder missing
        def456 — media file missing (folder + info.json present)
      Orphan on disk (Class B) — 1:
        ghi789 — folder present with media; not in archive.txt
      Incomplete folder (Class C) — 1:
        jkl012 — info.json present, no media file
      Invalid metadata.json (Class D) — 2:
        mno345 — metadata.json missing
        pqr678 — unparseable JSON
      Malformed archive.txt lines (Class E) — 1:
        Line 42: "youtube zzz" (invalid video ID format)
      Summary: 2 Class A, 1 Class B, 1 Class C, 2 Class D, 1 Class E
    ```
37. When `--repair` is set, after the summary line, print a `--repair` action section:
    ```
      --repair:
        backup written: data/<slug>/archive.txt.pre-repair-20260526T153042Z
        pruned: abc123, def456
        2 entries removed from archive.txt
    ```
38. If no discrepancies were found, the per-creator output is:
    ```
    ==> <slug> (AUDIT)
      OK — no discrepancies detected
    ```
39. Final cross-creator total (printed after all creators):
    ```
    Total: <A> Class A, <B> Class B, <C> Class C, <D> Class D, <E> Class E across <N> creators
    ```
40. If `--creator` was used, `<N>` is `1` for uniformity.

### 4.9 `audit.log` format

41. PRD 8 MUST create and append to `data/<slug>/logs/audit.log` — a new per-creator log file. PRD 1's `get_creator_loggers` helper (extended in PRD 5 for `refresh.log` and PRD 6 for `upgrade.log`) MUST be extended to expose this sixth log file.
42. The log format matches the existing convention from PRD 1 §4.29: `<ISO 8601 UTC timestamp> <LEVEL> <message>` per line, plain text, append-only, no rotation.
43. Per-creator phase lines in `audit.log`:
    - At start: `INFO audit starting`
    - One per discrepancy detected, e.g.:
      - `WARN class_a orphan_in_archive: <VIDEO_ID> (folder missing)`
      - `WARN class_a orphan_in_archive: <VIDEO_ID> (media file missing)`
      - `INFO class_b orphan_on_disk: <VIDEO_ID>`
      - `WARN class_c incomplete_folder: <VIDEO_ID> (info.json present, no media)`
      - `WARN class_d invalid_metadata: <VIDEO_ID> (missing)`
      - `WARN class_d invalid_metadata: <VIDEO_ID> (unparseable)`
      - `WARN class_d invalid_metadata: <VIDEO_ID> (schema_version=2)`
      - `WARN class_d invalid_metadata: <VIDEO_ID> (video_id_mismatch: got xyz999, folder is mno345)`
      - `WARN class_e malformed_archive_line: line 42 ("<full line content>")`
    - When `--repair` runs:
      - `INFO repair backup: <path>`
      - `INFO repair pruned: <VIDEO_ID> (orphan in archive.txt)`
      - `INFO repair complete: <N> entries pruned`
    - At end: `INFO audit complete: A=<n> B=<n> C=<n> D=<n> E=<n>`
44. Class B is `INFO`-level rather than `WARN` because orphan-on-disk often represents legitimate state (deferred reconciliation) rather than corruption. The other discrepancy classes are `WARN`-level.

### 4.10 Dry-run interaction

45. `--audit --dry-run` behaves identically to `--audit` for the scanning/reporting phases (both are already read-only).
46. `--audit --dry-run` MUST suppress writes to `audit.log` (per PRD 7's universal rule). The console output is unchanged.
47. `--audit --dry-run --repair` MUST run the audit normally, then in the `--repair` section print what *would* be done with a `[DRY RUN]` prefix and skip the actual `archive.txt` modification and backup:
    ```
      --repair:
        [DRY RUN] would back up archive.txt to data/<slug>/archive.txt.pre-repair-<timestamp>
        [DRY RUN] would prune from archive.txt: abc123, def456
        [DRY RUN] 2 entries would be removed
    ```

### 4.11 Error handling

48. Per-creator audit work is wrapped in PRD 1's `creator_scope` boundary. An unexpected exception during one creator's audit logs to `errors.log` and continues to the next creator.
49. Specific recoverable errors:
    - `archive.txt` is unreadable (permissions, missing parent directory): log `ERROR audit failed: archive.txt unreadable — <reason>` to `errors.log`, log to `audit.log`, console summary for this creator is `<slug>: AUDIT FAILED — archive.txt unreadable`. Continue to next creator.
    - `videos/` directory is unreadable: same pattern.
    - One `metadata.json` is unreadable due to permissions (not just unparseable): classify as Class D with reason `unreadable`, continue.
50. Exit code: `0` for a successful audit run, regardless of discrepancies detected. `2` if `--repair` was passed without `--audit`. `1` only if the audit itself crashed (uncaught exception escaped `creator_scope` for every creator).

## 5. Non-Goals (Out of Scope)

- **Auto-fixing Class B / C / D / E.** Per Q1 = A, only Class A gets auto-repair. Other classes self-heal via normal sync (PRD 4's rebuild path handles Class D; Classes B and C require manual judgment).
- **Computing file hashes (sha256) for bitrot detection.** The plan defers integrity hashes explicitly; PRD 8 only does existence + size-nonzero checks.
- **Full schema validation of `metadata.json`** (every field type, enum values, etc.). Per Q4 = A — light validation only.
- **Network calls of any kind.** Audit is offline.
- **Deleting orphan-on-disk folders.** Too destructive; user must decide.
- **Adding orphan-on-disk IDs to `archive.txt`** as an alternative repair. Too aggressive; archive.txt should only contain things we know we downloaded via yt-dlp.
- **Auto-pruning old backup files.** Backups accumulate; user cleans up.
- **A `--repair-class b` / `--repair-class c` flag** for per-class repair. v1 only repairs Class A. If other classes need fixes later, add explicit flags.
- **A `--include-creator <slug>` (positive list) flag distinct from `--creator <slug>`.** v1 uses the existing `--creator` for scoping.
- **Interactive confirmation prompt before `--repair`.** Cron-unfriendly. Backup file (§4.7) is the safety net.
- **Logging Class B as a `WARN`.** It's `INFO` because orphan-on-disk is often expected state during partial recovery scenarios.
- **Auditing `manifests/`, `creator.json`, or PRD 5/6 state files.** PRD 8 is scoped to the `archive.txt` ↔ `videos/` consistency relationship. Manifests are snapshots; they don't have a consistency invariant to enforce.

## 6. Design Considerations

- **Why Class A is the only auto-repair target.** Per the user's note: "main concern is the actual video file is missing." Class A is the only discrepancy that *blocks future re-download* (archive.txt says "done"; reality says "no"). The other classes either resolve themselves on next sync (Class D via PRD 4's rebuild) or require user judgment (B, C, E).
- **Class priority for single-ID classification (§4.4 #19).** An ID can match multiple classes simultaneously (e.g., A and C: folder exists with info.json but no media). We classify under A in that case because A is what `--repair` acts on; surfacing the C aspect would just confuse the report. The reason text (`media file missing (folder + info.json present)`) preserves the informational nuance.
- **Why `--repair` writes a backup every time it modifies anything.** Cheap insurance against bugs in the audit logic (e.g., a future change that mis-classifies a Class A and prunes a valid entry). User can `mv archive.txt.pre-repair-<ts> archive.txt` to recover. The backup file proliferation is acceptable in exchange for safety.
- **Why backups are NOT auto-pruned.** Auto-pruning is itself a destructive operation that we'd then need to be careful about. The user manages disk space; backups are small (text files with one line per archived video — typically tens of KB even for large archives).
- **Why `archive.txt` is the only thing `--repair` touches.** It's the canonical state; everything else can be reconstructed by re-running the appropriate command. Mutating other files would expand the failure surface.
- **Why the 11-char regex for video IDs (§4.3 #14, §4.4 #17).** YouTube video IDs have been 11 characters since 2007; this is a stable invariant. The regex `[a-zA-Z0-9_-]{11}` matches yt-dlp's own convention. We're strict because the alternative is treating malformed entries as silent OK.
- **Why we skip symlinks (§4.4 #21).** A symlinked video folder could point outside `data/`, which would make filesystem semantics weird (e.g., the audit could traverse into unexpected places). v1 just skips and logs; if symlinks become a real use case, define semantics in a future PRD.
- **Why audit.log uses class names as part of the log message** (e.g., `WARN class_a orphan_in_archive: ...`). Makes grep-by-class trivial (`grep "class_a" audit.log`). Tradeoff: log lines are slightly longer.

## 7. Technical Considerations

- **PRD dependencies:**
  - PRD 1: per-creator loggers (extended for `audit.log`), `creator_scope` error boundary.
  - PRD 3: `archive.txt` format (`youtube <ID>` lines), `videos/<ID>/` folder layout, per-creator `merge_output_format` resolution for canonical media file extension.
  - PRD 4: `metadata.json` schema (`schema_version`, `video_id` fields) for Class D validation.
  - PRD 7: dry-run flag suppression of `audit.log` writes and `--repair` mutations.
- **Filesystem operations:**
  - `Path.iterdir()` for listing `videos/` subdirectories. Filter by name regex.
  - `Path.is_dir()`, `Path.is_file()` for existence checks.
  - `Path.stat().st_size` for the zero-size check (Class A).
  - `Path.is_symlink()` for the §4.4 #21 skip.
  - `shutil.copy2()` for archive.txt backup (preserves mtime).
  - `os.replace()` for atomic archive.txt rewrite.
- **Reading `archive.txt`:** open as text, iterate `enumerate(file, start=1)` to keep line numbers for Class E reporting.
- **Parsing `metadata.json`:** `json.load()` with `try/except (json.JSONDecodeError, OSError)`.
- **Performance:** for a 1000-video creator: 1 archive.txt read + 1 directory list + ~1000 stat calls + ~1000 small JSON parses. Should be sub-second on any reasonable disk.
- **No yt-dlp invocation.** Audit doesn't touch the network.
- **Sorting:** within each class, IDs are reported in `archive.txt` line order (for Class A, C, D, E) or directory iteration order (Class B). Stable across runs as long as the underlying source is stable.
- **`--repair` atomicity:** write the new `archive.txt` content to `archive.txt.tmp`, `os.replace` onto `archive.txt`. If the script is killed between backup-write and `os.replace`, the original `archive.txt` is still intact and the backup is present — safe to retry.

## 8. Success Metrics

This PRD is done when **all** of the following are true:

1. **Class A — folder missing.** Add a fake line `youtube zzzzzzzzzzz` to `archive.txt`. Run `uv run archive.py --audit --creator <slug>`. Confirm the report lists `zzzzzzzzzzz — folder missing` under Class A and the per-creator summary shows `1 Class A`. `audit.log` has the matching `WARN class_a` line.
2. **Class A — media file missing.** For a real archived video, delete its `<ID>.<ext>` media file (keep `info.json`, `metadata.json`). Run `--audit`. Confirm Class A entry with reason `media file missing (folder + info.json present)`.
3. **Class A — media file zero-size.** Truncate a real archived video to 0 bytes (`truncate -s 0 <path>`). Run `--audit`. Confirm Class A entry.
4. **Class B — orphan on disk.** Create `data/<slug>/videos/aaaaaaaaaaa/aaaaaaaaaaa.mkv` (non-zero size, valid ID format) by hand. Make sure `aaaaaaaaaaa` is NOT in `archive.txt`. Run `--audit`. Confirm Class B entry. Confirm it's reported as `INFO` in `audit.log`, not `WARN`.
5. **Class C — incomplete folder.** Create `data/<slug>/videos/bbbbbbbbbbb/bbbbbbbbbbb.info.json` (an empty JSON file `{}`) and add `youtube bbbbbbbbbbb` to `archive.txt`. Run `--audit`. Confirm Class A entry (not Class C — per §4.4 #19, A wins). Now create a `bbbbbbbbbbb.mkv.part` file in the folder too — confirm Class A still wins (media-file-missing dominates).
6. **Class C — pure case.** Create a folder with media file present AND `info.json` MISSING. Add to archive.txt. Run --audit. Confirm Class C with reason `info.json missing`.
7. **Class D — missing metadata.json.** For a real archived video with media + info.json present, delete its `metadata.json`. Run `--audit`. Confirm Class D entry with reason `missing`.
8. **Class D — unparseable.** Replace a video's `metadata.json` with literal garbage (`echo "garbage" > metadata.json`). Run `--audit`. Confirm Class D entry with reason `unparseable`.
9. **Class D — wrong schema_version.** Edit a `metadata.json` to set `"schema_version": 2`. Run `--audit`. Confirm Class D entry with reason `schema_version=2`.
10. **Class D — video_id mismatch.** Edit a `metadata.json` to set `"video_id": "xxxxxxxxxxx"`. Run `--audit`. Confirm Class D entry with reason `video_id_mismatch (got xxxxxxxxxxx, folder is <real-folder>)`.
11. **Class E — malformed archive.txt line.** Add a non-empty, non-comment line that isn't `youtube <11-char-id>` (e.g., `garbage`, `youtube xyz`, `youtube ` with no ID). Run `--audit`. Confirm Class E entry with line number and `audit.log` shows `WARN class_e malformed_archive_line: line N (...)`.
12. **All-OK case.** Run `--audit` on a clean creator with no issues. Confirm per-creator output is `OK — no discrepancies detected` and `audit.log` shows `INFO audit complete: A=0 B=0 C=0 D=0 E=0`.
13. **`--repair` happy path.** Start with two Class A discrepancies. Run `--audit --repair`. Confirm:
    - `archive.txt.pre-repair-<timestamp>` exists with the original (pre-repair) content.
    - `archive.txt` no longer contains the two orphan IDs.
    - `audit.log` shows the `INFO repair backup`, two `INFO repair pruned`, and `INFO repair complete: 2 entries pruned` lines.
    - Console output includes the `--repair:` section with backup path and pruned IDs.
14. **`--repair` no-op.** Run `--audit --repair` against a creator with zero Class A discrepancies. Confirm no backup file is created, `archive.txt` is unchanged (byte-identical), and `audit.log` shows `INFO repair: 0 orphans to prune`.
15. **`--repair` only touches Class A.** Set up a creator with one of every class (A, B, C, D, E). Run `--audit --repair`. Confirm only the Class A entry is pruned from `archive.txt`; the disk state for Class B / C / D / E is byte-identical to before.
16. **`--repair` without `--audit` rejected.** Run `uv run archive.py --repair`. Confirm exit code `2` and error message `error: --repair requires --audit`.
17. **`--audit --dry-run`.** Run with the same setup as #13. Confirm `audit.log` has NO new entries (mtime check), but console output is identical to the non-dry-run case.
18. **`--audit --dry-run --repair`.** Same setup. Confirm `archive.txt` is unchanged AND no backup file is created AND console output shows the `[DRY RUN] would prune ...` lines under `--repair:`.
19. **No network calls.** Use `iptables` / `pf` / network monitoring to block outbound, OR instrument subprocess calls. Run `--audit`. Confirm zero outbound connections, zero subprocess invocations (especially yt-dlp).
20. **Failure isolation.** Make `videos/` unreadable for one creator (`chmod 000`). Run `--audit` across all creators. Confirm that creator's audit fails with the documented error, but other creators are audited normally. Exit code is `0`.
21. **Multi-creator total.** Three creators: each with 1 Class A. Run `--audit`. Confirm final total line reads `Total: 3 Class A, 0 Class B, 0 Class C, 0 Class D, 0 Class E across 3 creators`.
22. **Backup filename collision.** Run `--audit --repair`, then immediately again (within the same second). Confirm the second run produces `archive.txt.pre-repair-<ts>-2` rather than overwriting the first backup.

These map onto no specific verification items from the original plan (PRD 8 is a new addition); the success metrics above are the complete verification surface.

## 9. Open Questions

1. **Should we auto-prune `archive.txt.pre-repair-*` backups older than N days?** Currently unbounded. Add `--prune-backups` flag later if accumulation becomes a problem.
2. **Should `--audit` also detect leftover `.pre-upgrade` files from interrupted PRD 6 runs?** PRD 6's startup recovery (PRD 6 §4.2) already handles this. Auditing it would be redundant but cheap. Defer.
3. **Should `--audit` validate that the `<ID>` in `archive.txt` actually corresponds to a YouTube video** (via a lightweight format check)? Currently §4.3 #14 enforces format only. To check actual existence on YouTube would require network calls (out of scope).
4. **Should orphan-on-disk (Class B) detection also report the folder's contents** (so the user can decide whether to keep or delete)? Currently we just name the folder. Adding "contains: <ID>.mkv (412MB), <ID>.info.json, <ID>.webp" lines might be useful but verbose. Defer.
5. **Should the audit produce a structured (JSON) report** alongside the human-readable one for tooling consumption (e.g., a Grafana dashboard)? Out of scope for v1 (per Q3 = A). Could be added as `--audit --json` later.
6. **Should `--audit` cross-check `creator.json` exists for every creator with a populated `videos/` directory**? Marginal value; `creator.json` missing just means Pass 1 never completed successfully, which is its own diagnostic. Defer.
7. **Should `--audit` validate that `manifests/` directory contents are reasonable** (e.g., at least one `playlists_index_*.json` exists if there are videos)? Out of scope; manifests are snapshots, not invariants.
