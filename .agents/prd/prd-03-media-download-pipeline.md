# PRD 03 — Media Download Pipeline (Pass 2)

## 1. Introduction / Overview

This PRD covers **Pass 2**: turning the candidate video-ID set from PRD 2 into archived media on disk. For each creator that successfully completed Pass 1, the script downloads each not-yet-archived, eligible video as `<ID>.mkv` (or the configured container) along with its raw `info.json`, thumbnail, and subtitles, and records the success in `archive.txt`.

When this PRD ships, `uv run archive.py` (no flags) does an end-to-end run: discover manifests (PRD 2) → download new videos (PRD 3) → exit. The script becomes useful for its primary purpose.

PRD 3 deliberately does **not** write the normalized `metadata.json` files — that's PRD 4's job. Pass 2 produces only what yt-dlp itself creates: the media file, `<ID>.info.json`, `<ID>.webp`, subtitle files, and one line per success in `archive.txt`.

See [`youtube-archive-plan.md`](../youtube-archive-plan.md) §"Pass 2 — Media" and [`prd-overview.md`](../prd-overview.md) for context.

## 2. Goals

1. After `uv run archive.py` completes for a creator who's never been archived before, every eligible video has a populated `data/<slug>/videos/<ID>/` folder with the merged media, raw `info.json`, thumbnail, and subtitles (when available).
2. A second invocation immediately afterward downloads nothing — every previously-archived video is recognized via `archive.txt` and skipped.
3. New uploads since the last run are detected and downloaded; old uploads from a creator's backfill are archived in chronological order (oldest first) so an interrupted run leaves a contiguous early-history archive.
4. Videos that are not-yet-downloadable (in-progress live streams, upcoming premieres, scheduled releases with future timestamps) and videos younger than `min_upload_age_hours` are skipped without being added to `archive.txt`, so they're naturally reconsidered on the next run.
5. A single video failure (network blip, geo-blocked, etc.) does not kill the run — it's logged and the next video proceeds.
6. A creator whose Pass 1 had a channel-level failure (per PRD 2 §4.9) is skipped entirely in Pass 2.

## 3. User Stories

- **As the developer**, I want to add a new creator to `config.toml` and run `uv run archive.py`, then walk away. Hours later I want to come back to a fully archived creator subtree.
- **As the developer**, when YouTube hasn't finished encoding all format tiers for a just-published video, I want my archive to wait a couple of days before grabbing it, so I get the best quality on the first try and don't waste an `--upgrade` cycle later.
- **As the developer**, if I lose my network connection halfway through a 500-video backfill, I want to re-run the same command and have it pick up exactly where it left off, downloading only what's missing.
- **As the developer**, I want the script's logs to make it obvious *why* something didn't download — gated for upload age, gated for live stream, real error, or already archived — without me having to inspect yt-dlp's raw stderr.
- **As the developer**, if one of forty videos fails because of a transient YouTube issue, I want the other thirty-nine to download anyway and the failure to retry naturally on the next run.

## 4. Functional Requirements

### 4.1 Channel-level failure gate

1. For each creator, Pass 2 MUST check the `channel_level_failed` flag returned by PRD 2 §4.7.
2. If the flag is `True`, the script MUST log a single `INFO` line to `download.log` (`Pass 2 skipped: channel-level failure in Pass 1`), print one line to stderr (`<slug>: skipping Pass 2 due to Pass 1 channel-level failure`), and proceed to the next creator. No yt-dlp invocations happen.

### 4.2 Source of truth: `archive.txt`

3. The script MUST treat `data/<slug>/archive.txt` as the **sole** source of truth for "this video has been archived." Disk state (presence of a video folder or media file) is NOT consulted in v1.
4. Before constructing the per-video work list, the script SHOULD read `archive.txt` once into an in-memory set and filter the candidate set against it. yt-dlp's `--download-archive` flag is still passed so yt-dlp also enforces the rule per-invocation (defense in depth).
5. `archive.txt` entries follow yt-dlp's native format: one line per archived video, `youtube <VIDEO_ID>\n`. The script never writes to this file directly — only yt-dlp does, via `--download-archive`. This is what guarantees that a failed download is naturally re-attempted on the next run.

### 4.3 Candidate filtering: live status and upload age

