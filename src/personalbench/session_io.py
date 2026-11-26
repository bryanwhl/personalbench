"""Bounded readers for ordinary and zstd-compressed local session exports."""

from __future__ import annotations

import os
import selectors
import shutil
import subprocess
import time
from pathlib import Path

MAX_SESSION_BYTES = 64 * 1024 * 1024


def read_session_bytes(
    path: Path, *, max_bytes: int = MAX_SESSION_BYTES, timeout_seconds: float = 30
) -> bytes:
    """Read at most the import limit, including decompressed data.

    zstd is an optional executable dependency for archived ``.jsonl.zst`` files.
    Decoder output stays in a bounded pipe buffer and the child is always reaped.
    """
    path = Path(path)
    if path.stat().st_size > max_bytes:
        raise ValueError("Session exceeds the import byte limit; select a smaller export")
    if not path.name.endswith(".jsonl.zst"):
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError("Session exceeds the import byte limit; select a smaller export")
        return raw
    decoder = shutil.which("zstd")
    if decoder is None:
        raise ValueError("Reading .jsonl.zst sessions requires the optional zstd executable")
    process = subprocess.Popen(
        [decoder, "--decompress", "--stdout", "--quiet", "--", str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    output = bytearray()
    deadline = time.monotonic() + timeout_seconds
    try:
        assert process.stdout is not None
        with process.stdout, selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise ValueError("Compressed session decoding exceeded its timeout")
                chunk = os.read(process.stdout.fileno(), min(65536, max_bytes + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > max_bytes:
                    raise ValueError("Decompressed session exceeds the import byte limit")
            try:
                status = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise ValueError("Compressed session decoding exceeded its timeout") from exc
            if status != 0:
                raise ValueError("Cannot decode compressed session; zstd reported invalid data")
        return bytes(output)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
