## Relevant Files

- `archive.py` - Single-file orchestrator at the repo root; all PRD 1 logic (config loading, validation, CLI, directory setup, logging helpers, ffmpeg check, error-isolation scaffolding) lives here.
- `pyproject.toml` - Project metadata; update `description` and confirm `name = "youtube-archive"`. No runtime dependencies — stdlib only.
- `.gitignore` - Excludes `data/`, `.venv/`, `__pycache__/`, `*.pyc`, and `config.toml`.
- `config.example.toml` - Committed sample config: `[defaults]` table populated with the plan's values plus one `[[creator]]` block with placeholder values and inline comments. A second `[[creator]]` block is included commented-out to demonstrate per-creator overrides.
- `config.toml` - The actual config file. **Not** committed (ignored). Read at runtime from `./config.toml`.
- `data/<slug>/` - Per-creator subtree created on first run: `videos/`, `manifests/playlists/`, `manifests/channel/`, `logs/{download,manifests,errors}.log`. Created by the script; not checked in.

### Notes

- Python 3.14, stdlib only — no new dependencies. Use `tomllib` (config), `argparse` (CLI), `logging` (logs), `subprocess` + `threading` (yt-dlp wiring helper), `pathlib` (filesystem), `re` (slug validation).
- Run the script with `uv run archive.py [flags]`.
- PRD 1 has no automated test suite in scope. Verification is manual against the success metrics in PRD §8 (covered by parent task 6.0 below). If you choose to add throw-away verification scripts, keep them out of the committed tree.
- All seven CLI flags must be parseable now even though only `--creator` has behavior wired up in this PRD; the other flags are accepted and stored for later PRDs to consume.

## Instructions for Completing Tasks

**IMPORTANT:** As you complete each task, you must check it off in this markdown file by changing `- [ ]` to `- [x]`. This helps track progress and ensures you don't skip any steps.

Example:
- `- [ ] 1.1 Read file` → `- [x] 1.1 Read file` (after completing)

Update the file after completing each sub-task, not just after completing an entire parent task.

## Tasks

- [ ] 0.0 Create feature branch
  - [ ] 0.1 Initialize the repo with `git init` (the working directory is not yet a git repository) and make an initial commit of the current state (`pyproject.toml`, `.agents/`) on `main`
  - [ ] 0.2 Create and checkout a new branch for this feature: `git checkout -b feature/project-foundation-and-config`

- [ ] 1.0 Set up project skeleton (files, metadata, sample config)
  - [ ] 1.1 Update `pyproject.toml`: confirm `name = "youtube-archive"`, add a one-line `description` (e.g. `"Single-file Python orchestrator that archives YouTube creators via yt-dlp."`), and leave `dependencies = []`
  - [ ] 1.2 Create `.gitignore` at the repo root excluding at minimum `data/`, `.venv/`, `__pycache__/`, `*.pyc`, and `config.toml`
  - [ ] 1.3 Create `config.example.toml` at the repo root with a `[defaults]` table containing `format = "bv*+ba/b"`, `format_sort = ["res:1080", "codec:av01", "acodec:opus"]`, `merge_output_format = "mkv"`, and `min_upload_age_hours = 48`
  - [ ] 1.4 Add one fully-populated `[[creator]]` entry to `config.example.toml` with placeholder `slug`, `name`, and `channel_url` values plus inline comments explaining each field
  - [ ] 1.5 Append a commented-out second `[[creator]]` block in `config.example.toml` showing the per-creator override pattern (`format`, `format_sort`, `merge_output_format`, `min_upload_age_hours`, `exclude_playlists`, `extra_playlists`)
  - [ ] 1.6 Create `archive.py` at the repo root with a `main()` function and the standard `if __name__ == "__main__": main()` entry point — empty body for now, filled in by later parent tasks

- [ ] 2.0 Implement config loading and validation
  - [ ] 2.1 Implement a loader that reads `./config.toml` (relative to CWD) using `tomllib`; on `FileNotFoundError`, print `error: ./config.toml not found (cwd: <absolute path>)` to stderr and exit 2
  - [ ] 2.2 On `tomllib.TOMLDecodeError`, print the underlying error message verbatim (prefixed with `error: config.toml: `), exit 2
  - [ ] 2.3 Validate `[defaults]`: `format` is a required non-empty string; `merge_output_format` is a required non-empty string; `format_sort` (if present) must be a list of strings; `min_upload_age_hours` (if present) must be a non-negative integer; default it to `48` when absent
  - [ ] 2.4 Validate at least one `[[creator]]` entry exists; each entry has `slug`, `name`, and `channel_url`
  - [ ] 2.5 Validate every creator's `slug` against the regex `^[a-zA-Z0-9][a-zA-Z0-9_-]*$` and ensure slugs are unique (case-sensitive) across creators
  - [ ] 2.6 Validate every creator's `channel_url` starts with `https://www.youtube.com/`
  - [ ] 2.7 Validate per-creator overrides (`format`, `format_sort`, `merge_output_format`, `min_upload_age_hours`) reuse the same type checks as `[defaults]`; validate `exclude_playlists` / `extra_playlists` (if present) are lists of strings
  - [ ] 2.8 Build the resolved config as an in-memory structure (e.g. a list of dicts) where each creator entry contains its merged effective settings, ready for later PRDs to consume
  - [ ] 2.9 Abort on the **first** validation failure with a one-line message of the form `error: config.toml: <field path>: <what's wrong>` (e.g. `error: config.toml: creator[1].slug: must match ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ (got "my creator")`) and exit code 2

