FROM python:3.14-slim

# supercronic — drop-in cron for containers, no init system needed.
# Pin to a specific release for reproducibility.
ARG SUPERCRONIC_VERSION=0.2.44
ARG SUPERCRONIC_SHA1=6eb0a8e1e6673675dc67668c1a9b6409f79c37bc

# yt-dlp's JS challenge solver prefers deno. Pin to a known good version.
ARG DENO_VERSION=2.8.1

ARG TARGETARCH=amd64

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        gosu \
        unzip \
    && curl -fsSL \
        "https://github.com/aptible/supercronic/releases/download/v${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH}" \
        -o /usr/local/bin/supercronic \
    && echo "${SUPERCRONIC_SHA1}  /usr/local/bin/supercronic" | sha1sum -c - \
    && chmod +x /usr/local/bin/supercronic \
    && DENO_ARCH=$(case "${TARGETARCH}" in amd64) echo x86_64-unknown-linux-gnu;; arm64) echo aarch64-unknown-linux-gnu;; esac) \
    && curl -fsSL \
        "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-${DENO_ARCH}.zip" \
        -o /tmp/deno.zip \
    && unzip /tmp/deno.zip -d /usr/local/bin/ \
    && chmod +x /usr/local/bin/deno \
    && rm /tmp/deno.zip \
    && apt-get purge -y --auto-remove unzip \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# yt-dlp via pip so we get a recent stable; rebuilds pick up new releases.
RUN pip install --no-cache-dir --upgrade pip yt-dlp

WORKDIR /app

# App source. Stdlib-only — no requirements to install.
COPY archive.py build_index.py ./
COPY youtube_archive/ ./youtube_archive/
COPY index.html ./

# Container-side glue.
COPY container/entrypoint.sh container/run.sh /app/
COPY container/crontab /app/crontab
RUN chmod +x /app/entrypoint.sh /app/run.sh

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

# /data — persistent archive root (bind-mounted to a TrueNAS dataset)
# /staging — local scratch (tmpfs); see staging_dir in config.toml
VOLUME ["/data"]

EXPOSE 8765

ENTRYPOINT ["/app/entrypoint.sh"]
