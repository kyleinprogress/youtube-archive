# PRD 05 — Metadata Refresh & Upgrade Detection (Pass 3)

## 1. Introduction / Overview

This PRD adds the `--refresh-metadata` command — an opt-in, read-only-on-disk-media pass that, for every video in `archive.txt`, fetches current metadata from YouTube and updates `metadata.json` to reflect three kinds of change:

1. **Metadata drift**: `title`, `thumbnail_url`, or `description_hash` changed since last check. The previous values get appended to `history` and `current` gets the new ones.
2. **Availability changes**: video deleted, made private, gated behind membership, or — equally important — resurrected from any of those states. Tracked in `availability` and `removed_detected_at`, with the state transition appended to `history`.
3. **Quality upgrades available**: a better-quality version of the video now exists per the creator's current `format` + `format_sort` config than what we have on disk. Recorded in `upgrade_available` for PRD 6 (`--upgrade`) to act on.

PRD 5 is intentionally read-only with respect to the **media files** on disk — it never re-downloads. Its writes are confined to `metadata.json` files. PRD 6 is the destructive sibling that consumes the `upgrade_available` field PRD 5 populates.

The plan calls this "Pass 3"; see [`youtube-archive-plan.md`](../youtube-archive-plan.md) §"Pass 3" and [`prd-overview.md`](../prd-overview.md) for surrounding context. PRD 5 owns verification items #9, #10, and the detection half of #14.

## 2. Goals

1. `uv run archive.py --refresh-metadata` walks every archived video for every selected creator, makes one yt-dlp call per video, and updates `metadata.json` files in-place. No media files are read, written, moved, or deleted.
2. Title, thumbnail, or description changes since the last refresh are detected; the **previous** values are preserved in `history` with a timestamp showing when they were last known valid; `current.*` always reflects the most recent values.
3. Videos that become unavailable (deleted, private, members-only) are flagged in `availability` + `removed_detected_at`, with the media file left untouched on disk. The whole point of the archive is that we keep the file even after YouTube doesn't.
4. Resurrected videos (those that flip back to available) are detected automatically — no manual reset needed.
5. Per-video upgrade availability is computed by simulating yt-dlp's format selection with the creator's current `format` + `format_sort`; if the resulting format differs from what's recorded in `downloaded.*`, `upgrade_available` is populated with a descriptor of the better format. Otherwise it's set to `null`.
6. The operator gets a glance-able per-creator summary at the end of the run (`<slug>: refresh — N checked, M metadata changes, K upgrades available, X newly unavailable`) so a weekly cron job's output is immediately useful.
7. Transient yt-dlp errors (network blips, rate limits) do NOT corrupt state — they leave `availability` and `current.*` untouched, log to `errors.log`, and proceed to the next video.

## 3. User Stories

- **As the developer**, I want to schedule a weekly `--refresh-metadata` run so I learn about title changes, removed videos, and quality upgrades without me having to babysit anything.
- **As the developer**, when a creator quietly renames a video, I want my archive's `metadata.json` to capture both the old and new titles with timestamps — so years later I can answer "what was this video originally called?"
- **As the developer**, when a creator deletes a video, I want my media file to stay on disk and `metadata.json` to flag the deletion in `availability` and `removed_detected_at` — preserving the archive while making the loss queryable.
- **As the developer**, when YouTube rolls out higher-bitrate AV1 1080p for a creator's existing 1080p VP9 archive, I want PRD 5 to flag the upgrade so I can run `--upgrade` when convenient — not silently lose the chance to improve quality.
- **As the developer**, when my home network glitches mid-refresh, I want partial results saved and the failed videos logged — not the archive's metadata corrupted by assumed deletions.
- **As the developer**, when a creator un-privates an old video I'd previously marked as `private`, I want my next refresh to notice and flip `availability` back to `"available"` automatically.

## 4. Functional Requirements

### 4.1 When PRD 5 runs