6. For each candidate video ID, the script MUST classify it into one of three buckets **before** invoking yt-dlp's download command:
    - **Eligible**: download now.
    - **Gated — not-yet-broadcast**: skip without writing to `archive.txt`, log as gated. (Live in progress, upcoming premiere, future release timestamp.)
    - **Gated — too recent**: skip without writing to `archive.txt`, log as gated. (Upload age < `min_upload_age_hours`.)
7. **Live-status classification** uses the manifest entry's `live_status` field from PRD 2:
    - `"is_live"` → gated (broadcasting now)
    - `"is_upcoming"` → gated (premiere scheduled)
    - `"was_live"` → eligible (finished live stream, now a VOD)
    - `"not_live"` / `null` / missing → eligible (treated as a normal video)
8. **Future release timestamp**: if `release_timestamp` exists and is in the future (relative to `datetime.now(timezone.utc)`), classify as gated regardless of `live_status`.
9. **Upload-age classification** uses the manifest entry's `timestamp` (preferred) or `release_timestamp` (fallback). If `(now - upload_time).total_seconds() / 3600 < min_upload_age_hours`, classify as gated. If `min_upload_age_hours == 0`, the gate is fully disabled.
10. **Fallback timestamp / live_status fetch**: if the manifest entry lacks BOTH `timestamp` and `live_status`, the script MUST make a single yt-dlp call to fetch them before classification: `yt-dlp --skip-download --no-warnings --print "%(timestamp)s|%(live_status)s|%(release_timestamp)s" <video_url>`. Parse the three pipe-delimited values; missing values come back as `"NA"`. If the fallback call fails, log a `WARN` (`could not determine eligibility for <ID>, treating as eligible`) and proceed as eligible — the user's goal is to archive, so we err on the side of attempting.
11. The fallback call is per-video and only happens when needed; for most well-formed manifests it never fires.

### 4.4 Processing order

12. After filtering, the eligible set MUST be sorted by upload `timestamp` **ascending** (oldest first). Videos with missing/unparseable timestamps sort to the end (the only ones likely to lack timestamps are edge cases like very-old uploads or unlisted re-uploads).
13. Within a creator, downloads are processed strictly in this sorted order. There is no parallelism — see §4.6.

### 4.5 Format resolution

14. For each creator, the script MUST resolve the effective format settings by overlaying per-creator overrides on top of `[defaults]`:
    - `format` (string) — required; from creator override if present, else from `[defaults]`. Both must be set; PRD 1 §4.4 already validates this.
    - `format_sort` (list of strings) — from creator override if present, else from `[defaults]`. May be empty list.
    - `merge_output_format` (string) — same resolution as `format`.
    - `min_upload_age_hours` (int) — same; defaults to 48 from PRD 1.
15. Per-creator override semantics: if a key is present in the `[[creator]]` block (even as an empty list or `0`), it shadows the `[defaults]` value entirely — no merging of list contents.

### 4.6 yt-dlp invocation

16. For each eligible video (in the order from §4.4), the script MUST invoke yt-dlp as a subprocess with the following arguments (one invocation per video; serial — never parallel):
    ```
    yt-dlp \
      -f "<resolved format>" \
      [-S "<format_sort, joined with commas>"]      # omitted if format_sort is empty/unset
      --merge-output-format <resolved merge_output_format> \
      --download-archive data/<slug>/archive.txt \
      --write-info-json \
      --write-thumbnail \
      --write-subs --write-auto-subs --sub-langs "en.*,en" \
      --convert-thumbnails webp \
      --embed-metadata --embed-thumbnail \
      --no-progress \
      -o "data/<slug>/videos/%(id)s/%(id)s.%(ext)s" \
      "https://www.youtube.com/watch?v=<VIDEO_ID>"
    ```
17. `-S` MUST be omitted from the command entirely (not passed as empty) if the resolved `format_sort` is an empty list or unset. This lets yt-dlp use its built-in default sort.
18. `--no-progress` is included to keep `download.log` parseable (progress bars use carriage returns and would clutter the log). Final completion status is still logged.
19. yt-dlp's default `--continue` behavior is in effect — if a `.part` file exists from a prior interrupted run, yt-dlp resumes it. No special handling required from PRD 3.
20. Downloads are **serial**: one yt-dlp invocation must complete (success, failure, or gated) before the next starts. No threading, no `asyncio`, no `--max-downloads` chains. Rationale: minimizes risk of YouTube rate-limiting and keeps logs linear.

