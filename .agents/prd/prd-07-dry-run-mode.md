# PRD 07 — Dry-Run Mode

## 1. Introduction / Overview

This PRD adds the `--dry-run` flag — a cross-cutting "show me what would happen, but don't do it" mode that composes with every command path already built by PRDs 2 through 6. It is the **last** PRD by design: it has to hook into every disk-writing and media-downloading operation in the codebase, so it depends on those operations existing first.

The hard rule of dry-run: **no files on disk are created, modified, or deleted**, and **no per-creator log files are written** (nothing in `download.log`, `manifests.log`, `errors.log`, `refresh.log`, `upgrade.log`, `audit.log`). The only side effect is human-readable output to stdout (and yt-dlp's read-only network calls that fetch metadata but don't download media).

Dry-run is **accurate, not just informational**. For default invocations, Pass 1's manifest network calls still happen so newly-uploaded videos appear in the preview. For `--refresh-metadata`, each video's yt-dlp `--dump-json` call still happens so real diffs can be shown. Only the *writes* and *downloads* are suppressed. Per Q1=A, Q2=A.

See [`youtube-archive-plan.md`](../youtube-archive-plan.md) and [`prd-overview.md`](../prd-overview.md). PRD 7 owns verification item #5 and the dry-run portions of #14.

## 2. Goals