1. PRD 5 MUST run only when `--refresh-metadata` is passed on the CLI.
2. `--refresh-metadata` MUST skip Pass 1 (PRD 2), Pass 2 (PRD 3), and PRD 4. The script does NOT discover manifests, does NOT download media, and does NOT update `playlists` membership in this mode.
3. `--refresh-metadata` MAY be combined with `--creator <slug>` to scope the refresh to one creator (per PRD 1's CLI surface). Without `--creator`, all creators in `config.toml` are processed sequentially.
4. `--refresh-metadata` is a separate code path from the default invocation; the default `uv run archive.py` never invokes PRD 5. (Composition with `--upgrade` is owned by PRD 6; composition with `--dry-run` is owned by PRD 7.)

### 4.2 Per-video refresh call

5. For each video ID in `data/<slug>/archive.txt` (in archive.txt order; no sort), PRD 5 MUST invoke yt-dlp exactly once with the following arguments:
    ```
    yt-dlp \
      --skip-download \
      --no-warnings \
      -f "<resolved format>" \
      [-S "<resolved format_sort, joined with commas>"]    # omitted if format_sort empty/unset
      --dump-json \
      "https://www.youtube.com/watch?v=<VIDEO_ID>"
    ```
6. The format selection flags (`-f`, `-S`) MUST be resolved using the same precedence as PRD 3 §4.5 — `[defaults]` overlaid with per-creator overrides. Passing them is what makes yt-dlp's output reflect the format it *would* pick now, which is the basis for upgrade detection in §4.5.
7. On success (exit code 0, parseable JSON to stdout), the script parses the JSON and proceeds through §§4.3–4.6 in order: metadata diff → availability tracking → upgrade detection → `last_metadata_check` update.
8. **Concurrency**: downloads in this pass are strictly **serial** — one yt-dlp invocation completes before the next starts. Rationale matches PRD 3's: minimizes rate-limit risk, keeps logs linear. For a 1000-video archive this is roughly 15-20 minutes per refresh, which is acceptable for the weekly/monthly cadence this command targets.
9. **No fallback or retry layer.** yt-dlp's built-in retries (default `--retries 10`) handle transient network issues. The script does NOT layer additional retries.

### 4.3 Metadata drift detection and `history` writes

10. After a successful refresh call, the script MUST compute the **new** values for the `current` block:
    - `title` ← parsed JSON's `title`
    - `thumbnail_url` ← parsed JSON's `thumbnail` (top-level)
    - `description_hash` ← `"sha256:" + sha256(parsed_json["description"].encode("utf-8")).hexdigest()` (matches PRD 4 §4.4 exactly; null/missing descriptions hash empty bytes)
11. The script MUST compute `changed_fields` as the subset of `{"title", "thumbnail_url", "description_hash"}` whose new value differs from the corresponding value in the existing `metadata.json.current` block.
12. If `changed_fields` is non-empty, the script MUST:
    1. Append one entry to `metadata.json.history`:
        ```json
        {
          "type": "metadata_change",
          "previous_observed_until": "<existing metadata.json.last_metadata_check>",
          "changed_fields": ["title", "description_hash"],
          "previous": {
            "title": "<existing current.title>",
            "thumbnail_url": "<existing current.thumbnail_url>",
            "description_hash": "<existing current.description_hash>"
          }
        }
        ```
        Notes on the entry:
        - `previous_observed_until` = the `last_metadata_check` from the existing `metadata.json` *before* this refresh runs. Semantically: "the prior values were known-good up through this timestamp."
        - `changed_fields` lists ONLY the keys that actually differ (sorted alphabetically for determinism). This makes the entry human-skim-able without diffing.
        - `previous` always carries the FULL prior `current` block (all three fields), even when only one changed. This keeps the schema uniform and lets readers reconstruct any field's history.
    2. Replace `metadata.json.current` with the new values (all three fields).
13. If `changed_fields` is empty (nothing actually changed since the last refresh), the script MUST NOT append any history entry. `current` stays untouched.
14. The implicit "when was the current value first seen?" question is always answerable: for the latest value, it's the current `last_metadata_check`; for an older value preserved in history entry N, it's history entry N+1's `previous_observed_until` (or the current `last_metadata_check` if N is the most recent entry).

### 4.4 Availability tracking

15. The script MUST classify each refresh outcome into one of these states based on yt-dlp's exit code and stderr:
    - **available**: exit 0, JSON parsed successfully.
    - **removed**: exit non-zero AND stderr contains any of: `"Video unavailable"`, `"This video has been removed"`, `"This video is no longer available"`, `"has been terminated"`.
    - **private**: exit non-zero AND stderr contains: `"Private video"`, `"This video is private"`.
    - **members_only**: exit non-zero AND stderr contains: `"members-only"`, `"members only"`, `"requires a Channel Membership"`.
    - **age_restricted**: exit non-zero AND stderr contains: `"Sign in to confirm your age"`, `"age-restricted"`.
    - **transient_error** (NOT a real state, internal classification only): exit non-zero AND stderr matches none of the above (network errors, 5xx responses, JSON parse errors, etc.).
16. On a **transient_error** classification, the script MUST:
    - Log one `ERROR` to `errors.log`: `refresh failed: <VIDEO_ID> — <last 5 lines of stderr>`.
    - Log one `WARN` to a new `data/<slug>/logs/refresh.log` file: `refresh transient error: <VIDEO_ID>`.
    - Leave `availability`, `removed_detected_at`, `current.*`, `upgrade_available`, AND `last_metadata_check` all unchanged. The video is treated as "we don't know" for this run; the next refresh tries again.
    - Continue to the next video.
17. On any of the real-state classifications (`available`, `removed`, `private`, `members_only`, `age_restricted`):
    - Capture the prior `availability` value from existing `metadata.json` for comparison.
    - Set new `availability` to the classified value.
    - If new state is *not* `"available"` AND `removed_detected_at` is currently `null`, set `removed_detected_at` to the current UTC timestamp. If `removed_detected_at` is already populated, leave it (preserves the original detection time).
    - If new state IS `"available"` AND prior state was not `"available"` (a resurrection), set `removed_detected_at` back to `null`.
    - If new state differs from prior state, append an entry to `history`:
        ```json
        {
          "type": "availability_change",
          "previous_observed_until": "<existing last_metadata_check>",
          "previous": "<prior availability value>"
        }
        ```
18. If the video's new state is anything other than `"available"`, the script MUST skip §4.3 (metadata drift) and §4.5 (upgrade detection) entirely for this video — yt-dlp gave us no payload to diff against or compute upgrades from. `current.*` and `upgrade_available` stay as they were. (`last_metadata_check` is still updated per §4.6.)
19. The media file on disk MUST NOT be touched in any availability-change scenario. The archive's whole purpose is to preserve files YouTube no longer serves.

### 4.5 Upgrade detection

20. When the refresh call succeeded and the video is `"available"`, the script MUST compute upgrade availability by comparing yt-dlp's selected format against `metadata.json.downloaded`.
21. yt-dlp's selected format is read from the parsed JSON's top-level fields after the `-f` / `-S` selection rules were applied (since we passed `-f` and `-S` on the command line in §4.5 #5): `format_id`, `height`, `vcodec`, `acodec`, `filesize` (or `filesize_approx` if `filesize` is null).
22. **Upgrade rule**: an upgrade is available if `simulated.format_id != downloaded.format_id`. Rationale: yt-dlp's selection with current `format` + `format_sort` represents the user's stated preference for what they want. If yt-dlp would pick something other than what we have, that's an upgrade by definition (whether because of a better encoding now available, a new tier rolled out, or a config change that raised the bar).
23. If an upgrade is available, populate `upgrade_available` with:
    ```json
    {
      "detected_at": "<current UTC timestamp>",
      "format_id": "<simulated format_id>",
      "height": <simulated height or null>,
      "vcodec": "<simulated vcodec>",
      "acodec": "<simulated acodec>",
      "filesize": <simulated filesize, with filesize_approx fallback>
    }
    ```
    Always overwrite the prior `upgrade_available` value with the new descriptor — `detected_at` is the most-recent detection time, not the first.
24. If no upgrade is available (`simulated.format_id == downloaded.format_id`), set `upgrade_available` to `null`. This handles the case where an upgrade was previously detected but is no longer applicable (e.g., creator's config changed back, or YouTube removed the better tier).
25. No `history` entry is written for upgrade-detection state changes. `upgrade_available` is a pure "current state" pointer; PRD 6 writes the actual `{type: "upgrade", ...}` history entry when the upgrade is executed.

### 4.6 `last_metadata_check` updates

26. On any **successful** refresh (yt-dlp exit 0 with parseable JSON, OR a clean availability classification in §4.4 with one of the real states), the script MUST set `metadata.json.last_metadata_check` to the current UTC ISO 8601 timestamp.
27. On a **transient_error** (§4.16), `last_metadata_check` MUST NOT be updated. Rationale: the field semantically means "last time we successfully observed this video's state" — if we couldn't observe it, we didn't check it.

### 4.7 Orphan / corruption handling

28. **Orphan path** (ID in `archive.txt` but `data/<slug>/videos/<ID>/` does not exist): skip silently — no log line, no error. Matches PRD 4 §4.6 #19; PRD 8 (`--audit`) is the proper surfacing mechanism.
29. **Missing `metadata.json` path** (folder exists but `metadata.json` is missing): log one `WARN` to `refresh.log` (`refresh skipped: <VIDEO_ID> (no metadata.json; run a normal sync first)`) and skip the video. PRD 4 is responsible for creating `metadata.json`; PRD 5 only updates existing ones.
30. **Channel-level failure flag**: PRD 5 does NOT consult `channel_level_failed` from PRD 2, because PRD 5 doesn't run Pass 1 in the first place. Each video's refresh stands on its own.

### 4.8 Per-creator and per-run console summary

31. After each creator's refresh completes, the script MUST print one line to stdout:
    ```
    <slug>: refresh — <C> checked, <M> metadata changes, <A> availability changes, <U> upgrades available, <X> transient errors, <S> skipped
    ```
    Where:
    - `<C>` = number of videos for which a refresh call was attempted
    - `<M>` = number of videos with at least one `metadata_change` history entry appended this run
    - `<A>` = number of videos with at least one `availability_change` history entry appended this run
    - `<U>` = number of videos that now have a non-null `upgrade_available` (the **total** in this state, not just newly-flagged)
    - `<X>` = number of videos that failed with transient errors
    - `<S>` = number of videos skipped (orphan + missing-metadata cases combined)
32. After all creators complete, the script MUST print one final summary line:
    ```
    Total: <M> metadata changes, <A> availability changes, <U> upgrades available, <X> transient errors across <N> creators
    ```
33. If only one creator was processed (via `--creator`), the final total line is still printed but with `<N> = 1`. Keeps output uniform.
34. Console output during the per-video work is silent (no progress bar, no per-video lines). Detail goes to logs.

### 4.9 Logging

35. PRD 5 MUST create and write to `data/<slug>/logs/refresh.log` (a new per-creator log file). PRD 1's `get_creator_loggers` helper (or equivalent) MUST be extended to expose this fourth log file when this PRD is implemented.
36. Per-creator phase lines in `refresh.log`:
    - At start: `INFO refresh starting (<N> videos in archive.txt)`
    - Per video, one of (no line on no-op refreshes where nothing changed):
      - `INFO metadata_change: <VIDEO_ID> (changed: title, description_hash)` — listing only the changed field names
      - `INFO availability_change: <VIDEO_ID> (available -> removed)` — showing the transition
      - `INFO upgrade_available: <VIDEO_ID> (height 1080 -> 2160)` — newly-detected upgrade (only on the run where it transitions from null to populated)
      - `INFO upgrade_cleared: <VIDEO_ID>` — newly-cleared upgrade (transition from populated to null)
      - `WARN refresh skipped: <VIDEO_ID> (no metadata.json; run a normal sync first)`
      - `WARN refresh transient error: <VIDEO_ID>`
    - At end: `INFO refresh complete: <C> checked, <M> metadata changes, <A> availability changes, <U> upgrades available, <X> transient errors, <S> skipped`
37. Real failures (transient errors per §4.16) also write a one-line `ERROR` to `errors.log` with the last 5 lines of yt-dlp stderr. `download.log` and `manifests.log` are NOT touched by PRD 5.

### 4.10 Atomic writes

38. Every `metadata.json` rewrite MUST use the atomic write pattern from PRD 4 §4.7: write to a `.tmp` sibling, then `os.replace()`. This prevents readers from observing half-written files mid-refresh.
39. JSON output format: `json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False)` with a trailing newline. Field order in `current`, `downloaded`, and `upgrade_available` MUST match PRD 4 §4.2 #5 exactly. Field order in `history` entries MUST be `type`, then the timestamp field, then payload fields — to keep entries readable when grep'd or diff'd.

## 5. Non-Goals (Out of Scope)

- **Re-downloading media.** That's PRD 6 (`--upgrade`).
- **Discovering new videos.** No Pass 1 in this mode; PRD 5 only refreshes what's already in `archive.txt`.
- **Updating `playlists` membership.** That's PRD 4. `--refresh-metadata` does NOT refresh playlist memberships — that requires Pass 1 manifests, which this mode skips.
- **Parallel refresh calls.** Serial only in v1, matching PRD 3.
- **A separate `--detect-upgrades-only` mode.** Upgrade detection is bundled into every refresh call (it's free — same yt-dlp invocation).
- **Notifying the user outside the console** (email, push, webhook). v1 is console + log files only.
- **Backfilling history for videos that were archived before this PRD existed.** Existing `metadata.json` files have `history: []`; PRD 5 starts appending from its first run forward. Pre-PRD-5 changes are lost (we never had the data).
- **Pruning history entries** older than some threshold. History grows unbounded in v1.
- **Computing diffs on the actual description text** (only the hash). If you need to know what changed in a description, that's not surfaced — only that *something* changed.
- **Schema migration logic** for `schema_version > 1`. v1 only.
- **Surfacing orphans** (covered by PRD 8 audit).

## 6. Design Considerations

- **Bundled `metadata_change` entries.** When title, thumbnail, and description all change in one refresh, that's ONE history entry with `changed_fields: ["description_hash", "thumbnail_url", "title"]` and the full prior `previous` block — not three separate entries. Rationale: changes that arrive in the same refresh are correlated by definition (creator did a batch edit, or we're catching up after a long gap); separate entries would falsely imply they happened at different times.
- **Why `previous_observed_until` over `detected_at`.** Both are reconstructable from the other plus surrounding state. `previous_observed_until` is the one the user explicitly preferred — emphasizes "this old value was confirmed good through this date." The detection time of the new value is always the current `last_metadata_check` (which we update in §4.6).
- **Why `changed_fields` is included.** Diffing `previous` against the next entry's `previous` (or against `current`) is the source-of-truth way to determine what changed, but it's annoying for human reading. `changed_fields` is the easy-read shortcut; it's strictly derived from the diff, never authoritative.
- **Why the full `previous` block, not just changed fields.** Uniform schema. A consumer of the JSON can always pluck `previous.title` and know it's there, regardless of which fields triggered the entry. If we omitted unchanged fields, consumers would need `previous.get("title", current["title"])` everywhere.
- **`upgrade_available` is current-state, not history.** PRD 5 writes/clears it freely each run. PRD 6 is the one that appends `{type: "upgrade", ...}` to `history` when an upgrade is actually executed. Keeps the model clean: history is for events that *happened*, upgrade_available is for events that *could happen*.
- **Why availability re-checks aren't gated.** A `removed` video's refresh costs ~1s and gives us resurrection detection for free. Skipping them would save trivial time but add complexity (a special `--include-unavailable` flag, schema markers for "stop checking after N confirmations", etc.) and lose the auto-resurrection behavior. For a 1000-video archive with 50 unavailable videos, that's 50 extra seconds per refresh — well worth it.
- **Why stderr text matching for availability.** It's the only reliable signal yt-dlp gives us — there's no machine-readable error code distinguishing "deleted" from "private" from "network failed." Matching on stable English substrings is fragile (yt-dlp could change the wording in a future version) but it's the only game in town. We mitigate by being permissive (multiple substrings per state) and conservative (unknown errors default to transient, not removed).

## 7. Technical Considerations

- **PRD dependencies:**
  - PRD 1: per-creator logger (now extended to expose `refresh.log` as a fourth file per §4.35), `creator_scope` error boundary, subprocess streaming helper.
  - PRD 4: provides the `metadata.json` files this PRD reads and mutates. PRD 5 will NOT run successfully against a creator whose videos lack `metadata.json` files — PRD 4 must have run at least once first. The missing-metadata WARN in §4.29 is the user-facing diagnostic for this.
  - Indirectly PRD 2/3: `archive.txt` is the work list; the per-creator format settings used in §4.5 #6 come from `config.toml` resolution rules already established in PRD 3 §4.5.
- **yt-dlp invocation:** `--dump-json` returns one large JSON object to stdout. Capture it via `subprocess.run(..., capture_output=True, text=True)`. Don't stream — the JSON parser needs the whole document. For very large videos (long descriptions, many formats), the JSON can be 100KB+; this is well within stdlib `json` capabilities.
- **JSON parse failures count as transient errors per §4.16** — yt-dlp normally never emits malformed JSON on success, so a parse failure usually means yt-dlp wrote a partial response (e.g., killed mid-write).
- **Subprocess timeouts:** consider a per-video timeout (~60s) to prevent a hung yt-dlp from blocking the entire refresh. If a timeout fires, treat it as a transient error. Stdlib `subprocess.run(timeout=60)` raises `TimeoutExpired` cleanly.
- **History size:** if a creator changes a video's title weekly, that video accumulates one entry per refresh that detects a change. Over years this could grow large (KBs per video, MBs across a 1000-video archive). v1 doesn't prune. Plan note: if this becomes a real problem, a `--prune-history --older-than 1y` flag is the natural extension.
- **Resilience to PRD 4 schema changes:** PRD 5 reads `metadata.json.current.title`, `.thumbnail_url`, `.description_hash`; `metadata.json.downloaded.format_id`; `metadata.json.last_metadata_check`; `metadata.json.availability`; `metadata.json.removed_detected_at`; `metadata.json.history` (mutates by appending); `metadata.json.upgrade_available` (rewrites). All of these are PRD 4 §4.2 #5 schema fields. If PRD 4's schema changes in a future PRD, PRD 5 needs corresponding updates.
- **Stdlib only:** `subprocess` for yt-dlp, `json` for parsing, `hashlib.sha256` for description hashing, `datetime` for timestamps, `pathlib` for file ops. No third-party dependencies.

## 8. Success Metrics

This PRD is done when **all** of the following are true:

1. **Title-change history (verification item #9).** Manually edit `current.title` in one video's `metadata.json` to a wrong value (e.g., `"WRONG"`). Run `uv run archive.py --refresh-metadata --creator <slug>`. Confirm:
    - `current.title` is restored to YouTube's actual title.
    - One new entry in `history` with `type: "metadata_change"`, `changed_fields: ["title"]`, and `previous.title == "WRONG"`.
    - `last_metadata_check` is updated to a fresh timestamp.
2. **No-op refresh.** Run `--refresh-metadata` twice in a row on a creator where nothing changed on YouTube between runs. Confirm: zero new history entries, `last_metadata_check` updated on both runs, no console output beyond the summary lines, no log lines for unchanged videos.
3. **Multi-field change in one refresh.** Force changes to both `current.title` and `current.thumbnail_url` by hand-editing `metadata.json`. Run `--refresh-metadata`. Confirm a single history entry with `changed_fields: ["thumbnail_url", "title"]` (alphabetically sorted), not two entries.
4. **Unavailable video flagged (verification item #10).** Take a known-deleted YouTube video ID, add `youtube <ID>` to `archive.txt` by hand, create a placeholder video folder with a minimal valid `metadata.json` (use PRD 4 to generate it if possible, or hand-craft). Run `--refresh-metadata --creator <slug>`. Confirm:
    - `availability` flips to `"removed"`.
    - `removed_detected_at` is populated with a fresh timestamp.
    - One new `availability_change` entry in `history` with `previous: "available"`.
    - The placeholder folder is NOT deleted.
    - `current.*` fields are unchanged (we couldn't fetch new ones).
5. **Resurrection detection.** Manually edit a video's `metadata.json` to set `availability: "removed"` and `removed_detected_at: "2025-01-01T00:00:00Z"`. Run `--refresh-metadata` against the actually-available video. Confirm:
    - `availability` flips back to `"available"`.
    - `removed_detected_at` becomes `null`.
    - One new `availability_change` entry with `previous: "removed"`.
6. **Upgrade detection — newly-flagged (verification item #14, detection half).** Manually edit one video's `metadata.json.downloaded` to a deliberately worse format (e.g., `height: 360`, `vcodec: "avc1.42001E"`, `format_id: "18"`). Run `--refresh-metadata`. Confirm `upgrade_available` is populated with a descriptor whose `format_id` differs from `"18"` and whose `height` is greater than 360. Confirm one `INFO upgrade_available` line in `refresh.log`.
7. **Upgrade detection — newly-cleared.** Starting from the state in #6, manually edit `metadata.json.downloaded` to match what `upgrade_available` currently points at. Run `--refresh-metadata`. Confirm `upgrade_available` is now `null` and a `INFO upgrade_cleared` line appears.
8. **Upgrade detection — config-driven.** With a video's `downloaded.height` at 1080, change the creator's `format_sort` in `config.toml` to `["res:2160"]`. Run `--refresh-metadata`. If a 4K version exists on YouTube, confirm `upgrade_available` is populated with `height: 2160`.
9. **Transient error doesn't corrupt state.** Temporarily block network access (or point `channel_url` at a bogus DNS name? — actually use `iptables` or pull the cable) and run `--refresh-metadata`. Confirm:
    - All videos get logged as `WARN refresh transient error`.
    - `availability`, `current.*`, `upgrade_available`, AND `last_metadata_check` are all unchanged in `metadata.json` for every affected video.
    - `errors.log` has the per-video `ERROR refresh failed` entries.
    - Console summary reports the transient errors.
    - Script exits cleanly with code 0.
10. **Console summary.** For a creator with 10 videos, 2 of which had title changes and 1 of which has a newly-detected upgrade, confirm the per-creator line reads `<slug>: refresh — 10 checked, 2 metadata changes, 0 availability changes, 1 upgrades available, 0 transient errors, 0 skipped`.
11. **Orphan silence.** Add a fake `archive.txt` line `youtube zzz999fake`. Run `--refresh-metadata`. Confirm zero console output and zero log lines mention `zzz999fake`. Skip count in the summary is incremented by 1.
12. **Missing metadata.json warning.** For a real archived video, delete its `metadata.json` (keep the media). Run `--refresh-metadata`. Confirm one `WARN refresh skipped: <ID> (no metadata.json; run a normal sync first)` line and the skip count in the summary is incremented.
13. **Default invocation doesn't trigger PRD 5.** Run `uv run archive.py` (no flags). Confirm no `refresh.log` entries are appended (`stat` the file's mtime).
14. **`--creator` scoping.** Run `--refresh-metadata --creator <slug>` with multiple creators in config. Confirm only the named creator's videos are refreshed (mtime check on other creators' `metadata.json` files).
15. **Media files are never touched.** After any `--refresh-metadata` run, every video media file's mtime AND content (sha256) is identical to before the run. Verify by computing hashes before/after a refresh of a creator with at least 5 archived videos.

These map onto verification items **#9** (title-change history), **#10** (unavailable video flagged), and the detection half of **#14** (upgrade detection) from the plan.

## 9. Open Questions

1. **Should we expose a `--refresh-metadata --include-unavailable=false` flag** to skip videos already in non-available states? Default is to re-check everything per Q3 = A. Easy to add later if the re-check cost becomes painful.
2. **Subprocess timeout value.** §7 suggests 60s. Should this be configurable? A 60s default seems generous for a metadata-only call; lower might be better but risks false-positive transient errors for slow connections. Defer to first user with a complaint.
3. **Should the per-video log lines include the old/new values** (e.g., `metadata_change: <ID> (title: "Old" -> "New")`)? Adds context but bloats logs and risks line-length issues with long titles. Currently lines just say which fields changed; the values are in `metadata.json.history`.
4. **Should we record a refresh-run summary** like `data/<slug>/manifests/refresh_<date>.json` for post-hoc analysis? Currently no — the per-video state lives in each `metadata.json` and the aggregate is in `refresh.log`. Add later if querying becomes a need.
5. **What if `metadata.json.downloaded.format_id` is `null`** (e.g., archived before the format_id field was populated by PRD 4)? ✅ Resolved (2026-05-26): keep §4.22's current behavior — treat any selected format as an upgrade. `downloaded.format_id` should always be populated when `metadata.json` exists (PRD 4 §4.3 requires `info.json` present at creation, which carries `format_id`). If a null ever appears in the field, it's bad data; flagging an upgrade surfaces the problem and lets the next `--upgrade` run repopulate it correctly, rather than silently masking the issue with a skip or "no upgrade" fallback.
6. **History growth — should we add a max-entries-per-video cap** like "keep last 100 history entries"? Currently unbounded. If creator-side title editing becomes a habit, history could balloon. Likely not a problem for v1 but flag for monitoring.
