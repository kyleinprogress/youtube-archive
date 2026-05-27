# PRD 02 — Manifest Discovery (Pass 1)

## 1. Introduction / Overview

This PRD covers **Pass 1** of the archive pipeline: the read-only metadata sweep that, for each creator, builds the authoritative on-disk record of *what currently exists* on their channel. No media is downloaded here. The output is two things:

1. A set of JSON snapshots on disk (`creator.json`, `manifests/channel/*`, `manifests/playlists/*`) that capture the channel's playlist structure and full upload history as of *now*, with a parallel set of timestamped copies that preserve the historical record across runs.
2. An in-memory **candidate video-ID set** (the union of all video IDs seen across all manifests) plus a per-creator **proceed flag**, both consumed by PRD 3 to drive Pass 2.

When this PRD ships, `uv run archive.py --manifests-only` works end-to-end. See [`youtube-archive-plan.md`](../youtube-archive-plan.md) §"Pass 1 — Manifests" and [`prd-overview.md`](../prd-overview.md) for surrounding context.

The **central correctness point** of the entire design — playlist membership being derived from these per-playlist manifests rather than from per-video `info.json` — depends on this PRD producing accurate manifests. PRD 4 will later read what we write here.

## 2. Goals

1. After a successful run, every creator has an up-to-date `creator.json`, `manifests/channel/playlists_index_latest.json`, `manifests/channel/uploads_latest.json`, and one `manifests/playlists/<PLAYLIST_ID>_latest.json` per active playlist — plus a same-day timestamped copy of each.
2. Every yt-dlp invocation in Pass 2 (built by PRD 3) and Pass 3 (PRD 5) can use the canonical `https://www.youtube.com/channel/UC...` URL stored in `creator.json`, so handle renames never silently break archive runs.
3. A transient failure on one playlist out of forty does not kill the run for that creator — the other thirty-nine still get fetched, and the candidate set returned to PRD 3 reflects what we successfully learned.
4. A failure on a channel-level fetch (canonical resolution, channel `/playlists`, or channel `/videos`) sets a flag that PRD 3 reads to refuse downloading for that creator on this run.
5. `uv run archive.py --manifests-only` runs Pass 1 for every selected creator and exits cleanly with no media touched.

## 3. User Stories

- **As the developer**, I want to add a creator with `channel_url = "https://www.youtube.com/@theircustomname"` and have it Just Work — without me having to look up their `UC...` ID by hand.
- **As the developer**, when a creator renames their YouTube handle, I want my archive to keep working without any config change because the canonical URL is what's actually being used.
- **As the developer**, when I run `--manifests-only` weekly, I want a date-stamped record of what playlists existed each week so I can later answer "when did this playlist get deleted?" by reading the manifest files.
- **As the developer**, when a single playlist fetch fails with a transient network blip, I want a clear `WARN` line in the log and the run to continue, not a crashed run.
- **As the developer**, when something *substantively* fails (canonical resolution, uploads tab, channel playlists tab), I want PRD 3 to skip downloading for that creator on this run rather than archive against stale knowledge.

## 4. Functional Requirements

### 4.1 Channel URL resolution (canonical lookup)

