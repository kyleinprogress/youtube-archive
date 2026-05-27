# PRD 01 — Project Foundation & Config

## 1. Introduction / Overview

The YouTube Archive tool is a single-file Python orchestrator (`archive.py`) that uses the `yt-dlp` CLI to archive specific YouTube creators with full metadata, preserving playlist membership without duplicating media files. See [`youtube-archive-plan.md`](../youtube-archive-plan.md) for the full design and [`prd-overview.md`](../prd-overview.md) for how this PRD relates to the other six.

This PRD covers the **foundation everything else plugs into**: the config file format, the CLI skeleton, per-creator directory creation, the logging infrastructure, the error-handling posture, and the startup `ffmpeg` check. When this PRD ships, the tool will be runnable end-to-end but won't actually do any work — it will parse config, set up directories and logs, and exit cleanly. The behavior of each CLI flag is wired in by later PRDs.

The goal is to make every subsequent PRD trivial to plug in: each later PRD should only have to add its specific functionality without re-deriving config loading, directory paths, logging, or error containment.

## 2. Goals

1. A developer can run `uv run archive.py` against a valid `config.toml` and see clean startup + clean exit with no errors and no spurious files written.
2. An invalid `config.toml` causes a fast, human-readable abort with a specific error message that points at the offending field.
3. Each creator declared in `config.toml` ends up with its own self-contained subtree under `data/<slug>/` containing the expected subdirectories and empty log files, ready to be populated by later PRDs.
4. The seven CLI flags (`--creator`, `--manifests-only`, `--refresh-metadata`, `--upgrade`, `--audit`, `--repair`, `--dry-run`) are parseable and pass-through correctly even though their behavior is implemented in later PRDs.
5. Missing `ffmpeg` is detected at startup and surfaces a clear error before any other work happens.
6. The error-handling posture is established as helper functions / scoping conventions that later PRDs can use without inventing their own.

## 3. User Stories

- **As the developer (also the user)**, I want to clone this repo, create a `config.toml`, and run `uv run archive.py` once to verify everything is wired up before I trust it with actual creators.
- **As the developer**, when I add or rename a creator in `config.toml`, I want the directory tree under `data/` to update automatically on the next run so I never have to `mkdir` by hand.
- **As the developer**, when I mistype a field in `config.toml`, I want the script to refuse to run and tell me exactly what's wrong, rather than silently archiving the wrong thing.
- **As the developer**, when `ffmpeg` isn't installed, I want to know that on the *first* run — not after waiting 20 minutes for a download to finish and fail at the merge step.

## 4. Functional Requirements

### 4.1 Project files

1. The repo MUST contain `archive.py` at the repo root.
2. The repo MUST contain `pyproject.toml` at the repo root. The existing skeleton from `uv init` is acceptable; update the `name` to `youtube-archive` and the `description` to a one-line summary. No runtime dependencies are required (stdlib only).
3. The repo MUST contain `.gitignore` at the repo root excluding at minimum: `data/`, `.venv/`, `__pycache__/`, `*.pyc`.
4. `config.toml` lives at the repo root. It is **not** committed by default — add an example file `config.example.toml` instead, and add `config.toml` to `.gitignore`.

### 4.2 Config file location and loading