### 4.7 Output capture and logging

21. yt-dlp stdout and stderr MUST be streamed to `data/<slug>/logs/download.log` via the PRD 1 subprocess helper. Raw yt-dlp output goes verbatim; the script wraps it with one `INFO` line before (`==> downloading <VIDEO_ID>`) and one after (`<-- done: <VIDEO_ID>` on success, `<-- FAILED: <VIDEO_ID>` on non-zero exit).
22. Gated videos do NOT invoke yt-dlp. They get one `WARN` line in `download.log`:
    - `WARN gated (live stream in progress): <VIDEO_ID>`
    - `WARN gated (upcoming premiere): <VIDEO_ID>`
    - `WARN gated (future release: <ISO timestamp>): <VIDEO_ID>`
    - `WARN gated (upload age <N>h < min_upload_age_hours=<N>): <VIDEO_ID>`
23. On a per-creator basis, the script MUST log Pass 2 summary lines:
    - `INFO Pass 2 starting: <total candidate count>` (after Pass 1 hands off)
    - `INFO Pass 2 filtering: <archived already> already in archive.txt, <eligible> eligible, <gated_live> gated (live/premiere), <gated_age> gated (too recent)`
    - One pair of `==> downloading` / `<-- done|FAILED` per eligible video
    - `INFO Pass 2 complete: <downloaded> downloaded, <gated> gated, <failed> failed`
24. Console output during Pass 2 is limited to the existing `==> <slug>` header (already printed before Pass 1) and a single end-of-creator summary line: `<slug>: Pass 2 — <downloaded> downloaded, <gated> gated, <failed> failed`. Per-video activity stays in the log.

### 4.8 Per-video failure handling

25. A non-zero yt-dlp exit code for a single video MUST be handled as follows:
    - Log one `<-- FAILED: <VIDEO_ID>` line to `download.log`.
    - Append one `ERROR download failed: <VIDEO_ID> — <last 5 lines of yt-dlp stderr>` line to `errors.log`.
    - Do NOT add the video ID to `archive.txt` (yt-dlp handles this natively — only successful downloads get added).
    - Move on to the next video.
26. yt-dlp's own internal retries (default `--retries 10`) handle transient network issues. Do not layer additional retries on top.
27. A partially-populated video folder from a failed download (`<ID>.part`, `<ID>.info.json` without `<ID>.mkv`, etc.) is left on disk untouched. yt-dlp will resume on the next run via `--continue`.

### 4.9 Default run flow

28. The default invocation `uv run archive.py` (no flags) MUST, for each selected creator (or each creator in the config if `--creator` is not specified):
    1. Run PRD 2 Pass 1.
    2. If `channel_level_failed` is `False`, run PRD 3 Pass 2 against the candidate set.
    3. (Future PRD 4: write `metadata.json` for each successfully downloaded video. Not in scope here.)
29. Creators are processed sequentially. The order is the order they appear in `config.toml`.
30. A failure inside one creator's Pass 2 (any exception not caught by per-video handling) MUST be caught by the `creator_scope` helper from PRD 1 §4.9, logged to that creator's `errors.log`, and processing MUST continue with the next creator.

## 5. Non-Goals (Out of Scope)

- **Writing `metadata.json`.** That's PRD 4. PRD 3 stops at yt-dlp's outputs + `archive.txt`.
- **Parallel downloads.** Serial only in v1.
- **Bandwidth throttling (`--limit-rate`).** Not configurable in v1. If it becomes a need, add a `bandwidth_limit` key to `[defaults]` in a future PRD.
- **A `--max-downloads N` cap per run.** Not in v1.
- **A consistency-check / audit command** that cross-references `archive.txt` against on-disk files and flags discrepancies (videos in `archive.txt` whose folders are missing, or vice versa). This is a planned future PRD — see Open Questions §9.1 and the note added to the PRD overview. Out of scope here so PRD 3 stays focused on the happy-path download pipeline.
- **Self-healing when on-disk state contradicts `archive.txt`.** Per Q4=A, `archive.txt` is the only source of truth in PRD 3.
- **Re-downloading existing videos at better quality.** That's PRD 6 (`--upgrade`).
- **Members-only / private content** requiring auth cookies. Out of scope for v1 entirely.
- **Comments archival (`--write-comments`).** Plan defers explicitly.
- **Live-stream capture.** In-progress live streams are gated; we wait for the VOD.
- **Detecting / pruning leftover `.part` files** beyond yt-dlp's own resume behavior.

