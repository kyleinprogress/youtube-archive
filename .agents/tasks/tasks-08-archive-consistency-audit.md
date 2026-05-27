## Relevant Files

- `archive.py` - `--audit` code path (archive.txt parsing, disk scan, five discrepancy classes A–E with priority A > C > D, `--repair` action for Class A only, timestamped backup, atomic rewrite, console + audit.log output, dry-run interaction) is added to the single-file orchestrator from PRDs 1–7.
- `data/<slug>/archive.txt` - PRD 8 reads this for the expected set; `--repair` rewrites it atomically (tempfile + `os.replace`) to remove Class A entries. Backup is written first per §4.7.
- `data/<slug>/archive.txt.pre-repair-<YYYYMMDDTHHMMSSZ>` - Timestamped backup created before any `--repair` modification. Never auto-pruned.
- `data/<slug>/videos/<ID>/` - Scanned by the disk-scan phase; classification reads `<ID>.<resolved merge_output_format>`, `<ID>.info.json`, and `metadata.json` (no media file open / hashing).
- `data/<slug>/logs/audit.log` - **New per-creator log file** introduced by this PRD. Created empty by PRD 1's helper (extended here to expose a sixth log file).

### Notes

- Stdlib only: `pathlib`, `re`, `json`, `shutil` (for `copy2`-preserving-mtime backup), `os` (for `os.replace`), `datetime`.
- Reuse PRD 1's `get_creator_loggers(slug)` (now extended for `audit.log`), `creator_scope(slug)`.
- Reuse PRD 3 §4.5's per-creator `merge_output_format` resolution for the canonical-media-file extension.
- Reuse PRD 7's `dry_run` plumbing — `--audit` is already read-only, but the `audit.log` write and the `--repair` mutation need explicit `dry_run` guards.
- No network calls of any kind. No yt-dlp invocation.
- Performance target: sub-second for a 1000-video-per-creator audit on any reasonable disk.
- Within each class, IDs are reported in `archive.txt` line order (Classes A, C, D, E) or directory iteration order (Class B). Stable across runs.
- No automated test suite is in scope; verification is the success-metrics walkthrough in parent task 6.0.

## Instructions for Completing Tasks

**IMPORTANT:** As you complete each task, you must check it off in this markdown file by changing `- [ ]` to `- [x]`. This helps track progress and ensures you don't skip any steps.

Example:
- `- [ ] 1.1 Read file` → `- [x] 1.1 Read file` (after completing)

Update the file after completing each sub-task, not just after completing an entire parent task.

## Tasks

- [x] 0.0 Create feature branch
  - [x] 0.1 Confirm the PRD 07 feature branch has been merged into `main` (or is otherwise reflected there); resolve any outstanding state first
  - [x] 0.2 Create and checkout a new branch: `git checkout -b feature/archive-consistency-audit`

- [x] 1.0 Wire `--audit` as a standalone read-only code path and add the sixth per-creator log file
  - [x] 1.1 Extend PRD 1's `get_creator_loggers(slug)` to return a sixth logger backed by `data/<slug>/logs/audit.log`; touch the file in PRD 1's directory-setup step so it exists empty after the first PRD 8 run
  - [x] 1.2 Add `--audit` handling to the main CLI dispatcher so it skips Pass 1 (PRD 2), Pass 2 (PRD 3), PRD 4, PRD 5, and PRD 6 entirely
  - [x] 1.3 Reject `--repair` without `--audit`: per PRD 1 §4.6 #24 (already validated there) — exit code `2` with message `error: --repair requires --audit`
  - [x] 1.4 Allow composition with `--creator <slug>` (scope to one creator) and with `--dry-run` (PRD 7 territory; ensure `--audit` checks the flag)
  - [x] 1.5 Wrap each creator's audit work in `creator_scope(slug)` so uncaught exceptions log to `errors.log` and processing continues with the next creator
  - [x] 1.6 At the top of each creator's audit, emit `INFO audit starting` to `audit.log` (unless `dry_run`); at the bottom emit `INFO audit complete: A=<n> B=<n> C=<n> D=<n> E=<n>`
  - [x] 1.7 Audit is read-only with respect to network — no yt-dlp calls, no HTTP, no DNS

