#!/usr/bin/env python3
"""Build a single index.json that describes every archived video.

Walks `data/<slug>/` looking for:
  - `creator.json` (channel-level snapshot)
  - `manifests/channel/playlists_index_latest.json` (current playlist list)
  - `videos/<id>/metadata.json` (per-video normalized metadata)
  - `videos/<id>/<id>.info.json` (raw yt-dlp metadata)

Output: `index.json` at the repo root, consumed by `index.html`.

This file is a thin shim that delegates to `youtube_archive.web_ui`.
Run from the repo root:

    uv run build_index.py
    python3 build_index.py

Or use `uv run archive.py --serve` to build and serve the UI in one step.
"""

from __future__ import annotations

import sys

from youtube_archive.web_ui import cli_build_main


if __name__ == "__main__":
    sys.exit(cli_build_main())
