# Backlog

Future enhancements deferred until after PRDs 1-8 are complete. Each entry is a sketch — promote to a full PRD in `.agents/prd/` when ready to implement.

---

## Authentication (cookie support for members-only / private content)

**Motivation:** The operator may be a paid member / channel sponsor for some configured creators and wants to archive members-only videos. Authentication is also needed for age-restricted content that requires an age-confirmed YouTube account and for any private videos shared with the operator's account.

Currently the script makes anonymous requests through yt-dlp, which means members-only and certain age-gated videos either fail to download or fall back to public formats only.

**Scope of a future PRD:**

- Add per-creator and global config fields for authentication. Two mechanisms, mutually exclusive:
  - `cookies_from_browser` (string) — passed as `--cookies-from-browser <name>` to yt-dlp. Values: `firefox`, `chrome`, `brave`, `edge`, `safari`, etc. Firefox is most reliable; Chrome-family browsers encrypt their cookie database on modern versions and sometimes need extra setup.
  - `cookies_file` (string, absolute path) — passed as `--cookies <path>`. The user exports a Netscape-format cookies file using a browser extension like "Get cookies.txt LOCALLY".
- If both are set on the same creator, that's a config validation error.
- Per-creator overrides shadow `[defaults]` like the other settings.
- Inject the relevant flag into every yt-dlp invocation that hits a creator's content:
  - `resolve_canonical_channel` (PRD 2 §4.1) — needs auth to resolve channels with members-only content properly.
  - `fetch_playlists_index`, `fetch_playlist_manifest`, `fetch_uploads_manifest` (PRD 2 §4.3, §4.5, §4.6) — members-only playlists need auth to enumerate.
  - `build_download_command` (PRD 3 §4.6) — the actual download.
  - `probe_subtitles` and `fetch_eligibility_metadata` (PRD 3, subtitle preference feature) — same per-video calls.
  - `refresh_video_metadata` (PRD 5 §4.2) — the `--refresh-metadata` per-video call.
  - PRD 6's upgrade re-download.
- Factor the "build the auth flags" logic into a helper so all the above call sites use the same code path.

**Operational considerations:**

- **Cookie staleness.** YouTube cookies expire (typically days to weeks). When they do, all the above calls start failing with auth errors. The script should surface this cleanly — a stderr message like `error: <slug>: authentication appears to have failed; refresh your cookies and re-run` rather than burying it in `errors.log`.
- **Cookie security.** Cookies grant full account access. The cookies file (or browser profile path) should never be logged. The script should not echo the cookies-related yt-dlp flags into `download.log` either — currently every command line gets implicitly visible through yt-dlp's own startup output. Worth checking that `--no-progress` plus our existing log filters keep cookies out of the logs.
- **Per-creator vs global.** Most operators will set this once in `[defaults]` and be done. The per-creator override is for niche cases (different account per creator, e.g., your account for one channel and a shared family account for another). v1 could ship with `[defaults]`-only and add per-creator later if needed.
- **`config.toml` gitignore status.** Already gitignored, so cookie paths stored there don't leak.

**Verification metrics (sketch):**

- Configure `cookies_from_browser = "firefox"` against a creator whose channel has at least one members-only video. Confirm the members-only video is in the candidate set (Pass 1 sees it) and downloads successfully (Pass 2 fetches the higher-quality format that members get).
- Configure `cookies_file = "/path/to/exported.txt"` and confirm same.
- Configure both → exit 2 with a config validation error.
- Without auth configured, confirm the existing anonymous-access behavior for public channels is unchanged.
- Forcibly expire cookies (e.g., delete the cookies file or log out of the browser) and confirm the script surfaces a clear auth-failure message rather than silently degrading.