- [x] 2.0 Parse archive.txt and run the disk scan with the five-class taxonomy
  - [x] 2.1 Read `data/<slug>/archive.txt` line by line, preserving 1-indexed line numbers. Tolerate empty lines and `#` comments (skip without flagging)
  - [x] 2.2 Validate each non-empty, non-comment line against the regex `^youtube [a-zA-Z0-9_-]{11}$`. Failures are **Class E (malformed archive.txt line)**: record the line number and the full line content. Log `WARN class_e malformed_archive_line: line N ("<full line content>")` to `audit.log`
  - [x] 2.3 Build the expected-set of valid video IDs from passing lines. If `archive.txt` does not exist, treat the expected set as empty (not an error)
  - [x] 2.4 List `data/<slug>/videos/` and identify all subdirectories whose name matches `^[a-zA-Z0-9_-]{11}$`. Ignore subdirectories with other names. Skip symlinks with one `INFO symlink skipped: <path>` line
  - [x] 2.5 Resolve the canonical media file extension from the creator's resolved `merge_output_format` (same precedence as PRD 3 §4.5)
  - [x] 2.6 For each ID in the expected set, classify per priority A > C > D (each ID reported in at most one class):
    - **Class A (orphan in archive.txt)** if any of: folder does not exist; folder exists but canonical media file `<ID>.<ext>` is missing; canonical media file exists with size 0. The folder-missing and media-missing cases produce different reason strings (`folder missing`, `media file missing (folder + info.json present)`, `media file missing`, `media file zero-size`)
    - **Class C (incomplete folder)** if the folder exists AND canonical media is present-and-nonzero AND `<ID>.info.json` is missing OR a `<ID>.<ext>.part` resume marker exists alongside complete media. Distinct reason strings per sub-case
    - **Class D (invalid metadata.json)** only if folder + media + info.json all exist (see 3.0 for detail)
  - [x] 2.7 For each video folder ON disk whose ID is NOT in the expected set AND whose canonical media file is present-and-nonzero, classify as **Class B (orphan on disk)**. Log at `INFO` level (NOT `WARN`) — orphan-on-disk often represents legitimate deferred-reconciliation state
  - [x] 2.8 Maintain per-class counters and per-class entry lists (with reason strings + line numbers for Class E) for the console + log output

- [x] 3.0 Implement Class D metadata.json validation
  - [x] 3.1 For each Class D candidate (ID in archive.txt, folder + media + info.json all present), check `metadata.json` existence; if missing → Class D with reason `missing`
  - [x] 3.2 Read and `json.loads(...)`. On `json.JSONDecodeError` → Class D with reason `unparseable`. On `OSError` (permissions / IO) → Class D with reason `unreadable`
  - [x] 3.3 Check `parsed.get("schema_version") == 1`. If not → Class D with reason `schema_version=<actual>` (or `schema_version=missing` if the field is absent)
  - [x] 3.4 Check `parsed.get("video_id") == <folder_name>`. If not → Class D with reason `video_id_mismatch (got <actual>, folder is <folder>)`
  - [x] 3.5 If all checks pass, no Class D entry. Light validation only — do NOT validate every field's type or enum values (per Q4=A)
  - [x] 3.6 Emit per-entry log lines to `audit.log` matching the format `WARN class_d invalid_metadata: <VIDEO_ID> (<reason>)`

- [x] 4.0 Implement the `--repair` action (Class A only)
  - [x] 4.1 `--repair` MUST only act when combined with `--audit`; only modify `archive.txt`; never delete files, move files, regenerate `metadata.json`, or take any action on Class B / C / D / E
  - [x] 4.2 If a creator has zero Class A entries, `--repair` is a no-op for that creator: log `INFO repair: 0 orphans to prune`; do NOT write a backup; do NOT modify `archive.txt`
  - [x] 4.3 For each creator with one or more Class A entries: write a backup to `data/<slug>/archive.txt.pre-repair-<YYYYMMDDTHHMMSSZ>` (UTC) using `shutil.copy2` to preserve mtime
  - [x] 4.4 Backup collision: if a backup file for the exact same timestamp already exists, append `-2`, `-3`, etc. (incrementing counter). Never overwrite an existing backup
  - [x] 4.5 Log `INFO repair backup: <path>` to `audit.log`
  - [x] 4.6 Rewrite `archive.txt` excluding the lines for the Class A orphan IDs. Preserve original line order for remaining entries. Write atomically: `archive.txt.tmp` + `os.replace()`
  - [x] 4.7 Log each pruned ID: `INFO repair pruned: <VIDEO_ID> (orphan in archive.txt)` to `audit.log`
  - [x] 4.8 At the end of the `--repair` section, log `INFO repair complete: <N> entries pruned` to `audit.log` and print the console `--repair:` section per PRD 8 §4.8 #37: `backup written: <path>` line, `pruned: <comma-joined IDs>` line, `<N> entries removed from archive.txt` line
  - [x] 4.9 Auto-pruning of old backup files is OUT OF SCOPE — user manages disk space