- [ ] 3.0 Implement the CLI surface with argparse
  - [ ] 3.1 Build a stdlib `argparse.ArgumentParser` accepting all seven flags: `--creator <slug>`, `--manifests-only`, `--refresh-metadata`, `--upgrade`, `--audit`, `--repair`, `--dry-run`
  - [ ] 3.2 Provide a one-line `help=` description for every flag so `uv run archive.py --help` documents them clearly
  - [ ] 3.3 After config load + validation, if `--creator` was passed, verify the slug exists in the loaded config; otherwise abort with `error: --creator: no creator with slug "<slug>" found in config.toml` and exit 2
  - [ ] 3.4 Compute the list of creators selected for the run (all creators by default, or just the one specified via `--creator`) and store args + selection on a single in-memory context for the rest of `main()` to use
  - [ ] 3.5 Ensure a successful run exits with code 0

- [ ] 4.0 Implement directory creation and logging infrastructure
  - [ ] 4.1 For each selected creator, create `data/<slug>/videos/`, `data/<slug>/manifests/playlists/`, `data/<slug>/manifests/channel/`, and `data/<slug>/logs/` using `Path.mkdir(parents=True, exist_ok=True)` (idempotent; creates `data/` on demand)
  - [ ] 4.2 Touch (create-if-missing, do not truncate) `download.log`, `manifests.log`, and `errors.log` inside each creator's `logs/` directory so they exist as empty files after the first run
  - [ ] 4.3 Confirm the script does **not** create `archive.txt`, `creator.json`, or any files inside `videos/` / `manifests/` — those belong to later PRDs
  - [ ] 4.4 Configure stdlib `logging` so that each creator has three named loggers (e.g. `archive.<slug>.download`, `archive.<slug>.manifests`, `archive.<slug>.errors`), each with a `FileHandler` pointing at the matching log file in append mode
  - [ ] 4.5 Set a formatter that produces lines of the form `<ISO 8601 UTC timestamp> <LEVEL> <message>` (e.g. `2026-05-26T15:30:42Z INFO starting manifest discovery for creator-slug`), using only `INFO`, `WARN`, `ERROR` levels
  - [ ] 4.6 Implement and export a helper `get_creator_loggers(slug) -> (download_log, manifests_log, errors_log)` so later PRDs do not need to know log file paths or configure handlers themselves
  - [ ] 4.7 Implement a `run_subprocess_streaming(cmd, logger, *, level=logging.INFO, stderr_level=logging.WARN)` helper using `subprocess.Popen` + a small thread (or threads) that pump stdout/stderr line-by-line into the given logger; return the process exit code
  - [ ] 4.8 Print exactly one `==> <slug>` header to stdout per selected creator at the start of its processing block; all detailed activity goes to log files only

- [ ] 5.0 Implement ffmpeg startup check and per-creator error-isolation posture
  - [ ] 5.1 After config load + validation but **before** any per-creator work, run `ffmpeg -version` as a subprocess with stdout and stderr suppressed and inspect the exit code
  - [ ] 5.2 On `FileNotFoundError` (ffmpeg not on PATH) or non-zero exit, print `error: ffmpeg not found or not working — install with: brew install ffmpeg` to stderr and exit 2 — this check runs unconditionally, including under `--manifests-only` and `--dry-run`
  - [ ] 5.3 Wrap the per-creator processing block in a `try` / `except Exception` boundary (deliberately not `BaseException`, so `KeyboardInterrupt` and `SystemExit` still abort the whole run): on exception, write a one-line summary plus traceback to that creator's `errors.log`, print a one-line error to the console (stderr), and continue to the next creator
  - [ ] 5.4 Implement a `creator_scope(slug)` context manager (or equivalent helper function) that wraps the boundary above so later PRDs inherit the error-isolation semantics by using the helper rather than re-implementing it
  - [ ] 5.5 Confirm that when all creators process cleanly the script exits with code 0; when one creator fails the others still process and the final exit code is 0 (per-creator failure is **not** a run-level failure in v1)

- [ ] 6.0 Verify the implementation against PRD success metrics
  - [ ] 6.1 With no `config.toml` present, `uv run archive.py` prints `error: ./config.toml not found (cwd: <abs path>)` and exits with code 2
  - [ ] 6.2 Copy `config.example.toml` → `config.toml`, then `uv run archive.py` finishes in under 1 second, prints the `==> <slug>` header(s), and creates the full directory tree plus the three empty log files under `data/<slug>/`
  - [ ] 6.3 `uv run archive.py --help` lists all seven flags (`--creator`, `--manifests-only`, `--refresh-metadata`, `--upgrade`, `--audit`, `--repair`, `--dry-run`) each with a one-line description
  - [ ] 6.4 `uv run archive.py --creator nonexistent` exits 2 with `error: --creator: no creator with slug "nonexistent" found in config.toml`
  - [ ] 6.5 Spot-check validation: hand-mutate `config.toml` to remove `[defaults].format`, introduce duplicate slugs, use an invalid slug character, drop `channel_url`, and set `min_upload_age_hours` to a string — each mutation produces a one-line error pointing at the right field and exit code 2
  - [ ] 6.6 Temporarily move/rename `ffmpeg` on `PATH` (e.g. `PATH=/tmp uv run archive.py`) and confirm the `error: ffmpeg not found ...` message appears and the script exits 2
  - [ ] 6.7 Run `uv run archive.py` a second time against the same config and confirm it is a no-op on disk (directories already exist; PRD 1 itself writes no log lines)
  - [ ] 6.8 Write a throw-away scratch script that imports / calls `get_creator_loggers(slug)` and writes one line at each level to each logger; confirm all three log files contain correctly-formatted `<ISO 8601 UTC timestamp> <LEVEL> <message>` lines
