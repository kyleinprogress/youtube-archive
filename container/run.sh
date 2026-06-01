#!/bin/sh
# Daily archive sync: invoked by supercronic on the configured schedule.
# Output goes to stdout/stderr (captured by `docker logs`); per-creator file
# logs continue to land under /data/<slug>/logs/.
set -e

cd /app

echo "=== $(date '+%Y-%m-%dT%H:%M:%SZ') Starting archive sync ==="
python archive.py
echo "=== $(date '+%Y-%m-%dT%H:%M:%SZ') Sync complete ==="