1. `uv run archive.py --dry-run` on a creator with new videos lists the IDs that would download (and the IDs that would be gated for live-stream, premiere, or upload-age reasons), with no files modified on disk anywhere.
2. `uv run archive.py --dry-run --refresh-metadata` shows the actual proposed diffs for each video (title changes, thumbnail changes, availability flips, upgrade_available transitions), with no `metadata.json` written.
3. `uv run archive.py --dry-run --upgrade` lists which videos would be re-downloaded and what the recorded `upgrade_available` descriptor says the upgrade would be.
4. `uv run archive.py --dry-run --audit` runs PRD 8's audit — which is read-only anyway — and confirms no log files were written (audit normally writes to `audit.log`; in dry-run, that's also suppressed).
5. Every dry-run invocation produces output that's both glance-able (per-creator summary) and actionable (per-video detail list).
6. Re-running the same command without `--dry-run` afterward produces the exact same set of changes the dry-run predicted (modulo race conditions on YouTube's side between the two runs).

## 3. User Stories

- **As the developer**, before kicking off a multi-hour backfill for a new creator, I want to see how many videos would download and what's gated for upload-age or live-stream reasons.
- **As the developer**, before running `--refresh-metadata` for the first time in a month, I want to preview how many metadata changes are about to land so I'm not surprised when `history` arrays balloon.
- **As the developer**, before running `--upgrade` (which is destructive and bandwidth-heavy), I want to confirm which videos will be replaced and with what format.
- **As the developer running scheduled cron jobs**, I want a "preview" mode that exits cleanly with zero side effects so I can include it in CI smoke tests of my config without polluting the archive.
- **As the developer debugging an unexpected outcome**, I want dry-run to reach the same decisions as a real run (same eligibility, same filtering, same orderings) so I can use it as a diagnostic tool.

## 4. Functional Requirements

### 4.1 CLI surface

1. `--dry-run` is a boolean CLI flag, already declared in PRD 1 §4.19 #19. PRD 7 wires up its behavior.
2. `--dry-run` MUST be composable with every other flag: alone, with `--manifests-only`, with `--refresh-metadata`, with `--upgrade`, with `--audit`, and with `--creator <slug>`.
3. If `--dry-run` is combined with multiple mode flags (e.g., `--dry-run --refresh-metadata --upgrade`), each mode runs in dry-run sequentially in the same order the real invocation would (refresh first, then upgrade). Each mode produces its own output section.
4. The default invocation (`--dry-run` with no other mode flag) dry-runs the Pass 1 + Pass 2 + PRD 4 sequence.
5. `--audit --dry-run` is allowed — PRD 8 is already read-only, so dry-run just additionally suppresses the `audit.log` write.

### 4.2 Universal dry-run rules

When `--dry-run` is in effect, the following hold across the entire program execution:

6. **No files written to `data/`**. Every code path that writes to a file under `data/` MUST check the dry-run flag and skip the write. This includes:
    - `creator.json` (PRD 2 §4.2)
    - `manifests/channel/playlists_index_*.json` (PRD 2 §4.3)
    - `manifests/playlists/<PLAYLIST_ID>_*.json` (PRD 2 §4.5)
    - `manifests/channel/uploads_*.json` (PRD 2 §4.6)
    - `archive.txt` (PRD 3 §4.5 — yt-dlp's `--download-archive` only writes on download; since downloads are skipped, this is naturally suppressed)
    - Media files, `info.json`, thumbnails, subtitles in `videos/<ID>/` (PRD 3 §4.6 — naturally suppressed since yt-dlp isn't invoked)
    - `metadata.json` (PRD 4 §4.7, PRD 5 §4.10, PRD 6 §4.8)
    - `.pre-upgrade` renames or restorations (PRD 6 §4.5)
    - Per-creator log files: `download.log`, `manifests.log`, `errors.log`, `refresh.log`, `upgrade.log`, `audit.log` (all PRDs)
7. **Directory creation MAY happen.** PRD 1 §4.6 creates `data/<slug>/{videos,manifests/{playlists,channel},logs}` on startup. Suppressing directory creation under dry-run would mean we can't show what the layout *would* look like, and empty directories are harmless. So `Path.mkdir(parents=True, exist_ok=True)` calls are NOT suppressed.
8. **yt-dlp read-only network calls SHOULD happen.** Specifically:
    - PRD 2's manifest discovery (`yt-dlp --flat-playlist --dump-single-json`) for accurate candidate listings.
    - PRD 3's fallback timestamp/live-status fetch (PRD 3 §4.10) for accurate eligibility classification.
    - PRD 5's per-video `yt-dlp --skip-download --dump-json` for accurate diff computation.
9. **yt-dlp download calls MUST NOT happen.** Specifically:
    - PRD 3's media download (PRD 3 §4.16) is skipped entirely; the script prints "would download" lines instead.
    - PRD 6's upgrade re-download (PRD 6 §4.14 #3) is skipped entirely.
10. **Logger calls MUST be redirected to no-op.** The per-creator loggers from PRD 1 §4.31 MUST detect dry-run and drop all log records on the floor (no file handler attached, or a no-op handler). Alternative: PRD 7 swaps in a `logging.NullHandler` for all per-creator loggers when dry-run is set. This guarantees that any `INFO`/`WARN`/`ERROR` call from any PRD's code path produces no log file write.
11. **All output goes to stdout** via `print(..., flush=True)`. stderr is reserved for unexpected exceptions only.
12. **Exit code is 0** for any successful dry-run, regardless of what changes are detected. Dry-run is purely informational; "would do work" is not a failure.

### 4.3 Dry-run for default invocation (Pass 1 + Pass 2 + PRD 4)

13. For each creator (or the one named by `--creator`), PRD 7 MUST:
    1. Print `==> <slug> (DRY RUN)` to stdout.
    2. Run PRD 2's Pass 1 normally — fetch manifests, parse them, build the in-memory data structure (PRD 2 §4.7) — but with all on-disk writes suppressed per §4.2 #6.
    3. Print a per-creator section header for Pass 1 output (§4.8).
    4. Run PRD 3's Pass 2 in dry-run: filter candidates against `archive.txt`, classify for live-status and upload-age (using PRD 3 §4.3 logic), apply oldest-first sort. Do NOT invoke yt-dlp's download command. For each candidate, print one line categorizing it: "would download", "gated (live stream in progress)", "gated (upcoming premiere)", "gated (future release: <ts>)", "gated (upload age <N>h < min_upload_age_hours=<M>)", or "already archived (skip)".
    5. Run PRD 4's metadata pass in dry-run: walk `archive.txt`, for each video compute what `metadata.json` *would* look like after the refresh. Don't write. Report aggregate counts only (per-video detail for playlists changes is too noisy for typical archives).
    6. Print the per-creator summary line.
14. Channel-level failures from Pass 1 (PRD 2 §4.9) MUST be reported in the dry-run output: `Pass 1: CHANNEL-LEVEL FAILURE — would skip Pass 2 and PRD 4 for this creator`. Continue to the next creator.

### 4.4 Dry-run for `--refresh-metadata`

15. When `--dry-run --refresh-metadata` is set, PRD 7 MUST:
    1. Print `==> <slug> (DRY RUN --refresh-metadata)` per creator.
    2. Run PRD 5's per-video logic against every ID in `archive.txt`: make the yt-dlp `--skip-download --dump-json` call (with format flags so upgrade detection works), classify availability, compute the new `current.*` values, compute the new `upgrade_available` descriptor.
    3. For each video where ANY change would be written to `metadata.json`, print one line summarizing the change. See §4.8 for output format.
    4. For videos with no change, do NOT print per-video lines (they'd flood the output). They are counted in the summary.
    5. For transient errors (PRD 5 §4.16), print one line per affected video (`<ID> — transient error: <one-line stderr>`) and count in the summary.
    6. Print the per-creator summary line.

### 4.5 Dry-run for `--upgrade`

16. When `--dry-run --upgrade` is set, PRD 7 MUST:
    1. Print `==> <slug> (DRY RUN --upgrade)` per creator.
    2. Run PRD 6's startup recovery sweep (PRD 2 §4.2) in dry-run: identify `.pre-upgrade` leftovers, classify Case A / B / C, but do NOT restore or delete anything. Print what would happen.
    3. Walk `metadata.json` files to find non-null `upgrade_available` targets (PRD 6 §4.3). Apply the availability filter (skip `availability != "available"`). Sort by `detected_at` ascending.
    4. For each target, print one line: `  <VIDEO_ID> — would upgrade <from format_id>/<from height>p (<from vcodec>) -> <to format_id>/<to height>p (<to vcodec>)` using the recorded `upgrade_available` descriptor and the current `downloaded` block. Per Q3=A: no network calls.
    5. Do NOT invoke yt-dlp. Do NOT rename any files to `.pre-upgrade`.
    6. Print the per-creator summary line.

### 4.6 Dry-run for `--manifests-only`

17. When `--dry-run --manifests-only` is set, PRD 7 MUST:
    1. Print `==> <slug> (DRY RUN --manifests-only)` per creator.
    2. Run PRD 2's Pass 1 with writes suppressed (same as §4.3 #13.2).
    3. Print the Pass 1 output section (§4.8). Do NOT print Pass 2 or PRD 4 sections.
    4. Print a per-creator summary line: `<slug>: --manifests-only — would write N manifest files`.

### 4.7 Dry-run for `--audit`

18. When `--dry-run --audit` is set, PRD 8's audit runs as it normally would (read-only by design), but the `audit.log` write is suppressed. The discrepancy report still prints to stdout exactly as it does in a real audit run.
19. `--dry-run --audit --repair` MUST behave as `--dry-run --audit` would (no `archive.txt` modifications); the `--repair` flag is acknowledged but its mutations are suppressed. Print a `[DRY RUN]` prefix on the lines that describe what `--repair` would do.

### 4.8 Output format

20. Each creator gets a section starting with a header line:
    ```
    ==> <slug> (DRY RUN[<mode suffix>])
    ```
    where `<mode suffix>` is one of: `` (empty, for default), ` --refresh-metadata`, ` --upgrade`, ` --manifests-only`, ` --audit`, ` --refresh-metadata --upgrade`, etc. — matching the actual flags passed.
21. The per-creator section body uses **two-space indented bullets** for per-item detail. Section headers within a creator (e.g., `Pass 1:`, `Pass 2 (would download):`) use no indent but are visually grouped under the creator header.
22. Default invocation per-creator output template:
    ```
    ==> <slug> (DRY RUN)
      Pass 1 (manifest discovery):
        canonical URL: https://www.youtube.com/channel/UC...
        playlists discovered: 5
        final playlist set: 5 (discovered: 5, extra: 0, excluded: 0)
        per-playlist manifests: 5 would be written (5 succeeded, 0 failed)
        uploads tab: 142 videos
        candidates after union: 143 unique video IDs
      Pass 2 (eligibility):
        abc123 — would download (oldest first; uploaded 2022-03-14)
        def456 — would download (uploaded 2024-11-02)
        ghi789 — gated (upload age 12h < min_upload_age_hours=48)
        jkl012 — gated (upcoming premiere: 2026-05-30T15:00:00Z)
        139 already archived (skip)
      PRD 4 (metadata.json):
        142 metadata.json files would be touched (playlists field refresh)
      Summary: 2 would download, 2 gated, 139 already archived
    ```
23. `--refresh-metadata` per-creator output template:
    ```
    ==> <slug> (DRY RUN --refresh-metadata)
      abc123 — would update: title changed ("Old Title" -> "New Title")
      def456 — would update: thumbnail_url changed
      ghi789 — would update: description_hash changed; title changed ("..." -> "...")
      jkl012 — would update: availability changed (available -> removed)
      mno345 — would update: upgrade_available newly populated (height 1080 -> 2160)
      pqr678 — would update: upgrade_available cleared (was: height 2160, format 337)
      stu901 — transient error: HTTP 503
      135 no changes (skip)
      Summary: 142 checked, 5 metadata changes, 1 availability change, 1 upgrade newly available, 1 upgrade cleared, 1 transient error, 0 skipped
    ```
24. `--upgrade` per-creator output template:
    ```
    ==> <slug> (DRY RUN --upgrade)
      startup recovery: 0 .pre-upgrade leftovers found
      abc123 — would upgrade 1080p (format 248, vp09.00.40.08) -> 2160p (format 337, vp09.00.50.08); detected 2026-05-12
      def456 — would upgrade 720p (format 22, avc1.64001F) -> 1080p (format 248, vp09.00.40.08); detected 2026-05-19
      ghi789 — skipped (availability: removed)
      Summary: 2 targets, would attempt upgrade on 2, 1 skipped
    ```
25. Final total line printed after all creators:
    ```
    Total: <aggregated counts matching the mode> across <N> creators
    ```
26. If `--creator` was used, `<N>` is `1` for uniformity. If no work was found anywhere, the total line still prints (e.g., `Total: 0 would download, 0 gated, 0 already archived across 1 creator`).
27. Per-creator lines for per-video output use the video ID first, then ` — `, then the action description. Consistent across all modes for grep-ability.

### 4.9 Logging suppression

28. PRD 1's `get_creator_loggers` helper (or equivalent) MUST be extended to accept a `dry_run: bool` parameter. When true, the returned loggers MUST attach a `logging.NullHandler` instead of the usual `FileHandler`(s), so all `INFO`/`WARN`/`ERROR` calls become no-ops.
29. The dry-run flag MUST be set at the top level (in `argparse` parsing) and passed through to `get_creator_loggers` and any subprocess helper that writes to log files.
30. `errors.log` is also suppressed under dry-run. Unexpected exceptions in dry-run mode print to stderr instead, with `[DRY RUN]` prefix.
31. yt-dlp's stdout/stderr from read-only calls (manifest fetches, refresh calls) are still captured by the subprocess helper, but in dry-run mode they're discarded after parsing (not written to any log file). The parsed JSON data is what matters; the verbose progress lines are noise.

## 5. Non-Goals (Out of Scope)

- **A `--dry-run --verbose` flag** for even more detailed output (e.g., showing the full proposed `metadata.json` diff). Defer; current verbosity (per-video lines) is enough for v1.
- **A `--dry-run --quiet` flag** for summary-only output. If you only want summaries, redirect to grep or `tail`.
- **Structured (JSON) output** for tool consumption. v1 is human-readable text only.
- **Exit codes that differ based on whether changes were detected.** Dry-run always exits 0; if you want "would anything change?" as a boolean for cron, parse the summary line.
- **A `--dry-run-write-to <file>` option** to capture dry-run output. Use `tee` or shell redirection.
- **Cross-creator output aggregation beyond the final total line.** Each creator is its own section.
- **Showing what `metadata.json` files would look like in full** (just listing the changed fields). The point of dry-run is preview, not diff exploration.
- **Per-creator log files in a "dry-run dump" mode** for forensic investigation. If you need this level of detail, run the real command.
- **Suppressing manifest discovery network calls** in default dry-run (we deliberately make them per Q1=A).
- **Suppressing per-video refresh network calls** in `--dry-run --refresh-metadata` (deliberately made per Q2=A).
- **Making upgrade dry-run more accurate by pre-flighting refresh calls** (per Q3=A — trust recorded `upgrade_available`).

## 6. Design Considerations

- **The dry-run flag is plumbing.** Every PRD that writes to disk or invokes yt-dlp's downloader must check this flag at the point of writing/downloading. It's a cross-cutting concern with no single home other than "every relevant code path." When implementing, the cleanest pattern is to pass a `dry_run: bool` (or a `Config` object containing it) through the call graph.
- **Why directory creation isn't suppressed.** Creating empty directories is harmless, and skipping it would mean writes that the dry-run logic missed would fail with `FileNotFoundError` instead of being silently no-op'd. Letting directories exist makes the "no-write" pattern slightly more forgiving.
- **Why log writes are suppressed even though `download.log`/etc. would otherwise contain useful detail.** Two reasons: (1) the user explicitly asked for "no per-creator log file writes"; (2) the same information ends up in stdout output, so logs would be redundant.
- **Why `--dry-run --refresh-metadata` still makes network calls.** Without them, we can't show real diffs — the user would see "0 metadata changes" every time, which is misleading. Network cost is the same as a real refresh; that's the cost of accuracy.
- **Why `--dry-run --upgrade` doesn't make network calls.** PRD 6 itself doesn't pre-flight before upgrading (per Q3 there) — dry-run is consistent with that. If the user wants fresh upgrade detection, they run `--dry-run --refresh-metadata` first (or just `--refresh-metadata` for real).
- **Output is to stdout, not stderr.** Dry-run is the primary output of the program in this mode, not a diagnostic. stderr is reserved for unexpected exceptions.
- **`flush=True` on every print.** Lets the user pipe dry-run to `less` or `tee` interactively, with output appearing as work progresses rather than buffered.
- **No progress indicator.** Dry-run for a large refresh can take minutes (network-bound). We don't add a progress bar in v1 — keep output deterministic and tool-friendly. If the user wants progress, the per-video lines as they print serve as one.

## 7. Technical Considerations

- **PRD dependencies (heavy):**
  - PRD 1: extends `get_creator_loggers` to accept `dry_run`; the subprocess helper similarly needs to know.
  - PRD 2: every write call site (`creator.json`, manifest snapshots) needs a `dry_run` guard.
  - PRD 3: download invocation needs a `dry_run` guard; the dry-run path prints `INFO` to stdout instead.
  - PRD 4: every `metadata.json` write call site needs a `dry_run` guard.
  - PRD 5: every `metadata.json` write call site needs a `dry_run` guard; the refresh-call path still runs.
  - PRD 6: download invocation AND every `.pre-upgrade` rename / file delete needs a `dry_run` guard; the recovery sweep classifies but doesn't act.
  - PRD 8: `audit.log` write needs a `dry_run` guard; the `--repair` path's `archive.txt` modification needs one too.
- **Implementation pattern:** rather than scatter `if dry_run: ...` everywhere, factor the writes into helpers (e.g., `write_json_atomic(path, obj, dry_run=False)`) that check the flag once. Same for the subprocess download invocations.
- **Atomic write helper:** the helper from PRD 4 §7 that does `write_json_atomic` should grow a `dry_run` parameter. When set, it returns immediately without touching the filesystem. All callers in PRDs 2, 4, 5, 6 use it.
- **Logger swap:** at the top of each creator's processing loop, when dry-run is set, call `get_creator_loggers(slug, dry_run=True)` instead of `get_creator_loggers(slug)`. The function returns loggers whose handlers are `NullHandler` — all log lines silently dropped.
- **No console output from log helpers in dry-run.** PRD 1's `INFO`/`WARN`/`ERROR` log helpers (if they also print to console for some classes of message) must check dry-run to avoid duplicating output that PRD 7 is already producing.
- **stdout buffering:** Python's `print(..., flush=True)` works on all platforms. If we ever switch to `logging` for stdout (we shouldn't, for dry-run), be careful with handler flushing.
- **`--dry-run --refresh-metadata --upgrade`:** when both flags are set, run refresh's dry-run first (showing what would change in `metadata.json`), then upgrade's dry-run (showing what would be re-downloaded based on the *pre-refresh* `upgrade_available` — not the post-refresh hypothetical). This matches the real-run semantics: refresh runs first, populates `upgrade_available`, then upgrade reads what's on disk.

## 8. Success Metrics

This PRD is done when **all** of the following are true:

1. **Default dry-run on a creator with new videos (verification item #5).** For a creator with at least one not-yet-archived video, `uv run archive.py --dry-run --creator <slug>` produces output listing the candidate IDs that would download. Verify by comparing filesystem state before and after — every file in `data/<slug>/` has byte-identical content and unchanged mtime (use `stat` or sha256 directory tree hash).
2. **Default dry-run with no new videos.** Same creator, second invocation: output should say `0 would download, 0 gated, N already archived`. Still zero filesystem changes.
3. **Manifests-only dry-run.** `uv run archive.py --dry-run --manifests-only --creator <slug>` produces Pass 1 output and exits. Filesystem unchanged. `creator.json`, manifest files, log files: all untouched.
4. **Refresh-metadata dry-run.** `uv run archive.py --dry-run --refresh-metadata --creator <slug>` on a creator with at least one video whose title was manually edited in `metadata.json` shows that video flagged with `would update: title changed (...)`. After the run, `metadata.json` still has the manually-edited (wrong) title — no rewrite occurred.
5. **Upgrade dry-run (verification item #14 dry-run portion).** Manually populate one video's `upgrade_available` with a real descriptor. Run `uv run archive.py --dry-run --upgrade --creator <slug>`. Output lists that video with the documented `would upgrade ...` line. The media file's sha256 is unchanged. No `.pre-upgrade` files appear.
6. **Audit dry-run.** `uv run archive.py --dry-run --audit --creator <slug>` produces audit output to stdout. The `audit.log` file is NOT created/modified.
7. **Audit + repair dry-run.** Set up an orphan in `archive.txt`. Run `uv run archive.py --dry-run --audit --repair`. Output shows `[DRY RUN] would prune from archive.txt: <ID>`. After the run, `archive.txt` is byte-identical to before.
8. **Multi-flag composition.** `uv run archive.py --dry-run --refresh-metadata --upgrade` runs both modes' dry-runs sequentially, each producing its own per-creator section. Final filesystem state is unchanged.
9. **Per-creator log files untouched.** After any dry-run invocation, every per-creator log file (`download.log`, `manifests.log`, `errors.log`, `refresh.log`, `upgrade.log`, `audit.log`) has the same mtime as before. Verify with `find data/ -name '*.log' -newer <reference-file>`.
10. **Stdout output is the only side effect.** Diff filesystem state pre- and post-run (use `find . -newer <ref> -type f | xargs sha256sum` or similar). The diff should be empty.
11. **Channel-level failure reported, not silenced.** Configure a creator with a bogus `channel_url`. Run `--dry-run`. Output includes `Pass 1: CHANNEL-LEVEL FAILURE — would skip Pass 2 and PRD 4 for this creator`. No per-creator log file is written for that creator's failure.
12. **Refresh dry-run uses network calls.** Run `--dry-run --refresh-metadata` against a creator with 10 videos. Confirm via network monitoring (e.g., `lsof -i` snapshots, or counting yt-dlp invocations in stdout via instrumentation) that ~10 yt-dlp calls were made.
13. **Upgrade dry-run uses NO network calls.** Run `--dry-run --upgrade` against a creator with 5 upgrade targets. Confirm zero yt-dlp invocations (other than what the recovery sweep might do, which is zero).
14. **Exit code is 0** for every dry-run invocation, including ones that report changes would be made.
15. **Real run produces what dry-run predicted.** Sequence: (a) run `--dry-run` and capture the would-download list; (b) run the real command immediately after; (c) compare the actually-downloaded videos against the dry-run prediction. Subset relationship should hold (the real run can be a strict subset if YouTube state changed between runs; in steady state with no concurrent changes, they should be identical).
16. **`==> <slug> (DRY RUN...)` header always present** for every creator processed, even ones with no work to report.

These map onto verification item **#5** (dry-run on a creator with new videos — full coverage) and the dry-run portions of **#14** (upgrade dry-run lists targets without touching files) from the plan.

## 9. Open Questions

1. **Should dry-run output be colorized** (e.g., green for "would download", yellow for "gated", red for "transient error") when stdout is a TTY? Nice for interactive use, but adds complexity. Defer.
2. **Should we add a `--dry-run --since <timestamp>` filter** to limit `--refresh-metadata` dry-run to videos checked before that time? Useful for "what's about to expire?" patterns. Defer.
3. **What if a user runs `--dry-run` on a creator that was never archived at all** (no `data/<slug>/` exists)? Currently the directory-creation behavior (§4.7) would create empty dirs. We could detect "fresh creator" and report differently. Currently the answer is just "Pass 2 sees zero candidates already archived" — works but maybe could be friendlier.
4. **Should we cap per-video output at N lines** (e.g., "and 850 more") to keep output manageable for huge backfills? Currently we list everything. If a 5000-video first-run dry-run produces 5000 lines, that's a lot. Could add `--max-lines` to compact.
5. **Should dry-run record what it *would have done* to a temp file** (e.g., `/tmp/youtube-archive-dry-run.json`) so a later real run could pick up where dry-run left off? Out of scope for v1 — would require deduping logic and adds state. Just re-run without `--dry-run`.
6. **Stdout vs separate `--dry-run --output-file <path>`.** Currently stdout only. Shell redirection works fine; no need to add CLI surface.