**Dependencies:** None on PRDs 6-8 specifically, but cleaner to land after them so the auth wiring covers every code path (PRD 6's upgrade, PRD 7's dry-run, PRD 8's audit are all yt-dlp-free so no auth needed there — but worth verifying).

---

## Browse UI / archive index

**Motivation:** The on-disk layout uses raw YouTube IDs as folder names (`data/<slug>/videos/<ID>/`), which is filesystem-safe and stable but hostile to humans. The operator can't tell what's in `data/christine-payton/videos/Nt3QgZrJXfY/` without opening `metadata.json` or `info.json` and reading the title. As archives grow into the hundreds or thousands of videos per creator, navigating directly via Finder/`ls` becomes unworkable. A simple browsable interface — backed by the existing `metadata.json` and `creator.json` files — would make the archive actually usable as a personal library.

**Scope of a future PRD:**

Primary approach: **static HTML generator**, stays stdlib-only.

- New subcommand: `uv run archive.py --build-index` (or fold into the default flow as a final phase after PRD 4). Reads every creator's `metadata.json` and `creator.json`, emits a tree of HTML files under `data/_index/` (or `data/index.html` at the top + per-creator subpages).
- **Top-level index** at `data/index.html`:
  - Lists all configured creators with their avatar, display name, channel link, and video count.
  - Cross-creator search box (client-side JS, filtering against an inline JSON blob of all videos).
- **Per-creator page** at `data/<slug>/index.html`:
  - Header with creator metadata (handle, banner, subscriber count from `creator.json`).
  - Video grid showing thumbnail + title + upload date + duration. Pulled from each video's `metadata.json` + `info.json`.
  - Filter UI: by playlist membership (multi-select), by availability state (available/removed/private), by upgrade-available flag, by date range.
  - Sort UI: by upload date, archived date, title.
  - Click a video → opens its detail page (or modal).
- **Per-video page** at `data/<slug>/videos/<ID>/index.html` (sits alongside the media file):
  - Inline `<video>` element pointing at `<ID>.<merge_output_format>` with `<track>` elements for subtitles.
  - Title, description, upload date, playlist memberships (with links), `archived_at`, `last_metadata_check`.
  - Visual badge for `availability != "available"` (removed/private/etc).
  - Visual badge for `upgrade_available != null` showing the better format that's available.
  - History panel showing the metadata-change and upgrade history (from `metadata.json.history`).
  - Link to the original YouTube URL (even when the video is removed — useful for "what was this called again?" lookups).
- **Per-creator log viewer** at `data/<slug>/logs/index.html` (or a tab on the per-creator page):
  - Reads `data/<slug>/logs/{manifests,download,refresh,errors}.log`, splits on newlines, parses the `YYYY-MM-DDTHH:MM:SSZ <LEVEL> <message>` prefix per PRD-01 §29.
  - Renders each line with the UTC timestamp converted to the **browser's local timezone** via `new Date(iso).toLocaleString()` — log files stay UTC on disk (project-wide convention; see archive.py:105 `UTCFormatter` and PRD-01 §29), only the display is localized.
  - Per-log tabs or a unified merged view; level-based coloring (INFO/WARN/ERROR); a "jump to latest" anchor.
  - Optional: filter by level, free-text search, date-range picker. Stretch: tail-mode for `--serve` path (SSE/poll the file for new lines).
- **Templating:** stdlib `string.Template` or `html.escape` + f-strings. Keep it stupid simple — no Jinja, no React. The data shape is fixed and small.
- **No JS framework.** Vanilla JS for the filter/search interactions. The data is small enough (a JSON blob of a few thousand videos is ~megabyte-scale) to fit inline in the HTML.
- **Idempotent regeneration:** `--build-index` re-reads everything and overwrites the HTML. Atomic write via the existing `write_json_atomic`-style pattern (extended for `.html`).

**Secondary approach (follow-up PRD if static isn't enough): local web server.**

If browsing static HTML via `file://` hits limits (CORS issues with subtitle tracks, video seeking quirks, no real search backend), add a `--serve` subcommand that runs `http.server` (stdlib) or upgrades to FastAPI for richer features (HTTP range requests for video seeking, server-side search across large archives, on-the-fly stats). Keep static generation as the primary path so the archive is usable even without the server running.

**Operational considerations:**

- **MKV playback in browsers.** Chrome plays mkv with most common codecs (avc1, vp9, aac, opus). Safari is flaky with mkv — may need fallback to mp4 remux or a recommendation to use Chrome/Firefox for browsing. Worth testing across browsers before committing to mkv-as-canonical.
- **`file://` quirks.** Some browsers restrict file:// access to sibling files (e.g., Chrome blocks reading subtitle `<track>` from disk by default). The local web server path sidesteps this. If sticking with static, document the workaround (`--allow-file-access-from-files` in Chrome, or use the server).
- **Index staleness.** If the index regenerates only on `--build-index`, it'll lag behind the actual archive between runs. Folding it into the default flow (run after PRD 4) keeps it current automatically. Tradeoff: extra disk I/O per run, but cheap for normal archive sizes.
- **Large archives.** For 10,000+ video archives, inlining all metadata as a JSON blob in the top-level HTML becomes unwieldy (multi-megabyte page load). At that scale, switch to per-page pagination or move to the server-based approach.
- **Sensitive data.** Description text can contain personal info, links, etc. The index is for local browsing only — don't accidentally serve it publicly. If `--serve` is added, default to binding to `127.0.0.1` only.
- **Cross-referencing playlist membership.** Already in `metadata.json.playlists`. The UI just renders it as clickable filter chips.

**Verification metrics (sketch):**

- Run `--build-index` against a creator with 50+ archived videos. Confirm `data/<slug>/index.html` exists and renders correctly in Chrome and Firefox. Every video grid card shows the right thumbnail and title.
- Click into a video detail page. Confirm the inline `<video>` plays the mkv, the `<track>` element loads the subtitle file (if present), and the description renders.
- Filter by playlist membership in the per-creator page. Confirm only videos in the selected playlist appear.
- Filter by `availability: "removed"`. Confirm the badge shows and the YouTube link is still present even though the video is gone upstream.
- Hand-edit one `metadata.json` to set `upgrade_available` to a fake descriptor. Re-run `--build-index`. Confirm the upgrade badge appears on that video.
- Regenerate the index a second time with no changes. Confirm the output HTML is byte-identical to the prior run (idempotent).
- Search across the top-level index for a term that appears in multiple creators' video titles. Confirm hits surface from every creator.

**Dependencies:**

- PRD 4 (this is the source of truth for video metadata used by the UI).
- Ideally PRD 5 (so `availability` and `upgrade_available` badges have current data).
- Independent of PRDs 6, 7, 8 — though if PRD 6 runs first, the UI gets the correct `downloaded` block reflecting actual upgrades performed.
- Loosely benefits from the auth backlog item — members-only content will show the same badges and play the same way once auth is wired up.

---

## One-off video archiving (single URL, no channel monitoring)

**Motivation:** Sometimes the operator finds a single video worth keeping — an interview, a one-off talk, a video the algorithm surfaced — from a creator they don't want to subscribe-and-monitor. Today the only options are (a) add the whole channel to `config.toml` and accept ongoing monitoring of every upload, or (b) skip the archive entirely. A `--add-video <URL>` flag would let one-offs land in the archive with the same metadata layer (info.json, metadata.json, thumbnail, subtitles, embedded tags) but without committing to channel-level monitoring.

**Scope of a future PRD:**

- New CLI flag: `--add-video <URL>`. Single-URL form for v1; multi-URL (`--add-videos URL1 URL2 ...`) and file-input (`--add-video-file <path>`) are easy follow-ups but not v1 scope.
- Composable with `--dry-run` (preview which video would be archived). NOT composable with `--creator`, `--manifests-only`, `--refresh-metadata`, `--upgrade`, or `--audit` — each of those is its own focused mode. Combining them with `--add-video` exits 2 with a clear error.
- New on-disk location: `data/_oneoffs/` — a reserved pseudo-creator subtree mirroring the existing layout (`videos/<ID>/`, `archive.txt`, `logs/{download,manifests,errors,refresh}.log`). The leading underscore guarantees it can never collide with a real `[[creator]].slug` value (which must start `[a-zA-Z0-9]`).
- No Pass 1 (manifest discovery) for one-offs. The URL is the input; yt-dlp extracts everything else.
- Pass 2 download reuses the existing `build_download_command` + `run_download_subprocess` helpers. Format settings, subtitle preferences, and merge format all pulled from `[defaults]` (no per-video override knobs in v1 — operator can edit afterward if needed).
- Upload-age gate is **skipped** for one-offs. The operator is explicitly asking for this video; if it's freshly published, download it anyway.
- PRD 4 runs as normal afterward, writing `metadata.json` next to the media file. `source_creator = "_oneoffs"`, `playlists = []` (single-source, no manifest membership). Consider adding a new top-level field `source_channel_id` (and maybe `source_channel_handle`) populated from `info.json` so the original channel's identity isn't lost — useful for the future UI.
- One-offs participate in `--refresh-metadata` and `--upgrade` automatically because they're in `data/_oneoffs/archive.txt` and walked alongside configured creators. (Both passes iterate `archive.txt` files; the pseudo-creator falls in naturally if those walks include `_oneoffs/` in their discovery.)
- One-offs are **not** subject to `--audit` repair semantics differently from configured creators — same Class A through E discrepancy detection applies. The `_oneoffs` subtree just looks like another creator to the audit logic.

**Operational considerations:**

- **Idempotency.** Calling `--add-video <URL>` twice in a row with the same URL should be a no-op on the second call (`archive.txt` already has the ID; yt-dlp's `--download-archive` skip kicks in; PRD 4 hits the re-run path and just rewrites the `playlists` field, which is `[]` either way). Log a friendly "already archived" line instead of doing nothing silently.
- **URL parsing.** Use yt-dlp's own URL handling — accept anything yt-dlp accepts (`watch?v=`, `youtu.be/`, full or short forms). Don't write a custom URL parser; let yt-dlp normalize.
- **Non-video URLs.** If the URL points at a channel or playlist, fail cleanly with `error: --add-video expects a single video URL; got channel/playlist`. yt-dlp can be probed first to determine the URL type, or we can detect it from the response shape.
- **Auth interplay.** One-off videos may be members-only on creators the operator sponsors but doesn't archive in full. Wires up automatically once the auth backlog item lands — `[defaults].cookies_from_browser` / `cookies_file` apply to one-offs too.
- **Cross-creator duplication.** If the operator later adds the source channel as a configured creator, the same video may end up archived twice (once under `_oneoffs/`, once under `<new-creator>/`). The PRD should document this as intentional — subtrees are self-contained per the existing design. If the operator wants to deduplicate, they can manually delete the `_oneoffs/` copy. A future audit-style command could surface cross-subtree duplicates as a Class F discrepancy if it becomes a real pain point.
- **`source_channel_*` fields.** Adding fields to PRD 4's `metadata.json` schema means bumping `schema_version` to `2` and writing a migration helper that promotes v1 files. Worth doing once for this PRD rather than dribbling schema changes later.
- **Logging.** One-off downloads log to `data/_oneoffs/logs/download.log` just like configured creators. Console header: `==> _oneoffs` (or maybe `==> _oneoffs (<ID>)` so it's clear which video is being processed in a single-shot invocation). End-of-run summary line consistent with PRD 3.

**Verification metrics (sketch):**

- `uv run archive.py --add-video "https://www.youtube.com/watch?v=<ID>"` against a public video: confirm `data/_oneoffs/videos/<ID>/{<ID>.mkv, <ID>.info.json, metadata.json, <ID>.webp}` all exist. `metadata.json.source_creator == "_oneoffs"`, `metadata.json.playlists == []`, `archive.txt` has one entry.
- Re-run the same command: confirm zero new downloads, friendly "already archived" log line, file mtimes unchanged.
- `uv run archive.py --add-video <URL> --dry-run`: confirm the URL is reported as "would archive" but no files change on disk.
- `uv run archive.py --refresh-metadata` (no `--creator` flag, with one or more one-offs present): confirm the one-off videos get refreshed alongside any configured creators. Their `last_metadata_check` updates.
- `uv run archive.py --add-video "<channel-URL>"`: confirm exit 2 with a "not a single video URL" message; no files written.
- `uv run archive.py --add-video <URL> --creator some-slug`: confirm exit 2 with a "incompatible flags" message.
- `uv run archive.py --add-video <URL>` against a video freshly uploaded (within `min_upload_age_hours`): confirm it downloads anyway (upload-age gate doesn't apply to one-offs).
- After v2 schema migration: confirm previously-existing v1 `metadata.json` files in real creators' subtrees get promoted on next normal run, and new files (configured creators or one-offs) carry `schema_version: 2` plus the new `source_channel_id` field.

**Dependencies:**

- PRD 1 (CLI parsing, logging, directory setup, error-handling boundary).
- PRD 3 (download pipeline — reuse the command builder and subprocess helper).
- PRD 4 (metadata.json layer — extend the schema with `source_channel_id` and bump `schema_version` if going that route).
- PRD 5 (refresh participation — needs the `--refresh-metadata` walk to include `_oneoffs/` in its creator iteration).
- PRD 6 (upgrade participation — same).
- PRD 8 (audit participation — same).
- Auth backlog item (members-only one-offs).

**Open question:** does it belong as its own PRD (e.g., PRD 10) or as an extension to a future "operational features" PRD? Probably its own PRD — the schema bump alone justifies the focus. Promote when the operator wants to start saving one-offs (likely after the UI lands, since the UI is what makes one-offs actually browsable).
