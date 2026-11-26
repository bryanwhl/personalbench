"""Synthetic session archives exercise bounded decoding and history coverage."""

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime

import pytest

from personalbench.history import build_history, discover_sessions
from personalbench.imports import import_session
from personalbench.session_io import read_session_bytes


def archive(tmp_path, raw):
    decoder = shutil.which("zstd")
    if decoder is None:
        pytest.skip("Optional zstd executable is unavailable")
    path = tmp_path / "synthetic.jsonl.zst"
    path.write_bytes(
        subprocess.run(
            [decoder, "--quiet", "--stdout"], input=raw, capture_output=True, check=True
        ).stdout
    )
    return path


def test_compressed_import_and_discovery_match_plain_transcript(tmp_path):
    raw = "\n".join(
        json.dumps(row)
        for row in [
            {
                "type": "event_msg",
                "timestamp": "2026-11-25T10:00:00Z",
                "payload": {"type": "user_message", "message": "Explain a stack"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-11-25T10:00:01Z",
                "payload": {"type": "agent_message", "message": "Last in first out"},
            },
        ]
    ).encode()
    compressed = archive(tmp_path, raw)
    plain = tmp_path / "synthetic.jsonl"
    plain.write_bytes(raw)
    assert import_session(compressed) == import_session(plain)
    inventory = discover_sessions(roots=[tmp_path], as_of=datetime(2026, 11, 26, tzinfo=UTC))
    assert len(inventory["sessions"]) == 2
    compressed_row = next(row for row in inventory["sessions"] if row["compression"] == "zstd")
    assert compressed_row["bytes"] == compressed.stat().st_size
    benchmark = build_history(inventory)
    assert len(benchmark["tasks"]) == 1
    assert {row["status"] for row in inventory["sessions"]} == {"compiled", "duplicate"}


def test_normalized_json_and_plain_size_limit(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"messages": [{"role": "user", "text": "Explain a stack"}]}))
    assert import_session(path)["source_format"] == "normalized"
    with pytest.raises(ValueError, match="byte limit"):
        read_session_bytes(path, max_bytes=4)


def test_missing_zstd_is_explicit(tmp_path, monkeypatch):
    path = tmp_path / "session.jsonl.zst"
    path.write_bytes(b"archive")
    monkeypatch.setattr("personalbench.session_io.shutil.which", lambda _: None)
    with pytest.raises(ValueError, match="optional zstd executable"):
        import_session(path)


def test_corrupt_archive_and_invalid_utf8_are_errors(tmp_path):
    if shutil.which("zstd") is None:
        pytest.skip("Optional zstd executable is unavailable")
    path = tmp_path / "bad.jsonl.zst"
    path.write_bytes(b"not a compressed frame")
    with pytest.raises(ValueError, match="invalid data"):
        read_session_bytes(path)
    invalid = archive(tmp_path, b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        import_session(invalid)


def fake_decoder(tmp_path, monkeypatch, body):
    script = tmp_path / "decoder"
    script.write_text(f"#!{sys.executable}\nimport os, time\n{body}\n")
    script.chmod(0o700)
    monkeypatch.setattr("personalbench.session_io.shutil.which", lambda _: str(script))
    path = tmp_path / "session.jsonl.zst"
    path.write_bytes(b"test")
    return path


def test_decoded_output_limit_kills_and_reaps_decoder(tmp_path, monkeypatch):
    pid_file = tmp_path / "decoder.pid"
    path = fake_decoder(
        tmp_path,
        monkeypatch,
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "os.write(1, b'x' * 8192)\ntime.sleep(30)",
    )
    with pytest.raises(ValueError, match="Decompressed session exceeds"):
        read_session_bytes(path, max_bytes=128)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


def test_decoder_timeout_kills_and_reaps_decoder(tmp_path, monkeypatch):
    path = fake_decoder(tmp_path, monkeypatch, "time.sleep(30)")
    launch = subprocess.Popen
    children = []

    def track(*args, **kwargs):
        child = launch(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr("personalbench.session_io.subprocess.Popen", track)
    with pytest.raises(ValueError, match="timeout"):
        read_session_bytes(path, timeout_seconds=0.05)
    assert children[0].poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(children[0].pid, 0)