## 6. Design Considerations

- **Serial-by-default rationale.** A creator's first backfill of 500 videos × ~30s = ~4 hours. Slower than parallel, but: (1) zero risk of triggering YouTube rate-limits across concurrent connections from the same IP; (2) `download.log` is a clean linear trace, trivially tail-able for "what's it doing right now"; (3) errors are unambiguous — no question about which worker crashed; (4) we can revisit if 4-hour backfills become a real pain point in practice.
- **Oldest-first ordering rationale.** Pairs with the upload-age gate (`timestamp` is needed for both, so the data's already in hand). An interrupted backfill leaves a contiguous early-history archive on disk, which is much more useful than a scattershot middle. New uploads still get picked up promptly because each run re-discovers everything in Pass 1 and the new ones become eligible.
- **Pre-flight live-status gate rationale.** Without it, a popular creator's in-progress livestream would hit `errors.log` on every single run until they end the broadcast — which could be hours. Pre-filtering gives a clean `WARN gated` message and zero noise in `errors.log`.
- **Fallback `--print` call (§4.10) only triggers when the manifest is incomplete.** This is rare — yt-dlp's flat-playlist dump usually includes `timestamp` and `live_status` for normal videos. The fallback is for edge cases (unlisted re-uploads from old playlists, videos with unusual extractor paths).
- **`--no-progress` over `--newline`.** Both make logs parseable, but `--no-progress` keeps logs short and human-skim-able. The final completion summary that yt-dlp emits is enough for a "did it work?" verification.

## 7. Technical Considerations

- **PRD dependencies:** PRD 1 (subprocess helper, per-creator loggers, `creator_scope`), PRD 2 (manifest data structure: candidate IDs + per-video `timestamp`/`live_status` + `channel_level_failed` flag).
- **In-memory candidate handoff from PRD 2:** PRD 3 receives the data structure documented in PRD 2 §4.7. It does NOT re-read manifest files from disk for ordinary operation — everything it needs is already in memory.
- **`subprocess.Popen` invocation:** the yt-dlp command in §4.16 is a long list of args; build it as a Python list (not a shell string) to avoid quoting issues. Pass `stdout=PIPE, stderr=STDOUT` and use the PRD 1 streaming helper to route output to `download.log`.
- **Exit-code handling:** yt-dlp returns `0` on success, non-zero on any download failure. Don't try to interpret partial failures from stderr — trust the exit code per §4.25.
- **`archive.txt` lock-free model:** v1 is single-process, so we don't need file locking. yt-dlp's own writes to `archive.txt` are atomic (it uses an O_APPEND open).
- **Format selector edge cases:** if a creator's `format_sort` includes `res:1080` but a video is only available at 4K, yt-dlp will pick the 4K version (sort keys *rank*, they don't *cap*). To actually cap resolution, the user must put `[height<=1080]` in the `format` selector itself. This is yt-dlp behavior; document it in the config example file so users aren't surprised.
- **Subtitle availability:** `--write-subs --write-auto-subs --sub-langs "en.*,en"` requests English subtitles (creator-provided or auto-generated). If neither exists, yt-dlp silently produces no subtitle file. Not an error.
- **Thumbnail format:** `--convert-thumbnails webp` converts whatever yt-dlp downloads to `.webp`. Requires ffmpeg (already verified at startup by PRD 1).
- **Output template:** `-o "data/<slug>/videos/%(id)s/%(id)s.%(ext)s"` uses yt-dlp's template syntax. The `<slug>` is interpolated by Python *before* the command is built; `%(id)s` and `%(ext)s` are interpolated by yt-dlp.

## 8. Success Metrics

This PRD is done when **all** of the following are true:

