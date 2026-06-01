#!/bin/sh
# Container startup: optional PUID/PGID remap, then run --serve in the
# background and supercronic in the foreground as PID 1.
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}

if [ "$PUID" != "0" ]; then
    groupadd -o -g "$PGID" appuser 2>/dev/null || true
    useradd -o -u "$PUID" -g "$PGID" -d /app -s /bin/sh appuser 2>/dev/null || true
    # /data is bind-mounted; /staging is tmpfs. Make both writable for the
    # runtime user without disturbing the host-side ownership of /data subtrees
    # that already exist (chown -R could be slow on a large archive).
    chown "$PUID:$PGID" /data 2>/dev/null || true
    [ -d /staging ] && chown "$PUID:$PGID" /staging 2>/dev/null || true
    echo "Running as UID=$PUID GID=$PGID"
    EXEC_CMD="gosu appuser"
else
    echo "Running as root"
    EXEC_CMD=""
fi

cd /app

# Render the schedule from $ARCHIVE_CRON (env-substituted at runtime so a single
# image works for any schedule). Default: daily at 03:00.
SCHEDULE="${ARCHIVE_CRON:-0 3 * * *}"
echo "${SCHEDULE} /app/run.sh" > /app/crontab.rendered
echo "Schedule: ${SCHEDULE}  →  /app/run.sh"

# Web UI in a restart-on-crash background loop. Reads config.toml from /app
# (bind-mounted) for data_dir, then serves index.json + media from /data.
SERVE_HOST="${SERVE_HOST:-0.0.0.0}"
SERVE_PORT="${SERVE_PORT:-8765}"
echo "Starting --serve on ${SERVE_HOST}:${SERVE_PORT}"
(
    while true; do
        $EXEC_CMD python archive.py --serve --host "$SERVE_HOST" --port "$SERVE_PORT" \
            || echo "[$(date '+%Y-%m-%d %H:%M:%S')] --serve exited; restarting in 5s"
        sleep 5
    done
) &

# supercronic as PID 1 — handles signals, logs each invocation to stdout.
exec $EXEC_CMD supercronic /app/crontab.rendered
