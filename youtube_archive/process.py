from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class YtDlpResult:
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace").strip()


def run_yt_dlp_capture(cmd: list[str]) -> YtDlpResult:
    process = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return YtDlpResult(
        returncode=process.returncode,
        stdout=process.stdout,
        stderr=process.stderr,
    )


def run_subprocess_streaming(
    cmd: list[str],
    logger: logging.Logger,
    *,
    level: int = logging.INFO,
    stderr_level: int = logging.WARNING,
) -> int:
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    threads = []
    for stream, stream_level in (
        (process.stdout, level),
        (process.stderr, stderr_level),
    ):
        if stream is None:
            continue
        thread = threading.Thread(
            target=_pump_stream_to_logger,
            args=(stream, logger, stream_level),
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    return_code = process.wait()
    for thread in threads:
        thread.join()
    return return_code


def _pump_stream_to_logger(stream: Any, logger: logging.Logger, level: int) -> None:
    with stream:
        for line in stream:
            message = line.rstrip("\n")
            if message.strip():
                logger.log(level, message)
