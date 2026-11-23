"""Private snapshots and bounded polling for explicitly selected session files."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from personalbench.imports import import_session
from personalbench.privacy import findings, private_directory, write_json
from personalbench.schema import safe_relative

EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".ssh",
    ".aws",
    ".config",
    ".personalbench",
    ".codex",
    ".claude",
}
EXCLUDED_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "id_rsa",
    "id_ed25519",
}
SECRET = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|gh[pousr]_[A-Za-z0-9]{20,})\b"
)
MAX_SNAPSHOT_BYTES = 20_000_000


def _write(path: Path, data: dict) -> None:
    write_json(path, data)


def _safe_relative(name: str) -> Path:
    path = Path(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError("Unsafe snapshot path")
    safe_relative(name)
    return path


def capture_start(source: Path | None, output: Path) -> dict:
    """Snapshot selected UTF-8 files. Exclusions are recorded, never silent.

    Symlinks and files containing recognizable credentials fail closed. Capture
    is not a security sandbox and secret detection is deliberately conservative.
    """
    output = Path(output)
    if output.is_symlink():
        raise ValueError("Capture destination cannot be a symlink")
    if source is not None:
        source = Path(source)
        if any(p.is_symlink() for p in (source.absolute(), *source.absolute().parents)):
            raise ValueError("Capture source cannot contain a symlink")
        source = source.resolve(strict=True)
        if source.is_dir() and output.resolve().is_relative_to(source):
            raise ValueError("Capture destination must be outside the selected source")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Capture destination must be empty")
    private_directory(output)
    files, excluded, total = {}, [], 0
    if source is not None:
        candidates = []
        if source.is_file():
            candidates.append((source, Path(source.name)))
        elif source.is_dir():
            for directory, dirs, names in os.walk(source, followlinks=False):
                base = Path(directory)
                for name in list(dirs):
                    candidate = base / name
                    if candidate.is_symlink():
                        raise ValueError(
                            f"Symlink excluded from capture: {candidate.relative_to(source)}"
                        )
                    if name in EXCLUDED_DIRS:
                        excluded.append(
                            {
                                "path": str(candidate.relative_to(source)),
                                "reason": "excluded_directory",
                            }
                        )
                        dirs.remove(name)
                for name in sorted(names):
                    candidates.append((base / name, (base / name).relative_to(source)))
        else:
            raise ValueError("Capture source must be a regular file or directory")
        for candidate, relative in candidates:
            if candidate.is_symlink() or not candidate.is_file():
                raise ValueError(f"Nonregular file excluded from capture: {relative}")
            if (
                candidate.name.lower() in EXCLUDED_NAMES
                or candidate.name.lower().startswith(".env.")
                or candidate.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".keystore"}
            ):
                excluded.append({"path": str(relative), "reason": "potential_secret_filename"})
                continue
            size = candidate.stat().st_size
            if total + size > MAX_SNAPSHOT_BYTES:
                raise ValueError("Snapshot exceeds the 20 MB limit; select fewer files")
            raw = candidate.read_bytes()
            total += len(raw)
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                excluded.append({"path": str(relative), "reason": "binary_file"})
                continue
            if SECRET.search(content) or findings(content):
                raise ValueError(
                    f"Potential credential in selected file: {relative}; remove it before capture"
                )
            files[str(relative)] = content
    snapshot = output / "start-state"
    snapshot.mkdir(mode=0o700)
    inventory = []
    for name, content in files.items():
        destination = snapshot / _safe_relative(name)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        inventory.append({"path": name, "sha256": hashlib.sha256(content.encode()).hexdigest()})
    manifest = {
        "schema_version": 1,
        "status": "started",
        "created_at": datetime.now(UTC).isoformat(),
        "files": inventory,
        "excluded": excluded,
        "starting_state": "captured" if source is not None else "missing",
        "warnings": [
            "Snapshot captures selected text files only; dependencies and external services require separate review.",
            "Filename exclusions and credential detection cannot guarantee removal of every secret.",
        ],
    }
    _write(output / "capture.json", manifest)
    return manifest


def capture_finish(capture: Path, session_path: Path) -> dict:
    """Attach a completed session to its immutable initial snapshot."""
    capture = Path(capture)
    if (
        any(p.is_symlink() for p in (capture.absolute(), *capture.absolute().parents))
        or (capture / "capture.json").is_symlink()
        or (capture / "start-state").is_symlink()
    ):
        raise ValueError("Capture paths cannot be symlinks")
    manifest = json.loads((capture / "capture.json").read_text())
    inputs = {}
    root = (capture / "start-state").resolve(strict=True)
    for item in manifest.get("files", []):
        relative = _safe_relative(item["path"])
        source = root / relative
        if source.is_symlink() or not source.resolve(strict=True).is_relative_to(root):
            raise ValueError("Snapshot file escapes capture directory")
        for parent in source.relative_to(root).parents:
            if (root / parent).is_symlink():
                raise ValueError("Snapshot directory cannot be a symlink")
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("Snapshot changed after capture start")
        inputs[item["path"]] = raw.decode("utf-8")
    session = import_session(session_path)
    if manifest.get("starting_state") == "captured":
        session["inputs"] = inputs
    session["capture"] = {
        "created_at": manifest["created_at"],
        "excluded": manifest.get("excluded", []),
        "starting_state": manifest.get("starting_state", "missing"),
    }
    session["warnings"] += manifest.get("warnings", [])
    if manifest.get("excluded"):
        session["warnings"].append(
            "Snapshot excluded files; review the exclusion list before running."
        )
    manifest["status"] = "finished"
    manifest["session_digest"] = session["source_digest"]
    _write(capture / "session.json", session)
    _write(capture / "capture.json", manifest)
    return session


def capture_watch(
    capture: Path, session_path: Path, *, interval_seconds: float = 2, duration_seconds: float = 60
) -> Iterator[dict]:
    """Poll one selected export for a bounded duration; never scan other sessions.

    A yielded update is an imported snapshot, not proof the author finished work.
    Partial final JSONL records are retried on later polls.
    """
    if not 0.1 <= interval_seconds <= 60 or not 0 <= duration_seconds <= 3600:
        raise ValueError("Watch interval must be 0.1–60 seconds and duration 0–3600 seconds")
    deadline, previous = time.monotonic() + duration_seconds, None
    while True:
        try:
            session = capture_finish(capture, session_path)
        except (FileNotFoundError, ValueError) as exc:
            if time.monotonic() >= deadline:
                raise ValueError(f"Watch ended without a complete import: {exc}") from exc
        else:
            if session["source_digest"] != previous:
                previous = session["source_digest"]
                yield session
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(interval_seconds, remaining))
