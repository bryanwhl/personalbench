"""Discover a bounded history window and compile its sessions independently."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .imports import build_benchmark, import_session
from .privacy import write_json
from .schema import canonical_hash, validate_benchmark
from .session_io import read_session_bytes


def parse_as_of(value: str | None = None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("--as-of must be an ISO timestamp with a timezone") from exc
    if result.tzinfo is None:
        raise ValueError("--as-of must include a timezone")
    return result.astimezone(UTC)


def session_roots(source: str = "all") -> list[Path]:
    if source not in {"all", "codex", "claude"}:
        raise ValueError("Unknown session source")
    roots = []
    if source in {"all", "codex"}:
        home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        roots.extend([home / "sessions", home / "archived_sessions"])
    if source in {"all", "claude"}:
        roots.append(Path.home() / ".claude" / "projects")
    return roots


def _latest_timestamp(path: Path) -> datetime | None:
    """Read recorded activity, including session metadata, without trusting file mtimes."""
    latest = None
    text = read_session_bytes(path).decode("utf-8")
    for line_number, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON at session line {line_number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"Expected an object at session line {line_number}")  # noqa: TRY004
        stamp = row.get("timestamp")
        if not isinstance(stamp, str):
            continue
        try:
            parsed = parse_as_of(stamp)
        except ValueError:
            continue
        latest = max(latest, parsed) if latest else parsed
    return latest


def discover_sessions(
    *,
    days: int = 60,
    as_of: datetime | None = None,
    source: str = "all",
    roots: list[Path] | None = None,
    exclude: list[Path] | None = None,
) -> dict:
    """Inventory every session, selecting those active in the trailing window.

    Complete selected sessions are preserved, including earlier clarification context.
    Missing dates, malformed files, and exclusions remain visible in the inventory.
    """
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise ValueError("--days must be a positive integer")
    as_of = as_of or datetime.now(UTC)
    if as_of.tzinfo is None:
        raise ValueError("as_of must include a timezone")
    as_of = as_of.astimezone(UTC)
    start = as_of - timedelta(days=days)
    roots = session_roots(source) if roots is None else roots
    excluded = {Path(p).expanduser().resolve() for p in (exclude or [])}
    paths = sorted(
        {
            p.resolve()
            for root in roots
            for pattern in ("*.jsonl", "*.jsonl.zst")
            for p in Path(root).expanduser().rglob(pattern)
        }
    )
    entries = []
    for path in paths:
        entry = {"path": str(path)}
        try:
            entry["bytes"] = path.stat().st_size
            entry["compression"] = "zstd" if path.name.endswith(".jsonl.zst") else "none"
            if path in excluded:
                entry.update(status="excluded", reason="explicit exclusion")
            else:
                latest = _latest_timestamp(path)
                entry["last_activity"] = latest.isoformat() if latest else None
                if latest is None:
                    entry.update(status="excluded", reason="no timezone-aware recorded timestamp")
                elif latest > as_of:
                    entry.update(status="excluded", reason="activity after as-of")
                elif latest < start:
                    entry.update(status="excluded", reason="outside history window")
                else:
                    entry["status"] = "selected"
        except (OSError, UnicodeError, ValueError) as exc:
            entry.update(status="error", error=f"{type(exc).__name__}: {exc}")
        entries.append(entry)
    entries.sort(key=lambda row: (row.get("last_activity") or "", row["path"]), reverse=True)
    return {
        "days": days,
        "as_of": as_of.isoformat(),
        "since": start.isoformat(),
        "selection": "latest recorded activity; complete sessions retain earlier context",
        "source": source,
        "roots": [str(Path(p).expanduser().resolve()) for p in roots],
        "sessions": entries,
    }


def build_history(
    inventory: dict,
    *,
    name: str = "Personal benchmark — past 60 days",
    compiler: Callable[[str], str] | None = None,
    progress: Callable[[dict], None] | None = None,
    artifact_dir: Path | None = None,
) -> dict:
    """Distill all selected sessions without hiding preparation failures or missing inputs."""
    tasks, seen_raw, seen_content, seen_tasks = [], {}, {}, {}
    scoring = None
    for entry in inventory["sessions"]:
        if entry["status"] != "selected":
            continue
        try:
            session = import_session(Path(entry["path"]))
            digest = session["source_digest"]
            content_digest = canonical_hash(
                {
                    "messages": [
                        {"role": m["role"], "text": m["text"]} for m in session["messages"]
                    ],
                    "inputs": session.get("inputs", {}),
                }
            )
            entry.update(source_digest=digest, content_digest=content_digest)
            duplicate = seen_raw.get(digest) or seen_content.get(content_digest)
            if duplicate:
                entry.update(status="duplicate", duplicate_of=duplicate)
                continue
            if artifact_dir is not None:
                normalized_path = artifact_dir / f"{digest}.session.json"
                write_json(normalized_path, session)
                entry["normalized_session"] = str(normalized_path)
            benchmark = build_benchmark(session, name=name, compiler=compiler)
            if artifact_dir is not None:
                compiled_path = artifact_dir / f"{digest}.benchmark.json"
                write_json(compiled_path, benchmark)
                entry["compiled_benchmark"] = str(compiled_path)
            entry["duplicate_tasks"] = []
            entry["excluded_tasks"] = []
            task_ids = []
            scoring = benchmark["scoring"]
            for task in benchmark["tasks"]:
                indices = task["provenance"].get("source_message_indices")
                mapped = (
                    [session["messages"][index] for index in indices]
                    if indices is not None
                    else session["messages"]
                    if len(benchmark["tasks"]) == 1
                    else []
                )
                user_dates = []
                for message in mapped:
                    if message["role"] == "user" and message.get("timestamp"):
                        try:
                            user_dates.append(parse_as_of(message["timestamp"]))
                        except ValueError:
                            pass
                if (
                    user_dates
                    and inventory.get("since")
                    and not any(
                        parse_as_of(inventory["since"]) <= stamp <= parse_as_of(inventory["as_of"])
                        for stamp in user_dates
                    )
                ):
                    entry["excluded_tasks"].append(
                        {
                            "source_task_id": task["id"],
                            "reason": "No mapped user request or clarification in history window",
                            "last_user_activity": max(user_dates).isoformat(),
                        }
                    )
                    continue
                if not user_dates:
                    task["readiness"] = "incomplete"
                    task["provenance"]["warnings"].append(
                        "Goal user activity cannot be dated within the history window."
                    )
                task_digest = canonical_hash(
                    {key: task.get(key) for key in ("brief", "inputs", "criteria", "category")}
                )
                if task_digest in seen_tasks:
                    original = seen_tasks[task_digest]
                    entry["duplicate_tasks"].append(
                        {"source_task_id": task["id"], "duplicate_of": original["id"]}
                    )
                    original["provenance"].setdefault("duplicate_sources", []).append(
                        {
                            "source_path": entry["path"],
                            "source_digest": digest,
                            "source_message_indices": task["provenance"].get(
                                "source_message_indices"
                            ),
                        }
                    )
                    continue
                task["id"] = f"s-{digest[:16]}-{task['id']}"[:80]
                # Long compiler identifiers may share a prefix; include a stable suffix.
                if any(previous["id"] == task["id"] for previous in tasks):
                    task["id"] = f"s-{digest[:16]}-{canonical_hash(task)[:20]}"
                task["provenance"]["source_path"] = entry["path"]
                task["provenance"]["history_last_activity"] = entry.get("last_activity")
                tasks.append(task)
                task_ids.append(task["id"])
                seen_tasks[task_digest] = task
            entry.update(
                status="compiled",
                task_ids=task_ids,
                readiness={
                    state: sum(t["readiness"] == state for t in benchmark["tasks"])
                    for state in ("ready", "incomplete", "unsupported")
                },
            )
            seen_raw[digest] = entry["path"]
            seen_content[content_digest] = entry["path"]
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            entry.update(status="error", error=f"{type(exc).__name__}: {exc}")
        finally:
            if progress:
                progress(entry)
    if not tasks:
        raise ValueError("No tasks compiled from the selected history; inspect the inventory")
    return validate_benchmark(
        {
            "schema_version": 1,
            "name": name,
            "tasks": tasks,
            "scoring": scoring,
            "history": inventory,
        }
    )
