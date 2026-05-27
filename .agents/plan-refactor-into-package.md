# Plan: Split `archive.py` into a PRD-aligned package

## Context

`archive.py` started as an intentionally single-file orchestrator — that constraint made sense when the target was 100–200 lines and the rationale (stdlib-only, no `pip install`) is documented in README.md:206 and `.agents/youtube-archive-plan.md:18`. The file is now ~2100 lines covering five implemented PRDs, and navigation has crossed the point where keeping it monolithic costs more than it saves.

Refactor goal: keep `archive.py` as the CLI entrypoint, move the per-PRD logic into a `youtube_archive/` package, and extract shared PRD-01 plumbing into focused utility modules. Stdlib-only, single-binary deployment, and the existing public CLI surface (`uv run archive.py …`) are preserved. No behavior changes — pure mechanical move.

A read-only dependency audit (logged in this session) confirmed the proposed cluster split is fully **acyclic**, so no shim modules or lazy imports are needed.

## Target structure

```
Code/Youtube-Archive/
├── archive.py                  # CLI entry: main(), build_parser(), RunContext, dispatch
└── youtube_archive/
    ├── __init__.py             # empty (or re-exports if needed)
    ├── config.py               # PRD-01: config loading/validation, env setup, ffmpeg check
    ├── logging_setup.py        # PRD-01: UTCFormatter + per-creator loggers
    ├── process.py              # PRD-01: subprocess helpers
    ├── errors.py               # PRD-01: exception types + error-handling helpers
    ├── utils.py                # PRD-01: timestamps, atomic IO, generic value helpers
    ├── manifests.py            # PRD-02: pass 1
    ├── downloads.py            # PRD-03: pass 2
    ├── metadata.py             # PRD-04: pass 4 + creator.json
    └── refresh.py              # PRD-05: refresh mode + upgrade detection
```

Once approved, copy this plan into `.agents/` per the project convention.

## Per-module contents

Line numbers reference the current `archive.py`.

**`archive.py` (entry — stays at repo root)**
- `RunContext` (33), `main` (115), `build_parser` (142)
- Top-level constants currently at module scope: `CONFIG_PATH` (25), `DATA_DIR` (26), `SLUG_PATTERN` (27) → move to `config.py`; `archive.py` imports them if it needs them, otherwise drops them.
- `logging.addLevelName(logging.WARNING, "WARN")` (29) → move to `logging_setup.py` so the side effect runs on first logger access.

**`config.py`**
- `ConfigError` (38)
- `load_config` (184), `validate_config` (200), `_validate_config` (208)
- `_require_non_empty_string` (322), `_optional_string_list` (329), `_optional_non_negative_int` (340)
- `select_creators` (351)
- `check_ffmpeg` (368), `_ffmpeg_error` (383)
- `setup_creator_environment` (391) — also calls into `logging_setup.get_creator_loggers`
- Constants: `CONFIG_PATH`, `DATA_DIR`, `SLUG_PATTERN`

**`logging_setup.py`**
- `UTCFormatter` (105), `LOG_FORMATTER` (109)
- `get_creator_loggers` (2028), `_configure_creator_logger` (2039)
- Module-level `logging.addLevelName(logging.WARNING, "WARN")` (moved from line 29)

**`process.py`**
- `YtDlpResult` (1902)
- `run_yt_dlp_capture` (1912)
- `run_subprocess_streaming` (2059), `_pump_stream_to_logger` (2095)

**`errors.py`**
- `ChannelLevelFailure` (42), `PlaylistFetchFailure` (49)
- `creator_scope` (2104), `_handle_creator_failure` (2111)
- `log_channel_level_failure` (1926), `log_safe_detail` (1950)
- `last_five_lines` (1339)

**`utils.py`** (shared helpers used by ≥2 feature modules)
- `utc_timestamp` (2002), `utc_date_string` (2006), `iso_timestamp` (1972)
- `write_json_atomic` (2010), `atomic_write_bytes` (2020)
- `string_or_empty` (1530), `codec_or_none` (1534)
- `optional_int_value` (1954), `optional_string_value` (1964), `parse_printed_int` (1968)
- `parse_archive_video_ids` (1361) — used by both `downloads.read_download_archive` and `metadata.read_archive_video_ids_for_metadata`, so it lives here rather than in either feature module

**`manifests.py` (PRD-02)**
- `run_pass_one` (410), `_run_pass_one` (433)
- `resolve_canonical_channel` (1540)
- `fetch_playlists_index` (1685), `compute_final_playlist_set` (1731), `extract_playlist_id_from_url` (1783)
- `fetch_playlist_manifest` (1793), `fetch_uploads_manifest` (1848)
- `parse_json_bytes_or_channel_failure` (1887), `merge_manifest_entry` (1976), `append_candidate_video_id` (1991)
- `write_creator_json` (1596), `read_existing_creator_handle` (1634)
- `required_payload_string` (1645), `optional_payload_int` (1655)
- `extract_avatar_url` (1662), `extract_banner_url` (1672)

