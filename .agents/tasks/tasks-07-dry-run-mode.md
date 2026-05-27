## Relevant Files

- `archive.py` - `--dry-run` plumbing threads through every disk-writing and media-downloading site introduced by PRDs 1–6 and PRD 8. PRD 7's responsibility is to (a) plumb the flag, (b) suppress writes where appropriate, (c) produce human-readable preview output to stdout for each mode.
- All PRD 2 write call sites (`creator.json`, `manifests/channel/playlists_index_*.json`, `manifests/playlists/<ID>_*.json`, `manifests/channel/uploads_*.json`) - Need a `dry_run` guard.
- All PRD 3 download invocation sites (yt-dlp media download) - Need a `dry_run` guard.
- All PRD 4 / 5 / 6 `metadata.json` write sites - Need a `dry_run` guard.
- All PRD 6 `.pre-upgrade` rename / restore / delete sites - Need a `dry_run` guard.
- All PRD 8 `audit.log` write and `--repair` mutation sites - Need a `dry_run` guard.
- PRD 1's `get_creator_loggers(slug)` helper - Extended to accept `dry_run: bool` and return `logging.NullHandler`-backed loggers when true.
- PRD 4's `write_json_atomic(path, obj)` helper - Extended to accept `dry_run: bool` and return immediately when true.

### Notes