1. **End-to-end smoke run on a small playlist (5–10 videos)** — `uv run archive.py --creator <slug>` against a creator with one short playlist downloads every eligible video. After completion: `archive.txt` has one line per downloaded video; each `data/<slug>/videos/<ID>/` contains `<ID>.mkv`, `<ID>.info.json`, `<ID>.webp`, and a `.vtt` file if subtitles exist.
2. **Idempotent second run** — re-running the same command immediately afterward produces zero downloads. `download.log` shows `Pass 2 filtering: N already in archive.txt, 0 eligible, ...`. No new files in `videos/`.
3. **Channel-level failure gate** — using a creator whose Pass 1 sets `channel_level_failed` (e.g., a bogus `channel_url`), confirm Pass 2 logs `Pass 2 skipped: channel-level failure in Pass 1` and does not invoke yt-dlp.
4. **Format default applied** — with `[defaults]` set to `format_sort = ["res:1080", "codec:av01"]`, download a known-1080p-AV1 video. Run `ffprobe -v error -show_entries stream=codec_name,height` and confirm `av1` and `1080`.
5. **Per-creator format override** — add `format_sort = ["res:2160", "codec:av01"]` to one creator block. Re-run against a video known to be available in 4K. Confirm that creator's video is 2160p while other creators stay at 1080p.
6. **Upload-age gate** — set `min_upload_age_hours = 999999` temporarily. Run on a creator with recent uploads. Confirm new videos are logged as `WARN gated (upload age ...)` and are NOT in `archive.txt`. Reset to a normal value, re-run, confirm they now download.
7. **Live/premiere gate** — point a creator's `extra_playlists` at a playlist containing an upcoming-premiere video. Confirm Pass 2 logs `WARN gated (upcoming premiere): <ID>` and does NOT add the ID to `archive.txt`.
8. **Per-video failure isolation** — manually edit `extra_playlists` to include a playlist with one known-deleted video. Confirm the deleted video produces `ERROR download failed: <ID>` in `errors.log`, the other videos in the same playlist still download, the run exits `0`, and the failed video is NOT in `archive.txt`.
9. **Oldest-first ordering** — for a creator with 10 candidate videos, kill the process after 3 downloads complete. Confirm by inspecting the `info.json` `timestamp` fields of the 3 downloaded videos that they are the 3 oldest in the candidate set.
10. **Multi-creator end-to-end** — config with 2 creators, both new. `uv run archive.py` processes them sequentially (one's `==> <slug>` header appears entirely before the next), both produce populated subtrees, both have populated `archive.txt`.
11. **Resume from interrupted download** — start a download, kill the process mid-`.part`. Re-run. yt-dlp resumes the partial file (verify via `download.log`: yt-dlp typically logs `[download] Resuming download at byte N`); final file is complete and merged.

These map onto verification items **#3** (smoke run — the media half), **#11** (format default), **#12** (format override), and **#13** (upload-age gate) from the plan. Item #15 (failure isolation) gets its behavioral exercise here.

## 9. Open Questions

1. **Archive-consistency audit command (future PRD).** A separate script that cross-references `archive.txt` against on-disk video folders and flags discrepancies (entries with no folder, folders with no entry, missing media files inside an otherwise-populated folder). Useful weekly to detect manually-deleted videos or disk corruption. The proposed remediation pattern: log discrepancies, optionally let the user remove the affected ID from `archive.txt` so the next normal run re-downloads. Should this become its own PRD (e.g., PRD 8 — "Archive Consistency Audit")? See suggested addition to `prd-overview.md`.
2. **What if `min_upload_age_hours` is set very low (e.g., 1)?** YouTube's encoding pipeline sometimes takes longer than expected for higher-tier formats. The gate is advisory, not enforced — users are free to set whatever they want. Document the trade-off in `config.example.toml`.
3. **Should we add a `--max-downloads N` flag** for "do a quick partial backfill" runs? Not in v1; revisit if useful.
4. **Verbose mode (`-v`)** to stream yt-dlp output to console in addition to the log file? Nice-to-have; out of scope here.
5. **Should the per-creator end-of-Pass-2 console summary be printed even if Pass 2 had nothing to do** (e.g., 0 candidates after filtering)? ✅ Resolved (2026-05-26): yes — always print the `<slug>: Pass 2 — 0 downloaded, 0 gated, 0 failed` line so the user has explicit confirmation that checks ran and nothing needed action. Same convention applies uniformly to the per-creator summary lines in PRDs 5, 6, 7, and 8.