- [x] 5.0 Implement console + audit.log output, dry-run interaction, and failure isolation
  - [x] 5.1 Per-creator section header: `==> <slug> (AUDIT)` (or `==> <slug> (AUDIT --repair)` when `--repair` is in effect)
  - [x] 5.2 Per-creator body groups discrepancies by class in operational-priority order A → B → C → D → E; empty classes are omitted. Each non-empty class section: `<Class name> (Class <letter>) — <count>:` header followed by indented bullets per entry with reason strings
  - [x] 5.3 Per-creator summary line at the end: `Summary: <A> Class A, <B> Class B, <C> Class C, <D> Class D, <E> Class E`
  - [x] 5.4 No-discrepancy creator output: `==> <slug> (AUDIT)` followed by `  OK — no discrepancies detected` (still log `INFO audit complete: A=0 B=0 C=0 D=0 E=0` to `audit.log`)
  - [x] 5.5 After all creators, print one final `Total: <A> Class A, <B> Class B, <C> Class C, <D> Class D, <E> Class E across <N> creators`. If `--creator` was used, `<N>` is `1`
  - [x] 5.6 `--audit --dry-run`: behaves identically to `--audit` for scanning/reporting; suppress `audit.log` writes (NullHandler swap from PRD 7); console output unchanged
  - [x] 5.7 `--audit --dry-run --repair`: run the audit normally, then in the `--repair` section print `[DRY RUN] would back up archive.txt to data/<slug>/archive.txt.pre-repair-<timestamp>`, `[DRY RUN] would prune from archive.txt: <ID1>, <ID2>`, `[DRY RUN] <N> entries would be removed`. Skip the actual `archive.txt` modification and backup write
  - [x] 5.8 Failure isolation for recoverable per-creator errors:
    - `archive.txt` unreadable: log `ERROR audit failed: archive.txt unreadable — <reason>` to `errors.log`, log to `audit.log`, console summary for that creator is `<slug>: AUDIT FAILED — archive.txt unreadable`; continue to next creator
    - `videos/` directory unreadable: same pattern
    - A single `metadata.json` unreadable due to permissions: classify as Class D with reason `unreadable`; continue with the next ID
  - [x] 5.9 Exit codes: `0` for a successful audit run regardless of discrepancies detected; `2` if `--repair` was passed without `--audit`; `1` only if the audit itself crashed (uncaught exception escaped `creator_scope` for every creator)