- Stdlib only.
- The hard rule of dry-run: no files in `data/` are created, modified, or deleted (directory `mkdir -p` is allowed per PRD 7 §4.7 — empty directories are harmless), no per-creator log files are written, no media downloads happen.
- Read-only network calls deliberately still happen: Pass 1 manifest fetches, PRD 3's fallback `--print` timestamp/live-status fetch, PRD 5's per-video `--dump-json` refresh call. Without them, dry-run output wouldn't be accurate. (Per PRD 7 §4.2 #8, Q1=A, Q2=A.)
- Upgrade dry-run does NOT make network calls (per Q3=A; matches real `--upgrade`'s no-pre-refresh behavior).
- Output goes to stdout via `print(..., flush=True)`. stderr is reserved for unexpected exceptions.
- Exit code is `0` for any successful dry-run regardless of detected changes.
- Implementation pattern: factor writes into helpers that take `dry_run` once rather than scattering `if dry_run: ...` everywhere.
- No automated test suite is in scope; verification is the success-metrics walkthrough in parent task 6.0.

## Instructions for Completing Tasks

**IMPORTANT:** As you complete each task, you must check it off in this markdown file by changing `- [ ]` to `- [x]`. This helps track progress and ensures you don't skip any steps.

Example:
- `- [ ] 1.1 Read file` → `- [x] 1.1 Read file` (after completing)

Update the file after completing each sub-task, not just after completing an entire parent task.

## Tasks

- [ ] 0.0 Create feature branch
  - [ ] 0.1 Confirm the PRD 06 feature branch has been merged into `main` (or is otherwise reflected there); resolve any outstanding state first
  - [ ] 0.2 Create and checkout a new branch: `git checkout -b feature/dry-run-mode`

- [ ] 1.0 Plumb the `dry_run` flag through the call graph and suppress per-creator log writes
  - [ ] 1.1 Confirm `--dry-run` is already parseable from PRD 1 §4.19; surface its value on a shared context/config object that gets passed into every per-creator run function
  - [ ] 1.2 Extend PRD 1's `get_creator_loggers(slug, dry_run=False)` so that when `dry_run=True`, every returned logger is configured with a `logging.NullHandler` instead of the usual `FileHandler`. Any `INFO`/`WARN`/`ERROR` call from any PRD becomes a no-op
  - [ ] 1.3 Apply the same NullHandler swap to `errors.log` under dry-run; unexpected exceptions print to stderr instead with a `[DRY RUN]` prefix
  - [ ] 1.4 Extend PRD 4's `write_json_atomic(path, obj, dry_run=False)` to return immediately without touching the filesystem when `dry_run=True`. Update all callers in PRDs 2, 4, 5, 6 to pass the flag through
  - [ ] 1.5 Extend PRD 1's subprocess streaming helper to accept `dry_run`; when true, yt-dlp output is still captured (for parsing JSON in refresh calls) but discarded after parsing, not routed to any log file
  - [ ] 1.6 Directory creation (`Path.mkdir(parents=True, exist_ok=True)` from PRD 1 §4.6) is NOT suppressed; empty directories are harmless and ensure "missed" write sites fail-loudly with `FileNotFoundError` rather than silently no-op
  - [ ] 1.7 Verify that `archive.txt` is never written under dry-run (yt-dlp's `--download-archive` only writes on actual downloads, which are themselves suppressed; double-check no other code writes to it)

- [ ] 2.0 Implement dry-run for the default invocation (Pass 1 + Pass 2 + PRD 4)
  - [ ] 2.1 For each selected creator, print `==> <slug> (DRY RUN)` to stdout (replacing the normal `==> <slug>` header from PRD 1)
  - [ ] 2.2 Run PRD 2's Pass 1 normally — fetch manifests over the network, parse them, build the in-memory `candidate_set` — but with every on-disk write suppressed via the helpers from 1.0
  - [ ] 2.3 Print the Pass 1 output section per the template in PRD 7 §4.8 #22: canonical URL, playlists discovered count, final playlist set summary, per-playlist manifest write count (with success/failure breakdown), uploads tab video count, candidate union size
  - [ ] 2.4 Run PRD 3's Pass 2 in dry-run: filter candidates against the in-memory archive.txt set, classify for live-status and upload-age using PRD 3 §4.3 logic (including the fallback `--print` network call when needed), apply oldest-first sort. Do NOT invoke yt-dlp's download command
  - [ ] 2.5 For each candidate, print one indented bullet line with the verdict: `would download (oldest first; uploaded <YYYY-MM-DD>)`, `gated (upload age <N>h < min_upload_age_hours=<M>)`, `gated (upcoming premiere: <ts>)`, `gated (live stream in progress)`, `gated (future release: <ts>)`, or aggregate `<N> already archived (skip)` as a single rollup line at the end
  - [ ] 2.6 Run PRD 4's metadata pass in dry-run: walk `archive.txt`, compute what would be written for each ID, but never call `write_json_atomic`. Report aggregate counts only (per-video detail for playlist changes would flood output): `<N> metadata.json files would be touched (playlists field refresh)`
  - [ ] 2.7 On a Pass 1 channel-level failure, print `Pass 1: CHANNEL-LEVEL FAILURE — would skip Pass 2 and PRD 4 for this creator` and continue to the next creator
  - [ ] 2.8 Print the per-creator summary line `Summary: <X> would download, <Y> gated, <Z> already archived`

- [ ] 3.0 Implement dry-run for `--manifests-only` and `--refresh-metadata`
  - [ ] 3.1 `--dry-run --manifests-only`: print `==> <slug> (DRY RUN --manifests-only)`; run PRD 2's Pass 1 with writes suppressed; print the Pass 1 output section; print `<slug>: --manifests-only — would write N manifest files`
  - [ ] 3.2 `--dry-run --refresh-metadata`: print `==> <slug> (DRY RUN --refresh-metadata)`; run PRD 5's per-video logic against every ID in `archive.txt` — make the yt-dlp `--skip-download --dump-json` call for each (network calls deliberately happen per Q2=A); classify availability; compute new `current.*`; compute new `upgrade_available` descriptor
  - [ ] 3.3 For each video where ANY change would be written to `metadata.json`, print one indented line per the template in PRD 7 §4.8 #23: `<ID> — would update: title changed ("Old" -> "New")`, `<ID> — would update: thumbnail_url changed`, `<ID> — would update: description_hash changed`, `<ID> — would update: availability changed (available -> removed)`, `<ID> — would update: upgrade_available newly populated (height 1080 -> 2160)`, `<ID> — would update: upgrade_available cleared (was: height 2160, format 337)`. Multiple changes on the same video are joined with `; ` on a single line
  - [ ] 3.4 For videos with no change, do NOT print per-video lines; count them in the aggregate `<N> no changes (skip)` rollup line at the end
  - [ ] 3.5 For transient errors (PRD 5 §4.16), print one line per affected video: `<ID> — transient error: <one-line stderr>`
  - [ ] 3.6 Print the per-creator summary line: `Summary: <C> checked, <M> metadata changes, <A> availability changes, <U_new> upgrade newly available, <U_cleared> upgrade cleared, <X> transient errors, <S> skipped`

- [ ] 4.0 Implement dry-run for `--upgrade` and `--audit`
  - [ ] 4.1 `--dry-run --upgrade`: print `==> <slug> (DRY RUN --upgrade)`; run PRD 6's startup recovery sweep in dry-run — identify `.pre-upgrade` leftovers and classify Case A / B / C, but do NOT restore or delete anything. Print what would happen, e.g., `startup recovery: 0 .pre-upgrade leftovers found` or `startup recovery: would restore <ID> from .pre-upgrade (Case A)` / `startup recovery: would skip <ID> (Case B ambiguous)`
  - [ ] 4.2 Walk `metadata.json` files to find non-null `upgrade_available` targets; apply the availability filter; sort by `detected_at` ascending
  - [ ] 4.3 For each target, print one indented line: `<VIDEO_ID> — would upgrade <from format_id>/<from height>p (<from vcodec>) -> <to format_id>/<to height>p (<to vcodec>); detected <YYYY-MM-DD>`. For unavailable-skipped: `<VIDEO_ID> — skipped (availability: <state>)`
  - [ ] 4.4 Do NOT invoke yt-dlp. Do NOT rename any files to `.pre-upgrade`. No network calls (per Q3=A)
  - [ ] 4.5 Print the per-creator summary: `Summary: <T> targets, would attempt upgrade on <N>, <K> skipped`
  - [ ] 4.6 `--dry-run --audit`: PRD 8's audit is already read-only, so just additionally suppress the `audit.log` write (via the NullHandler swap from 1.0). Console output is unchanged from a real audit run
  - [ ] 4.7 `--dry-run --audit --repair`: run the audit normally; in the `--repair` section print `[DRY RUN] would back up archive.txt to data/<slug>/archive.txt.pre-repair-<timestamp>`, `[DRY RUN] would prune from archive.txt: <ID1>, <ID2>`, `[DRY RUN] <N> entries would be removed`. Skip the actual `archive.txt` modification and backup write
  - [ ] 4.8 `--dry-run --refresh-metadata --upgrade` composition: run refresh's dry-run FIRST (showing what would change in `metadata.json`), then upgrade's dry-run reads the EXISTING (pre-refresh) `upgrade_available` from disk — matches real-run semantics where refresh runs first and upgrade reads what's on disk

- [ ] 5.0 Implement output formatting conventions
  - [ ] 5.1 Every creator section starts with `==> <slug> (DRY RUN[<mode suffix>])` where `<mode suffix>` matches the actual flags passed (empty for default; ` --refresh-metadata`; ` --upgrade`; ` --manifests-only`; ` --audit`; combinations joined by spaces)
  - [ ] 5.2 Per-creator section body uses **two-space indented bullets** for per-item detail; intra-creator section headers (`Pass 1:`, `Pass 2 (eligibility):`, `PRD 4 (metadata.json):`, `--repair:`, etc.) use no indent but are visually grouped under the creator header
  - [ ] 5.3 Per-video lines use the format `<VIDEO_ID> — <action description>` (em-dash with spaces) for grep-ability across all modes
  - [ ] 5.4 After all creators complete, print one final `Total: <aggregated counts matching the mode> across <N> creators`; if `--creator` was used, `<N>` is `1`; if no work was found anywhere, still print the total with zeros
  - [ ] 5.5 Always emit `==> <slug> (DRY RUN...)` headers for every creator processed, even ones with no work to report
  - [ ] 5.6 Use `print(..., flush=True)` so output appears as work progresses (interactive piping to `less` / `tee` works correctly)
  - [ ] 5.7 Exit code is `0` for any successful dry-run regardless of detected changes

- [ ] 6.0 Verify against PRD success metrics
  - [ ] 6.1 Default dry-run on a creator with new videos: capture filesystem state (sha256 directory tree hash or `find ... -newer`) before and after `uv run archive.py --dry-run --creator <slug>`; confirm zero changes
  - [ ] 6.2 Default dry-run with no new videos: same creator, second invocation; output reports `0 would download, 0 gated, N already archived`; zero filesystem changes
  - [ ] 6.3 Manifests-only dry-run: `uv run archive.py --dry-run --manifests-only --creator <slug>` produces Pass 1 output and exits; filesystem unchanged (creator.json, manifest files, log files all untouched)
  - [ ] 6.4 Refresh-metadata dry-run: edit one video's `metadata.json.current.title` to a wrong value; run `--dry-run --refresh-metadata --creator <slug>`; confirm the output flags that video with `would update: title changed (...)` and `metadata.json` still has the wrong title afterwards
  - [ ] 6.5 Upgrade dry-run: manually populate one video's `upgrade_available` with a real descriptor; run `--dry-run --upgrade --creator <slug>`; output lists the video with the documented `would upgrade ...` line; media file sha256 unchanged; no `.pre-upgrade` files appear
  - [ ] 6.6 Audit dry-run: run `--dry-run --audit --creator <slug>`; confirm audit output appears on stdout; `audit.log` is NOT created/modified (mtime check)
  - [ ] 6.7 Audit + repair dry-run: set up an orphan in `archive.txt`; run `--dry-run --audit --repair`; output shows `[DRY RUN] would prune from archive.txt: <ID>`; `archive.txt` is byte-identical to before
  - [ ] 6.8 Multi-flag composition: `--dry-run --refresh-metadata --upgrade` runs both modes' dry-runs sequentially; each produces its own per-creator section; filesystem unchanged
  - [ ] 6.9 Per-creator log files untouched: after any dry-run invocation, every per-creator log file (`download.log`, `manifests.log`, `errors.log`, `refresh.log`, `upgrade.log`, `audit.log`) has the same mtime as before
  - [ ] 6.10 Stdout is the only side effect: diff filesystem state pre- and post-run (`find data/ -newer <ref> -type f | xargs sha256sum`); the diff should be empty
  - [ ] 6.11 Channel-level failure reported not silenced: configure a creator with a bogus `channel_url`; run `--dry-run`; output includes `Pass 1: CHANNEL-LEVEL FAILURE — would skip Pass 2 and PRD 4 for this creator`; no per-creator log file is written for that creator's failure
  - [ ] 6.12 Refresh dry-run uses network: run `--dry-run --refresh-metadata` against a creator with 10 videos; confirm via process monitoring or instrumented subprocess calls that ~10 yt-dlp invocations were made
  - [ ] 6.13 Upgrade dry-run uses NO network: run `--dry-run --upgrade` against a creator with 5 upgrade targets; confirm zero yt-dlp invocations
  - [ ] 6.14 Exit code 0 for every dry-run invocation including ones that report changes would be made
  - [ ] 6.15 Real run produces what dry-run predicted: run `--dry-run` and capture the would-download list; immediately run the real command; compare the actually-downloaded videos against the dry-run prediction (subset relationship; identical in steady state with no concurrent YouTube changes)
  - [ ] 6.16 `==> <slug> (DRY RUN...)` header always present for every creator processed, even ones with no work to report
