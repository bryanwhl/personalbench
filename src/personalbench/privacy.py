"""Private artifact storage and an intentionally conservative publication scanner."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_HOME = Path.home() / ".local" / "share" / "personalbench"
SECRET_PATTERNS = {
    "private-key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "provider-token": r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
    "aws-key": r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    "credential-assignment": r"""(?i)(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)\s*[=:]\s*["']?([A-Za-z0-9_+/=-]{16,})""",
}
SENSITIVE_NAMES = {"auth.json", "credentials.json", "id_rsa", "id_ed25519", ".env"}


def findings(text: str) -> list[str]:
    return [kind for kind, pattern in SECRET_PATTERNS.items() if re.search(pattern, text)]


def assert_no_secrets(value: object):
    if findings(json.dumps(value, ensure_ascii=False)):
        raise ValueError("Possible credentials detected. Remove them before model transmission.")


def private_directory(path: Path) -> Path:
    path = Path(path).absolute()
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            # macOS exposes its root-owned temporary directories through these aliases.
            system_alias = (
                sys.platform == "darwin"
                and ancestor in {Path("/var"), Path("/tmp")}
                and ancestor.lstat().st_uid == 0
                and ancestor.resolve() == Path("/private") / ancestor.name
            )
            if not system_alias:
                raise ValueError("Private artifact paths must not contain symlinks")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def write_private(path: Path, text: str):
    path = Path(path).absolute()
    private_directory(path.parent)
    if path.is_symlink():
        raise ValueError("Refusing to overwrite a symlink")
    fd, temporary = tempfile.mkstemp(prefix=".personalbench-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: Path, data: object):
    write_private(path, json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def read_json(path: Path):
    return json.loads(
        Path(path).read_text(encoding="utf-8"),
        parse_constant=lambda x: (_ for _ in ()).throw(ValueError("Nonfinite JSON value")),
    )


def scan_paths(root: Path, *, git=False) -> list[dict]:
    """Return names and finding types, never matched credential values."""
    root = Path(root).absolute()
    if git:
        names = (
            subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True)
            .stdout.decode()
            .split("\0")
        )
        paths = [root / p for p in names if p]
    else:
        paths = [root] if root.is_file() else list(root.rglob("*"))
    issues = []
    for path in paths:
        if not path.is_file() and not path.is_symlink():
            continue
        relative = str(path.relative_to(root)) if root.is_dir() else path.name
        if not git and any(p in {".git", ".venv", "__pycache__"} for p in path.parts):
            continue
        kinds = []
        if path.is_symlink():
            kinds.append("symlink")
        elif path.stat().st_size > 5_000_000:
            kinds.append("oversize-review-required")
        else:
            try:
                text = path.read_text(encoding="utf-8")
                kinds.extend(findings(text))
                if re.search(r"/(?:Users|home)/[^/\s]+/", text):
                    kinds.append("personal-absolute-path")
                if re.search(r'"type"\s*:\s*"session_meta"', text):
                    kinds.append("raw-session-record")
            except UnicodeError:
                kinds.append("binary-review-required")
        if path.name in SENSITIVE_NAMES or path.suffix in {".pem", ".key"}:
            kinds.append("sensitive-filename")
        if kinds:
            issues.append({"path": relative, "findings": sorted(set(kinds))})
    return issues