**`downloads.py` (PRD-03)**
- Dataclasses: `EligibleVideo` (56), `GatedVideo` (62), `WorkBuckets` (69), `DownloadResult` (77)
- `run_pass_two` (541), `build_download_work_list` (603)
- `read_download_archive` (658) — calls `utils.parse_archive_video_ids`
- `extract_eligibility_metadata` (672), `fetch_eligibility_metadata` (680)
- `classify_video` (708), `log_gated_video` (744)
- `probe_subtitles` (768), `pick_subtitle` (802), `_first_glob_match` (822), `resolve_subtitle_choice` (827)
- `build_download_command` (857), `run_download_subprocess` (901)

**`metadata.py` (PRD-04)**
- `should_run_metadata_pass` (925), `run_pass_four` (936)
- `read_archive_video_ids_for_metadata` (1344) — calls `utils.parse_archive_video_ids`
- `process_metadata_video` (1373)
- `build_metadata_from_info` (1437), `build_metadata_dict` (1464)
- `description_hash` (1506), `metadata_filesize` (1513), `copy_playlist_membership` (1523)

**`refresh.py` (PRD-05)**
- Dataclasses: `RefreshInvocationResult` (83), `RefreshVideoResult` (90), `RefreshCounts` (96)
- `run_refresh_mode` (976), `run_refresh` (996), `refresh_one_video` (1046)
- `refresh_video_metadata` (1125), `classify_refresh_stderr` (1169)
- `apply_metadata_refresh` (1192), `apply_availability_refresh` (1237), `apply_upgrade_detection` (1265)
- `selected_format_descriptor` (1298), `refresh_filesize` (1309), `count_current_upgrades` (1316)

## Import graph (final state)

```
archive.py            → config, logging_setup, errors, manifests, downloads, metadata, refresh
manifests.py          → errors, logging_setup, process, utils
downloads.py          → errors, logging_setup, process, utils
metadata.py           → logging_setup, utils
refresh.py            → errors, logging_setup, utils, metadata, config (for setup_creator_environment)
config.py             → logging_setup
process.py            → (stdlib only)
errors.py             → (stdlib only)
utils.py              → (stdlib only)
logging_setup.py      → (stdlib only)
```

Acyclic, verified by the dependency audit. The only feature-to-feature edge is `refresh → metadata` (for `read_archive_video_ids_for_metadata`), which is one-directional.

## Execution steps

1. Create `youtube_archive/__init__.py` (empty).
2. Create `utils.py`, `logging_setup.py`, `process.py`, `errors.py`, `config.py` — move their functions/classes from `archive.py`, including any required stdlib imports. Run `uv run python -c "import youtube_archive.config"` etc. after each module to catch missing imports immediately.
3. Create `manifests.py`, `downloads.py`, `metadata.py`, `refresh.py` — same drill, but each will need `from youtube_archive.utils import …` etc. at the top.
4. Reduce `archive.py` to its entry-point role: imports from each module, defines `RunContext` + `build_parser` + `main`, keeps the `if __name__ == "__main__": main()` guard at the bottom (currently implicit — verify it's present, add if missing).
5. Confirm no symbol is defined in two places; confirm no symbol is imported but unused (an unused-import pass at the end is a natural cleanup).

## Critical files to modify

- `/Users/kwarner/Code/Youtube-Archive/archive.py` — shrinks to ~150 lines (entrypoint only)
- `/Users/kwarner/Code/Youtube-Archive/youtube_archive/__init__.py` — new
- `/Users/kwarner/Code/Youtube-Archive/youtube_archive/{config,logging_setup,process,errors,utils,manifests,downloads,metadata,refresh}.py` — new

## Documentation updates (in the same commit)

- `README.md:206` — change "Single file. Everything lives in `archive.py`." to reflect the new package layout. Keep the stdlib-only and no-`pip install` claims, since both remain true.
- `.agents/youtube-archive-plan.md:18` — similar tweak; note that the single-file phase ended once the project grew past five PRDs of complexity.

PRDs themselves don't need edits — they describe behavior, not file layout.

## Verification

End-to-end smoke (no behavior changes expected):

1. `uv run python -c "import archive; import youtube_archive"` — import sanity (no circular imports, no missing symbols).
2. `uv run archive.py --help` — CLI parser still wires up.
3. `uv run archive.py` on a real `config.toml` with at least one creator that already has a populated `data/<slug>/` tree — confirm pass 1 (manifest fetch), pass 2 (downloads, even if nothing new), and pass 4 (metadata) all run and produce the same log lines as before the refactor.
4. `uv run archive.py --refresh-metadata` against the same creator — confirm refresh mode still executes and writes `refresh.log` entries.
5. `git diff --stat` should show a large negative in `archive.py` and matching positives across the new modules, with no net behavioral change. Spot-check a few moved functions to confirm bodies are byte-identical to the originals.
6. Optional: run an unused-import pass (`ruff check --select F401` or eyeball the imports in each new module) and trim dead imports left behind by the move.

## Out of scope

- No behavior changes, no API renames, no signature tweaks.
- No new abstractions or shared base classes — straight code move.
- No test additions (the project has no test suite yet; verification is the smoke run above).
- Existing TODO/comment cleanup — defer to a separate pass.