5. The script MUST always read config from `./config.toml` (relative to the current working directory). There is **no** `--config` override flag in v1.
6. If `./config.toml` does not exist, the script MUST abort with an error message of the form: `error: ./config.toml not found (cwd: <absolute path>)`.
7. If `./config.toml` exists but is not valid TOML, the script MUST abort with the underlying `tomllib.TOMLDecodeError` message preserved.
8. Config parsing MUST use the stdlib `tomllib` module (Python 3.11+, built into the project's Python 3.14).

### 4.3 Config schema

9. The config file MUST have a `[defaults]` table and at least one `[[creator]]` array-of-tables entry.
10. The `[defaults]` table accepts these keys (all optional except where noted):
    - `format` (string) — yt-dlp `-f` selector. **Required.** No fallback default.
    - `format_sort` (list of strings) — yt-dlp `-S` keys. Optional; defaults to empty list (lets yt-dlp use its built-in sort).
    - `merge_output_format` (string) — yt-dlp `--merge-output-format`. **Required.** No fallback default.
    - `min_upload_age_hours` (integer, ≥ 0) — Optional; defaults to `48`. Set to `0` to disable the gate.
11. Each `[[creator]]` entry MUST contain:
    - `slug` (string) — directory name; see slug validation in 4.4
    - `name` (string) — human display name
    - `channel_url` (string) — must start with `https://www.youtube.com/`
12. Each `[[creator]]` entry MAY contain (all optional):
    - `exclude_playlists` (list of strings) — playlist IDs to skip. Defaults to empty list.
    - `extra_playlists` (list of strings) — playlist URLs to include beyond the channel's `/playlists` tab. Defaults to empty list.
    - `format`, `format_sort`, `merge_output_format`, `min_upload_age_hours` — per-creator overrides that shadow `[defaults]`.
13. Resolved config (after merging `[defaults]` with per-creator overrides) MUST be made available as an in-memory data structure (e.g. a list of dicts) for later PRDs to consume.

### 4.4 Config validation (fail-fast)

14. After parsing, the script MUST validate the config and abort on the **first** violation it finds, printing a message that names the offending field and (where applicable) the creator slug.
15. Validation rules — abort if any of these fail:
    - `[defaults].format` is missing or not a non-empty string.
    - `[defaults].merge_output_format` is missing or not a non-empty string.
    - `[defaults].format_sort`, if present, is not a list of strings.
    - `[defaults].min_upload_age_hours`, if present, is not a non-negative integer.
    - No `[[creator]]` entries exist.
    - Any creator is missing `slug`, `name`, or `channel_url`.
    - Any creator's `slug` is not a non-empty string matching the regex `^[a-zA-Z0-9][a-zA-Z0-9_-]*$` (filesystem-safe; first char alphanumeric; rest may include `_` or `-`).
    - Any creator's `channel_url` does not start with `https://www.youtube.com/`.
    - Any two creators share the same `slug` (case-sensitive comparison).
    - Per-creator override keys, if present, fail the same type checks as their `[defaults]` counterparts.
    - `exclude_playlists` / `extra_playlists`, if present, are not lists of strings.
16. Error messages MUST be one line each and follow the form: `error: config.toml: <field path>: <what's wrong>` — e.g. `error: config.toml: creator[1].slug: must match ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ (got "my creator")`.
17. Exit code on validation failure: `2` (conventional for usage/config errors).

### 4.5 CLI surface

18. CLI MUST be implemented with stdlib `argparse`.
19. The following flags MUST be parseable in any combination (behavior wired in by later PRDs — for now, the script should accept them, store the values, and exit cleanly):
    - `--creator <slug>` — scope a run to one creator slug; defaults to all creators
    - `--manifests-only` — boolean
    - `--refresh-metadata` — boolean
    - `--upgrade` — boolean
    - `--audit` — boolean (PRD 8)
    - `--repair` — boolean (PRD 8; requires `--audit`, validated by PRD 8 §4.6 #24)
    - `--dry-run` — boolean
20. If `--creator` is passed with a slug that doesn't exist in the loaded config, the script MUST abort with: `error: --creator: no creator with slug "<slug>" found in config.toml`. Exit code `2`.
21. `--help` (auto-generated by argparse) MUST list all seven flags with a one-line description each.
22. Exit code on a successful run is `0`.

### 4.6 Directory creation

23. For each creator selected for the run (all creators by default, or the one specified via `--creator`), the script MUST create the following directory tree on startup if it does not already exist:
    ```
    data/<slug>/
    ├── videos/
    ├── manifests/
    │   ├── playlists/
    │   └── channel/
    └── logs/
    ```
24. Directory creation MUST be idempotent (use `mkdir -p` semantics — `Path.mkdir(parents=True, exist_ok=True)`).
25. The top-level `data/` directory is created on demand by the same call.
26. The script MUST NOT create `archive.txt`, `creator.json`, or any files inside `videos/` / `manifests/` — those belong to later PRDs.

### 4.7 Logging infrastructure

27. Each creator MUST get its own three log files inside `data/<slug>/logs/`:
    - `download.log` — Pass 2 download activity (written by PRD 3)
    - `manifests.log` — Pass 1 manifest activity (written by PRD 2)
    - `errors.log` — any errors scoped to that creator (written by all PRDs)
28. Log files are created (touched) by PRD 1 during directory setup so they exist as empty files after the first run.
29. Log format MUST be plain text, append-only, no rotation. Each line: `<ISO 8601 UTC timestamp> <LEVEL> <message>` — e.g. `2026-05-26T15:30:42Z INFO starting manifest discovery for creator-slug`.
30. Levels in use: `INFO`, `WARN`, `ERROR`. (Stdlib `logging` module is fine; configure one logger per creator with a `FileHandler` per log file.)
31. PRD 1 MUST provide a helper API that later PRDs use to get per-creator loggers — e.g. a function `get_creator_loggers(slug) -> (download_log, manifests_log, errors_log)`. The exact signature is up to the implementer, but later PRDs MUST NOT have to know the log file paths or configure handlers themselves.
32. Subprocess output streaming (yt-dlp stdout/stderr → log file) MUST use `subprocess.Popen` + thread/pipe wiring with stdlib only. PRD 1 SHOULD provide a helper that runs a subprocess and streams its output to a given logger; later PRDs use this helper.
33. Console output during a run is minimal: a one-line header per creator (`==> creator-slug`) and any errors. Detailed activity goes to log files only.

### 4.8 ffmpeg startup check

34. On startup, after config is loaded and validated but **before** any per-creator work begins, the script MUST run `ffmpeg -version` as a subprocess (with stdout/stderr suppressed, just checking exit code).
35. If `ffmpeg` is not found on `PATH` (FileNotFoundError) or `ffmpeg -version` exits non-zero, the script MUST abort with: `error: ffmpeg not found or not working — install with: brew install ffmpeg`. Exit code `2`.
36. This check is **unconditional** — it runs even for `--manifests-only` and `--dry-run`. (Rationale: we want a single, simple, predictable startup precondition; we'll relax this later if it proves annoying.)

### 4.9 Error-handling posture (wiring for later PRDs)

37. The main loop MUST iterate over creators with a `try`/`except` boundary so that an exception inside one creator's processing logs to that creator's `errors.log`, prints a one-line error to the console, and then continues to the next creator. The run does not abort.
38. PRD 1 SHOULD provide a context manager or helper function (e.g. `creator_scope(slug)`) that later PRDs use to wrap per-creator work and inherit the error-isolation semantics automatically.
39. Inside a creator, finer-grained `try`/`except` boundaries (e.g. per-video in Pass 2, per-playlist in Pass 1) are the responsibility of the PRD that implements that pass. PRD 1 just ensures the outer boundary exists.

## 5. Non-Goals (Out of Scope)

- **Any actual yt-dlp invocation.** PRD 1 doesn't download, fetch manifests, or call yt-dlp at all (other than the one `ffmpeg -version` subprocess for the availability check).
- **Writing `archive.txt`, `creator.json`, `metadata.json`, or anything inside `videos/` / `manifests/`** — those belong to PRDs 2/3/4.
- **A `--config PATH` flag.** v1 always reads `./config.toml`.
- **Log rotation, structured (JSON) logs, or remote log shipping.** v1 is plain text, append-only, local.
- **Conditional ffmpeg checks** based on which flags are passed — the check is unconditional in v1.
- **Schema migration / forward-compat shims** for future config versions.
- **A `config.toml` schema doc generator or `--validate-config` flag.** The validation logic is internal; users learn the schema from `config.example.toml` and this PRD.
- **Verbose / quiet flags (`-v`, `-q`).** Console output is fixed in v1.

## 6. Design Considerations

- **Single file.** All of the above lives in `archive.py`. No subpackages, no helper modules in v1. If `archive.py` exceeds ~500 lines by the time PRD 7 ships, that's fine — keep it together until there's a concrete reason to split.
- **Stdlib only.** Use `tomllib` (config), `argparse` (CLI), `logging` (logs), `subprocess` + `threading` (yt-dlp wiring), `pathlib` (filesystem), `re` (slug validation). Do not add Python dependencies.
- **`config.example.toml`** should be a minimal-but-complete file: a `[defaults]` table with the values from the plan (`format = "bv*+ba/b"`, `format_sort = ["res:1080", "codec:av01", "acodec:opus"]`, `merge_output_format = "mkv"`, `min_upload_age_hours = 48`) and **one** `[[creator]]` entry with placeholder values and inline comments explaining each field.
- **Console output style.** Match Unix tradition: nothing on success unless there's something to say. `==> creator-slug` per creator section is the only required line. Errors print to stderr.

## 7. Technical Considerations

- Python 3.14 is the target. `tomllib` is available; no `tomli` fallback needed.
- `uv 0.11.16` and `yt-dlp 2026.03.17` are installed system-wide on the dev machine. `archive.py` invokes yt-dlp as a subprocess; it does **not** import `yt_dlp` as a library (matches the plan's "CLI is more stable" decision).
- `ffmpeg` install path: `brew install ffmpeg` on macOS. The error message in 4.8 should mention this.
- The `logging` module's default `FileHandler` is append-mode and stdlib-safe for concurrent appends *within the same process* — that's all we need (no multi-process concurrency in v1).
- Slug regex `^[a-zA-Z0-9][a-zA-Z0-9_-]*$` is deliberately stricter than "filesystem-safe" — it disallows leading hyphens (which would break CLI parsing if a slug is ever passed as an argument) and dots/spaces (which complicate shell quoting).
- The error-isolation helper (4.9) should catch `Exception`, not `BaseException`, so `KeyboardInterrupt` and `SystemExit` still abort the whole run as expected.

## 8. Success Metrics

This PRD is done when **all** of the following are true:

1. `uv run archive.py` with no `config.toml` present prints the expected error and exits with code `2`.
2. `uv run archive.py` with a valid `config.example.toml` copied to `config.toml` runs to completion in under 1 second, prints the `==> <slug>` header(s), and creates the expected directory tree and empty log files under `data/<slug>/`.
3. `uv run archive.py --help` lists all seven flags with descriptions.
4. `uv run archive.py --creator nonexistent` exits with code `2` and the documented error message.
5. Hand-mutating `config.toml` to violate each of the validation rules in 4.4 produces a one-line error message pointing at the right field, and an exit code of `2`. (Spot-check at least: missing `[defaults].format`, duplicate slugs, invalid slug character, missing `channel_url`, wrong type on `min_upload_age_hours`.)
6. Temporarily renaming `ffmpeg` on `PATH` produces the expected error and exits with code `2`.
7. A second invocation with the same config is a no-op on disk (directories already exist; no log lines written by PRD 1 itself).
8. The `get_creator_loggers` helper (or equivalent) can be called from a scratch script and writes correctly-formatted lines to all three log files.

These map onto **verification item #1** (ffmpeg check) and the wiring half of **verification item #15** (failure isolation) from the plan. The behavioral half of #15 is exercised once PRD 2 lands.

## 9. Open Questions

1. **Should `config.example.toml` include one or two `[[creator]]` entries?** One is simpler; two demonstrates the per-creator override pattern. *Tentative answer:* one, with a commented-out second block showing overrides.
2. **Logger name format for the stdlib `logging` module** — `archive.<slug>.download` vs. `<slug>.download` vs. anonymous loggers. *Tentative answer:* `archive.<slug>.<kind>` for grep-ability; not user-visible so low stakes.
3. **Should the `==> creator-slug` console header be suppressed under `--dry-run`?** Will revisit when PRD 7 lands.
4. **Do we need a top-level `archive.log` (cross-creator) in addition to the per-creator logs?** Not in scope here, but if later PRDs want one, it can be added trivially to the logger helper. Out of scope for v1 of this PRD.