- [ ] 6.0 Verify against PRD success metrics
  - [ ] 6.1 Class A — folder missing: add a fake line `youtube zzzzzzzzzzz` to `archive.txt`; run `--audit --creator <slug>`; confirm the report lists `zzzzzzzzzzz — folder missing` under Class A; `audit.log` has `WARN class_a orphan_in_archive: zzzzzzzzzzz (folder missing)`
  - [ ] 6.2 Class A — media file missing: delete the canonical media file for a real archived video (keep `info.json`, `metadata.json`); run `--audit`; confirm Class A entry with reason `media file missing (folder + info.json present)`
  - [ ] 6.3 Class A — zero-size media: `truncate -s 0 <path-to-media>`; run `--audit`; confirm Class A entry with reason `media file zero-size`
  - [ ] 6.4 Class B — orphan on disk: create `data/<slug>/videos/aaaaaaaaaaa/aaaaaaaaaaa.<ext>` (non-zero size, valid ID format) by hand; ensure `aaaaaaaaaaa` is NOT in `archive.txt`; run `--audit`; confirm Class B entry; confirm `audit.log` shows the entry at `INFO` level, NOT `WARN`
  - [ ] 6.5 Class A wins over Class C: create `data/<slug>/videos/bbbbbbbbbbb/bbbbbbbbbbb.info.json` AND `bbbbbbbbbbb.<ext>.part`, add `youtube bbbbbbbbbbb` to `archive.txt`; run `--audit`; confirm the entry is reported as Class A (not Class C) because the canonical media file is missing
  - [ ] 6.6 Class C — pure case: create a folder with media file present (non-zero) AND `info.json` MISSING; add to `archive.txt`; run `--audit`; confirm Class C entry with reason `info.json missing`
  - [ ] 6.7 Class D — missing metadata.json: for a real archived video with media + info.json present, delete `metadata.json`; run `--audit`; confirm Class D entry with reason `missing`
  - [ ] 6.8 Class D — unparseable: replace a video's `metadata.json` with literal `garbage`; run `--audit`; confirm Class D entry with reason `unparseable`
  - [ ] 6.9 Class D — wrong schema_version: edit a `metadata.json` to set `"schema_version": 2`; run `--audit`; confirm Class D entry with reason `schema_version=2`
  - [ ] 6.10 Class D — video_id mismatch: edit a `metadata.json` to set `"video_id": "xxxxxxxxxxx"`; run `--audit`; confirm Class D entry with reason `video_id_mismatch (got xxxxxxxxxxx, folder is <real-folder>)`
  - [ ] 6.11 Class E — malformed archive.txt line: add `garbage`, `youtube xyz`, or `youtube ` to `archive.txt`; run `--audit`; confirm Class E entry with the correct line number; `audit.log` shows `WARN class_e malformed_archive_line: line N (...)`
  - [ ] 6.12 All-OK case: run `--audit` on a clean creator with no issues; per-creator output is `OK — no discrepancies detected`; `audit.log` shows `INFO audit complete: A=0 B=0 C=0 D=0 E=0`
  - [ ] 6.13 `--repair` happy path: start with two Class A discrepancies; run `--audit --repair`; confirm `archive.txt.pre-repair-<timestamp>` exists with the original content; `archive.txt` no longer contains the two orphan IDs; `audit.log` shows the `INFO repair backup`, two `INFO repair pruned`, and `INFO repair complete: 2 entries pruned` lines; console output includes the `--repair:` section
  - [ ] 6.14 `--repair` no-op: run `--audit --repair` against a creator with zero Class A; confirm no backup file is created, `archive.txt` is byte-identical, `audit.log` shows `INFO repair: 0 orphans to prune`
  - [ ] 6.15 `--repair` only touches Class A: set up one of each class (A, B, C, D, E); run `--audit --repair`; confirm only Class A is pruned from `archive.txt`; disk state for Classes B/C/D/E is byte-identical
  - [ ] 6.16 `--repair` without `--audit` rejected: run `uv run archive.py --repair`; confirm exit code `2` and error message `error: --repair requires --audit`
  - [ ] 6.17 `--audit --dry-run`: same setup as 6.13; confirm `audit.log` has NO new entries (mtime check); console output identical to non-dry-run
  - [ ] 6.18 `--audit --dry-run --repair`: same setup; confirm `archive.txt` is unchanged AND no backup file is created AND console output shows the `[DRY RUN] would prune ...` lines
  - [ ] 6.19 No network calls: instrument subprocess calls or use network monitoring during `--audit`; confirm zero outbound connections and zero yt-dlp invocations
  - [ ] 6.20 Failure isolation: `chmod 000` one creator's `videos/` directory; run `--audit` across all creators; confirm that creator fails with the documented error message but other creators are audited normally; exit code 0
  - [ ] 6.21 Multi-creator total: three creators each with 1 Class A; confirm the final total line reads `Total: 3 Class A, 0 Class B, 0 Class C, 0 Class D, 0 Class E across 3 creators`
  - [ ] 6.22 Backup filename collision: run `--audit --repair` twice in rapid succession (same UTC second); confirm the second run produces `archive.txt.pre-repair-<ts>-2` rather than overwriting the first backup