1. For each selected creator, the **first** yt-dlp call MUST resolve the user-provided `channel_url` to its canonical form `https://www.youtube.com/channel/<CHANNEL_ID>`.
2. The resolution call MUST accept any URL form yt-dlp itself accepts: `https://www.youtube.com/@handle`, `https://www.youtube.com/channel/UC...`, `https://www.youtube.com/c/customname`, `https://www.youtube.com/user/legacyname`.
3. The resolution call MUST NOT enumerate videos (use `--playlist-items 0` or equivalent — see §7).
4. The extracted fields used downstream are: `channel_id` (string, starts with `UC`), `channel` (the human-readable channel title), `uploader_id` (the `@handle`, including the `@`), `description`, `channel_follower_count` (integer; may be `null` if yt-dlp doesn't report it), `thumbnails` (list; first entry is the avatar by convention).
5. If resolution fails (network error, channel deleted, URL doesn't resolve, channel_id missing from the response), this is a **channel-level failure** (see §4.9).
6. The canonical URL `https://www.youtube.com/channel/<CHANNEL_ID>` MUST be used as the prefix for every subsequent yt-dlp call in PRD 2 and made available in-memory for PRDs 3 and 5.

### 4.2 `creator.json` channel-level snapshot

7. After successful canonical resolution, the script MUST write `data/<slug>/creator.json` with the following schema. Overwrite the file each run; do not write timestamped copies.

```json
{
  "config_slug": "creator-name",
  "config_channel_url": "https://www.youtube.com/@whatever-was-in-config",
  "canonical_url": "https://www.youtube.com/channel/UC...",
  "channel_id": "UC...",
  "handle": "@creator",
  "title": "Creator Display Name",
  "description": "Their channel description...",
  "subscriber_count": 123456,
  "avatar_url": "https://yt3.ggpht.com/...",
  "banner_url": "https://yt3.ggpht.com/...",
  "snapshot_taken_at": "2026-05-26T15:30:42Z"
}
```

8. `subscriber_count`, `avatar_url`, and `banner_url` MAY be `null` if the upstream response doesn't include them. All other fields are required.
9. `snapshot_taken_at` MUST be an ISO 8601 UTC timestamp.
10. If the existing `creator.json` (from a prior run) has a different `handle` than the new one, the script MUST log a single `INFO` line to `manifests.log`: `handle renamed: <old> -> <new> (canonical URL unchanged)`. The file is still overwritten with the new handle. No history is kept on disk in v1 (see Open Questions).

### 4.3 Channel `/playlists` discovery snapshot

11. After `creator.json` is written, the script MUST call `yt-dlp --flat-playlist --dump-single-json <canonical_url>/playlists` and capture the JSON output.
12. The raw output MUST be written verbatim to `data/<slug>/manifests/channel/playlists_index_latest.json` (overwritten each run).
13. A copy MUST also be written to `data/<slug>/manifests/channel/playlists_index_<YYYY-MM-DD>.json`, where the date is the UTC date of the run. Same-day re-runs overwrite this file — that's intentional.
14. From the parsed output, the script MUST extract the list of discovered playlist IDs (each entry's `id` field, which is a YouTube playlist ID starting with `PL`, `OL`, `LL`, etc.).
15. If the channel `/playlists` call fails or returns malformed JSON, this is a **channel-level failure** (§4.9).
16. A channel having zero public playlists is **not** an error. Proceed with an empty discovered set.

### 4.4 Final playlist set computation

17. The script MUST compute the **final playlist set** for the run as follows:
    - Start with the discovered playlist IDs from §4.3.
    - Extract a playlist ID from each entry in the creator's `extra_playlists` (config field) by parsing the `list=` query parameter from the URL. If a URL doesn't contain a `list=` parameter, log a `WARN` to `manifests.log` (`extra_playlists entry has no list= param, skipping: <url>`) and skip it.
    - Union the two sets, deduplicating by playlist ID. If an extra_playlists entry duplicates a discovered playlist, log a single `INFO` (`extra_playlists entry already discovered: <id>`) and treat it as one entry.
    - Apply the `exclude_playlists` filter: remove any playlist ID present in the creator's `exclude_playlists` config field.
18. The final set is what gets fetched in §4.5. The discovery snapshot in §4.3 is **not** retroactively modified to add `extra_playlists` entries (they didn't come from discovery; the snapshot records what discovery returned).

### 4.5 Per-playlist manifest fetches

19. For each playlist ID in the final set, the script MUST call `yt-dlp --flat-playlist --dump-single-json https://www.youtube.com/playlist?list=<PLAYLIST_ID>` and write the raw output to:
    - `data/<slug>/manifests/playlists/<PLAYLIST_ID>_latest.json` (overwritten each run)
    - `data/<slug>/manifests/playlists/<PLAYLIST_ID>_<YYYY-MM-DD>.json` (same-day overwrite)
20. After a successful playlist fetch, the script MUST extract every entry's `id` field (YouTube video ID) and add it to the in-memory **candidate video-ID set** for this creator. Each entry also retains its `index` (1-based position in the playlist) and the parent playlist's `title` — this triple `(playlist_id, playlist_title, index)` is what PRD 4 will read for membership.
21. If an individual playlist fetch fails for any reason (network error, playlist deleted, malformed response), this is a **per-playlist failure**: log a `WARN` line to `manifests.log` (`playlist fetch failed: <id> — <one-line error>`) AND an `ERROR` line to `errors.log` (`playlist <id>: <full yt-dlp stderr>`), omit that playlist from the candidate set contribution, and continue to the next playlist. The latest/timestamped files for that playlist are NOT updated; the previous run's files (if any) remain on disk untouched.
22. Empty playlists (zero entries) are **not** errors. They produce a written manifest with an empty `entries` array and contribute zero IDs.

### 4.6 Channel uploads tab snapshot

23. After per-playlist fetches complete, the script MUST call `yt-dlp --flat-playlist --dump-single-json <canonical_url>/videos` and write the raw output to:
    - `data/<slug>/manifests/channel/uploads_latest.json` (overwritten each run)
    - `data/<slug>/manifests/channel/uploads_<YYYY-MM-DD>.json` (same-day overwrite)
24. Every video ID from the uploads response MUST be added to the candidate video-ID set.
25. If the uploads tab fetch fails, this is a **channel-level failure** (§4.9). The candidate set from this creator is still returned with whatever we did learn from per-playlist fetches, but the channel-level failure flag is set.

### 4.7 Candidate video-ID set

26. The script MUST produce, for each creator processed, an in-memory data structure that PRD 3 will consume. The minimum shape:
    ```python
    {
      "slug": "creator-name",
      "canonical_url": "https://www.youtube.com/channel/UC...",
      "candidate_video_ids": ["abc123", "def456", ...],
      "channel_level_failed": False,
      "playlist_membership": {
          "abc123": [{"id": "PLxyz", "title": "Best Videos", "index": 7}, ...],
          ...
      }
    }
    ```
27. `candidate_video_ids` is an ordered, deduplicated list (first-seen order across all manifest sources, for predictable logging).
28. `playlist_membership` is keyed by video ID and lists every `(playlist_id, playlist_title, index)` triple that referenced that video across all *successful* per-playlist manifests in §4.5. Videos that appeared only in the uploads tab will have an empty list (`[]`). PRD 4 reads this structure verbatim.
29. `channel_level_failed` is `True` if §4.1, §4.3, or §4.6 failed. PRD 3 MUST refuse to download for any creator with this flag set.

### 4.8 `--manifests-only` mode

30. When `--manifests-only` is passed, the script MUST run Pass 1 for every selected creator and then exit with code `0`. It MUST NOT invoke any Pass 2 or Pass 3 logic.
31. Exit code is `0` even if some creators had channel-level failures, as long as the script itself didn't crash. Failures are reported via log files and a one-line console summary.
32. Default invocation (no `--manifests-only`) runs Pass 1 followed by Pass 2 (Pass 2 itself is PRD 3; this PRD only needs to call into it with the candidate set).

### 4.9 Failure handling

33. **Channel-level failures** (canonical resolution, channel `/playlists`, channel `/videos`):
    - Log a single `ERROR` line to `manifests.log` AND `errors.log` naming the failure phase and the yt-dlp stderr.
    - Set `channel_level_failed = True` for this creator.
    - SKIP any remaining Pass 1 steps for this creator that depend on the failed step. Specifically: if canonical resolution fails, skip *everything* for this creator (no `creator.json`, no manifests). If channel `/playlists` fails, still attempt the uploads tab (uploads is independent of the playlists listing); per-playlist fetches are skipped because the discovered set is unknown — extra_playlists entries can still be fetched. If uploads tab fails, we already have whatever per-playlist data succeeded.
    - Print one line to stderr: `error: <slug>: channel-level failure during <phase> — see data/<slug>/logs/errors.log`.
    - Move on to the next creator.
34. **Per-playlist failures** (§4.5):
    - Log to `manifests.log` (`WARN`) and `errors.log` (`ERROR`).
    - Do not affect any other playlist.
    - Do not set `channel_level_failed`.
35. The outer `creator_scope` helper from PRD 1 (§4.9 of PRD 1) catches any unexpected exception inside a creator's Pass 1 and routes it like a channel-level failure: log, mark failed, move on.

### 4.10 Logging output for this pass

36. `manifests.log` MUST receive an `INFO` line at the start and end of each creator's Pass 1, naming the phase transitions. Minimum lines per creator:
    - `INFO Pass 1 starting`
    - `INFO canonical URL resolved: <canonical_url>`
    - `INFO playlists discovered: N` (after §4.3)
    - `INFO final playlist set: N (discovered: D, extra: E, excluded: X)` (after §4.4)
    - One `INFO playlist fetched: <id> (N videos)` per successful playlist
    - `INFO uploads manifest fetched: N videos`
    - `INFO Pass 1 complete: M candidate video IDs`
37. `WARN` and `ERROR` lines are written as described in §§4.9.
38. Console output for this pass is limited to the `==> <slug>` header (already established by PRD 1) and any stderr error lines per §4.9. Per-playlist progress goes only to log files.

## 5. Non-Goals (Out of Scope)

- **Downloading any media.** That's PRD 3.
- **Writing `metadata.json` files** for individual videos. That's PRD 4.
- **Cross-creator deduplication.** If two configured creators happen to have the same video in a playlist (e.g., a collab), each creator's manifest records its own view. Pass 2 then downloads the video into both creator subtrees — that's by design (subtrees are self-contained).
- **Detecting handle renames or channel ID changes across runs beyond a single `INFO` log line.** No on-disk history of past handles.
- **Pruning stale `manifests/playlists/<PLAYLIST_ID>_latest.json` files** when a playlist is deleted from the channel. The historical timestamped copies are what record the deletion (the next-day run won't write a new latest, but the old latest stays).
- **Retrying failed fetches with custom backoff.** Trust yt-dlp's built-in retry behavior; do not layer a second retry loop on top.
- **Members-only / private content** that requires cookies or auth. Out of scope for v1.
- **Validating that `extra_playlists` URLs point at YouTube** beyond extracting `list=`. yt-dlp will surface the error if it's bogus.
- **A standalone `--validate-config` or `--list-creators` flag.** Out of scope.

## 6. Design Considerations

- **Order of operations per creator**: (1) canonical resolution → (2) `creator.json` write → (3) channel `/playlists` discovery + snapshot → (4) compute final playlist set → (5) per-playlist fetches → (6) uploads tab fetch → (7) assemble candidate set. Steps (5) and (6) could run in parallel for speed, but v1 runs them sequentially for simpler logging and easier debugging.
- **Same-day overwrite is intentional.** Running `--manifests-only` twice in one day produces no extra history. If you want sub-day granularity, run on different UTC dates. (Open Question: do we want optional second-granularity timestamps?)
- **The discovery snapshot (§4.3) records reality, not the run's working set.** That's why `extra_playlists` are deliberately *not* written into `playlists_index_<date>.json` — that file's job is to answer "what did discovery return on this date?", and `extra_playlists` are a config-level override, not a discovery result.
- **Raw passthrough.** Manifest files contain the verbatim yt-dlp `--dump-single-json` output. We do **not** trim, normalize, or reformat. The whole point of a snapshot is that it's the original response; downstream code (PRD 4) reads only the fields it needs.
- **`creator.json`** is the one exception: it's our own schema, populated from the resolution call's output. We curate it because we want a stable, documented shape that's safe to read across PRDs.

## 7. Technical Considerations

- **PRD 1 helpers**: this PRD uses `get_creator_loggers(slug)` (or whatever the PRD 1 helper ended up named) for `manifests_log` and `errors_log`, and the subprocess streaming helper for running yt-dlp. It uses the `creator_scope(slug)` boundary so unexpected exceptions get caught and routed to errors.log + next creator.
- **Canonical resolution invocation** (suggested): `yt-dlp --skip-download --no-warnings --playlist-items 0 --dump-single-json "<user_channel_url>"`. The `--playlist-items 0` flag prevents yt-dlp from iterating through every uploaded video while still letting it resolve and dump the channel metadata. If this doesn't return what's needed, the alternative is `<user_channel_url>/about` which is the lightest channel page.
- **Playlist ID extraction from URLs** (§4.4): use `urllib.parse.urlparse` + `parse_qs` to read the `list=` query parameter. No regex needed. Handles both `playlist?list=...` and `watch?v=...&list=...` forms.
- **Timestamp format**: ISO 8601 UTC with `Z` suffix for `snapshot_taken_at` and log lines (`datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')`). Date-only strings (`%Y-%m-%d`, UTC) for snapshot filenames.
- **JSON write atomicity**: write to a `.tmp` sibling, then `os.replace()` onto the final name. Prevents a half-written file from being read by a concurrent process (unlikely in v1, but cheap insurance).
- **yt-dlp version**: 2026.03.17 is installed system-wide and is the assumed target. Don't add a Python `yt_dlp` library dependency — invoke the CLI per PRD 1's pattern.
- **yt-dlp retry behavior**: yt-dlp retries network errors by default (`--retries 10`). Do not layer an additional retry loop. If a fetch fails after yt-dlp's own retries, treat it as a real failure per §4.9.
- **JSON parsing**: stdlib `json`. The flat-playlist dumps for large channels can be 1-10 MB. Stream-parsing isn't needed; `json.loads()` is fine for these sizes.

## 8. Success Metrics

This PRD is done when **all** of the following are true:

1. **Canonical resolution works for every URL form.** For a test creator with all four URL forms (`@handle`, `/channel/UC...`, `/c/custom`, `/user/legacy`) pointing at the same channel, each form in `channel_url` produces an identical `creator.json` (same `canonical_url`, same `channel_id`).
2. **`creator.json` is well-formed and contains the documented fields.** `python -m json.tool data/<slug>/creator.json` parses cleanly and every required field is present.
3. **Playlist discovery snapshot exists.** `data/<slug>/manifests/channel/playlists_index_latest.json` exists and lists every playlist visible on `https://www.youtube.com/@creator/playlists`. A same-day timestamped copy exists alongside.
4. **Per-playlist manifests exist for every discovered playlist.** `ls data/<slug>/manifests/playlists/` shows two files per playlist (one `_latest.json`, one `_<date>.json`).
5. **Uploads manifest exists.** `data/<slug>/manifests/channel/uploads_latest.json` exists; same-day timestamped copy alongside.
6. **`extra_playlists` works** — adding an arbitrary public YouTube playlist URL (even from another channel) to `extra_playlists` causes its manifest to appear under `manifests/playlists/` after the next run.
7. **`exclude_playlists` works** — adding a playlist ID to `exclude_playlists` makes its manifest *not* be written on the next run (the previous run's files remain — we don't delete history).
8. **`--manifests-only` is a no-op for media** — running it never creates files under `data/<slug>/videos/` and never appends to `data/<slug>/archive.txt`.
9. **Per-playlist failure isolation** — temporarily change one playlist's URL in `extra_playlists` to a known-deleted playlist ID. The run logs a `WARN` and `ERROR`, the other playlists still get their manifests written, the candidate set is returned, and the run exits `0`.
10. **Channel-level failure isolation** — temporarily change `channel_url` for one creator to a known-bad URL. The run logs an `ERROR` for that creator, the canonical resolution fails, no manifest files are written for that creator, the next creator in the config is still processed normally, and the run exits `0`.
11. **Handle rename detection** — manually edit `creator.json`'s `handle` field to something different, run again with `--manifests-only`. The `manifests.log` contains the documented `handle renamed: ...` `INFO` line and the file is overwritten with the correct handle.
12. **Empty channel playlist list** — point a creator at a channel with no public playlists. Run completes successfully; `playlists_index_latest.json` is written with an empty entries array; uploads manifest is still produced.

These map onto **verification items #2, #4, #7, and #8** from the plan.

## 9. Open Questions

1. **Should `creator.json` retain a `handle_history` array** so handle renames are queryable later? Defaults to "no, just the log line" per current spec. Cheap to add later if useful.
2. **Should snapshot timestamps include time-of-day (`YYYY-MM-DDTHHMMSSZ`) instead of date-only?** Date-only matches the plan; sub-day granularity could be added as a CLI flag (e.g., `--snapshot-second-granularity`). Not in v1.
3. **Should we delete stale `_latest.json` files** when a playlist disappears from discovery? Currently no — the stale file is the last known good snapshot. A future cleanup PRD could prune them by comparing against the latest `playlists_index`.
4. **Should the candidate set include `extra_playlists` video IDs even when the discovered set's `/playlists` fetch fails?** ✅ Resolved (2026-05-26): yes, current behavior is correct. A single playlist deletion or rename shouldn't block the rest of the process when there's still content to grab — only a whole-channel failure should stop a creator. The `channel_level_failed = True` flag still blocks Pass 2 downloads for this creator, so the partial candidate set is informational (recorded in manifests for posterity) rather than acted on.
5. **Should we record a `manifest_run_summary.json`** per run (timestamps, success/fail counts, list of fetched playlists) to make post-run inspection easier? Out of scope for v1 but a natural future addition.
