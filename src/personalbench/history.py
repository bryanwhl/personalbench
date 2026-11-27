"""Freeze a history window and prepare its independent goals with resumable evidence."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

from .imports import _text, _user_text, build_benchmark, import_session
from .privacy import private_directory, read_json, write_json, write_private
from .runners import RunnerError, check_cancellation, execution_cancellation
from .schema import canonical_hash, validate_benchmark
from .session_io import read_session_bytes

PREPARATION_VERSION = "frozen-history-v3"
_DYNAMIC = re.compile(
    r"\b(today|tonight|current|latest|recent|calendar|email|inbox|memory usage)\b", re.IGNORECASE
)


_SUCCESS_CONTRACT = "mandatory-outcome-v1"
_VALIDITY_GATE = {
    "id": "personalbench-validity-v1",
    "description": (
        "Binary mandatory-outcome validity gate. Return score 1 only when the submitted "
        "deliverable substantively fulfills every mandatory outcome and explicit constraint "
        "already stated in the task brief and preserved inputs. Return score 0 for a missing "
        "core deliverable or requested core component, a material factual error or fabrication "
        "that would change the user's understanding or action, or an unsupported claim that "
        "a required action or artifact was completed. For source-backed tasks, do not pass "
        "material claims contradicted by or fabricated beyond the supplied evidence. A refusal, "
        "promise, or description of future work is not completion when the task requires the "
        "deliverable itself. Do not fail this gate for minor wording, presentation, stylistic "
        "weaknesses, or nonessential refinements; assess those through the other quality "
        "criteria. Honor explicit conditions in the brief: honest disclosure of unavailable "
        "optional context satisfies such a condition. Do not invent new requirements or "
        "demand a particular historical answer. Use only binary 0 or 1; use score:null solely "
        "when essential verification evidence is absent and validity cannot be assessed."
    ),
    "kind": "judge",
    "value": None,
    "target": "output",
    "weight": 1,
    "critical": True,
}


def _apply_validity_gate(task: dict) -> None:
    """Add a hard core-outcome check without turning every quality refinement into a gate."""
    criteria = task["criteria"]
    expected = {key: value for key, value in _VALIDITY_GATE.items() if key != "id"}
    matching = next(
        (
            item
            for item in criteria
            if {key: value for key, value in item.items() if key != "id"} == expected
        ),
        None,
    )
    if matching is None:
        criterion = copy.deepcopy(_VALIDITY_GATE)
        existing = {item["id"] for item in criteria}
        suffix = 1
        while criterion["id"] in existing:
            suffix += 1
            criterion["id"] = f"{_VALIDITY_GATE['id']}-{suffix}"
        criteria.append(criterion)
    else:
        criterion = matching
    task.setdefault("provenance", {})["success_contract"] = {
        "version": _SUCCESS_CONTRACT,
        "criterion_id": criterion["id"],
        "policy": "Mandatory outcome validity must pass in addition to the quality threshold; minor style issues are not hard failures.",
    }


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


def _stamp(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_as_of(value)
    except ValueError:
        return None


def _records(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8")
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict) and isinstance(decoded.get("messages"), list):
        if not all(isinstance(row, dict) for row in decoded["messages"]):
            raise ValueError("Expected message objects in normalized session")
        return decoded["messages"]
    rows = []
    for number, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON at session line {number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"Expected an object at session line {number}")  # noqa: TRY004
        rows.append(row)
    return rows


def _normalize(raw: bytes) -> dict:
    # Import exactly the bytes observed, never reread a potentially active source file.
    with tempfile.TemporaryDirectory(prefix="personalbench-history-") as directory:
        path = Path(directory) / "snapshot.jsonl"
        path.write_bytes(raw)
        session = import_session(path)
    metadata = _metadata(raw)
    messages, counts = [], Counter()
    first_user = True
    for message in session["messages"]:
        if message["role"] != "user":
            messages.append(message)
            continue
        text = message["text"]
        cleaned, delegated = _CONTROL_PLANE.subn("", text)
        if delegated:
            counts["control_plane"] += delegated
            cleaned = _user_text(cleaned).strip()
            if not cleaned:
                continue
            message = dict(message, text=cleaned)
        automated = _automation_request(message["text"]) or (
            first_user and metadata.get("automation_origin")
        )
        first_user = False
        origin = "automation" if automated else "human"
        counts[origin] += 1
        if automated:
            message = dict(message, request_origin="automation")
        messages.append(message)
    session["messages"] = messages
    session["request_origin_counts"] = dict(counts)
    return session


_CONTROL_PLANE = re.compile(r"\A\s*<(codex_delegation|heartbeat)(?:\s[^>]*)?>[\s\S]*?</\1>\s*")


def _automation_request(text: str) -> bool:
    return bool(re.match(r"\A\s*(?:Automation:[^\n]*\n)?Automation ID:\s*[^\s]+", text))


def _freeze(raw: bytes, as_of: datetime) -> bytes:
    text = raw.decode("utf-8")
    try:
        normalized = json.loads(text)
    except json.JSONDecodeError:
        normalized = None
    if isinstance(normalized, dict) and isinstance(normalized.get("messages"), list):
        normalized["messages"] = [
            message
            for message in normalized["messages"]
            if (stamp := _stamp(message.get("timestamp"))) is not None and stamp <= as_of
        ]
        # Aggregate telemetry may extend beyond the frozen cutoff.
        for key in ("usage", "duration_seconds", "cost_usd"):
            normalized.pop(key, None)
        return json.dumps(normalized, ensure_ascii=False).encode()
    rows = []
    for row in _records(raw):
        stamp = _stamp(row.get("timestamp"))
        if (
            stamp is not None
            and stamp <= as_of
            or row.get("type") == "session_meta"
            and stamp is None
        ):
            rows.append(row)
    return "\n".join(json.dumps(row, ensure_ascii=False) for row in rows).encode()


def _metadata(raw: bytes) -> dict:
    for row in _records(raw):
        if row.get("type") == "session_meta":
            payload = row.get("payload", {})
            source = payload.get("source")
            spawn = (
                source.get("subagent", {}).get("thread_spawn", {})
                if isinstance(source, dict)
                else {}
            )
            return {
                "session_id": payload.get("id"),
                "cwd": payload.get("cwd"),
                "parent_session_id": spawn.get("parent_thread_id"),
                "forked_from_id": payload.get("forked_from_id"),
                "internal_subagent": isinstance(source, dict) and "subagent" in source,
                "automation_origin": bool(
                    payload.get("automation_id")
                    or source == "automation"
                    or isinstance(source, dict)
                    and source.get("automation")
                ),
            }
    return {}


def _write_immutable(path: Path, raw: bytes):
    private_directory(path.parent)
    if path.is_symlink():
        raise ValueError("Frozen artifacts cannot follow symbolic links")
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("Frozen session artifact changed unexpectedly")
        return
    write_private(path, raw.decode("utf-8"))


def discover_sessions(
    *,
    days: int = 120,
    as_of: datetime | None = None,
    source: str = "all",
    roots: list[Path] | None = None,
    exclude: list[Path] | None = None,
    snapshot_dir: Path | None = None,
    exclude_session_ids: list[str] | None = None,
    include_automations: bool = False,
) -> dict:
    """Select real user activity in the window, retaining earlier clarification context.

    Later activity never discards an otherwise eligible chat. Compiler input and tool
    evidence are frozen through the cutoff, and internal agent threads are accounted
    for as exclusions rather than independent human requests.
    """
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise ValueError("--days must be a positive integer")
    if not isinstance(include_automations, bool):
        raise TypeError("include_automations must be a boolean")
    as_of = as_of or datetime.now(UTC)
    if as_of.tzinfo is None:
        raise ValueError("as_of must include a timezone")
    as_of = as_of.astimezone(UTC)
    start = as_of - timedelta(days=days)
    roots = session_roots(source) if roots is None else roots
    excluded = {Path(path).expanduser().resolve() for path in (exclude or [])}
    ids = set(exclude_session_ids or [])
    if os.environ.get("CODEX_THREAD_ID"):
        ids.add(os.environ["CODEX_THREAD_ID"])
    paths = sorted(
        {
            path.resolve()
            for root in roots
            for pattern in ("*.jsonl", "*.jsonl.zst")
            for path in Path(root).expanduser().rglob(pattern)
        }
    )
    entries = []
    for path in paths:
        entry = {"path": str(path)}
        try:
            entry.update(
                bytes=path.stat().st_size,
                compression="zstd" if path.name.endswith(".jsonl.zst") else "none",
            )
            if path in excluded:
                entry.update(status="excluded", reason="explicit exclusion")
                entries.append(entry)
                continue
            raw = read_session_bytes(path)
            entry.update(_metadata(raw))
            entry["original_source_digest"] = hashlib.sha256(raw).hexdigest()
            if entry.get("session_id") in ids or entry.get("parent_session_id") in ids:
                entry.update(status="excluded", reason="active evaluation session or descendant")
            elif entry.get("internal_subagent"):
                entry.update(
                    status="excluded", reason="internal subagent; parent is the human goal source"
                )
            elif (rows := _records(raw)) and all(
                row.get("type") in {"summary", "file-history-snapshot", "queue-operation"}
                for row in rows
            ):
                entry.update(status="excluded", reason="metadata-only export; no user requests")
            else:
                try:
                    session = _normalize(raw)
                except ValueError as exc:
                    if "no user requests" not in str(exc):
                        raise
                    entry.update(status="excluded", reason="no user requests recorded")
                    entries.append(entry)
                    continue
                entry["request_origin_counts"] = session["request_origin_counts"]
                users = [message for message in session["messages"] if message["role"] == "user"]
                all_dates = [
                    stamp
                    for message in users
                    if (stamp := _stamp(message.get("timestamp"))) is not None
                ]
                entry["last_activity"] = max(all_dates).isoformat() if all_dates else None
                entry["window_request_origin_counts"] = dict(
                    Counter(
                        message.get("request_origin", "human")
                        for message in users
                        if (stamp := _stamp(message.get("timestamp"))) is not None
                        and start <= stamp <= as_of
                    )
                )
                if not users and session["request_origin_counts"].get("control_plane"):
                    entry.update(
                        status="excluded", reason="control-plane delegation only; no human requests"
                    )
                    entries.append(entry)
                    continue
                if (
                    not include_automations
                    and users
                    and all(message.get("request_origin") == "automation" for message in users)
                ):
                    entry.update(
                        status="excluded",
                        reason="automation-origin requests only; opt in with include_automations",
                    )
                    entries.append(entry)
                    continue
                dates = [
                    stamp
                    for message in users
                    if (include_automations or message.get("request_origin") != "automation")
                    and (stamp := _stamp(message.get("timestamp"))) is not None
                ]
                recent = [stamp for stamp in dates if start <= stamp <= as_of]
                if recent:
                    frozen = _freeze(raw, as_of)
                    entry.update(
                        status="selected",
                        last_window_activity=max(recent).isoformat(),
                        snapshot_digest=hashlib.sha256(frozen).hexdigest(),
                    )
                    if snapshot_dir is not None:
                        snapshot = Path(snapshot_dir) / f"{entry['snapshot_digest']}.jsonl"
                        _write_immutable(snapshot, frozen)
                        entry["snapshot_path"] = str(snapshot.absolute())
                elif not dates:
                    entry.update(status="excluded", reason="no timezone-aware user timestamp")
                elif min(dates) > as_of:
                    entry.update(status="excluded", reason="activity after as-of")
                else:
                    entry.update(status="excluded", reason="outside history window")
        except (OSError, UnicodeError, ValueError) as exc:
            entry.update(status="error", error=f"{type(exc).__name__}: {exc}", error_stage="import")
        entries.append(entry)
    entries.sort(
        key=lambda row: (
            row.get("last_window_activity") or row.get("last_activity") or "",
            row["path"],
        ),
        reverse=True,
    )
    return {
        "days": days,
        "as_of": as_of.isoformat(),
        "since": start.isoformat(),
        "selection": "dated coding-agent human requests; explicit automation-origin requests opt-in; control-plane delegations excluded; earlier context and frozen raw retained",
        "include_automations": include_automations,
        "scope_counts": dict(Counter(entry["status"] for entry in entries)),
        "exclusion_reasons": dict(
            Counter(entry["reason"] for entry in entries if entry.get("status") == "excluded")
        ),
        "source": source,
        "roots": [str(Path(path).expanduser().resolve()) for path in roots],
        "excluded_session_ids": sorted(ids),
        "preparation_version": PREPARATION_VERSION,
        "sessions": entries,
    }


def _frozen_source(entry: dict, inventory: dict, artifact_dir: Path | None) -> tuple[Path, bytes]:
    if entry.get("snapshot_path"):
        path = Path(entry["snapshot_path"])
        raw = read_session_bytes(path)
        if hashlib.sha256(raw).hexdigest() != entry["snapshot_digest"]:
            raise ValueError("Frozen source digest mismatch")
        return path, raw
    original = read_session_bytes(Path(entry["path"]))
    if (
        entry.get("original_source_digest")
        and hashlib.sha256(original).hexdigest() != entry["original_source_digest"]
    ):
        raise ValueError("Source changed after discovery; rediscover or use frozen snapshots")
    raw = _freeze(original, parse_as_of(inventory["as_of"])) if inventory.get("as_of") else original
    digest = hashlib.sha256(raw).hexdigest()
    if artifact_dir is not None:
        path = Path(artifact_dir) / "sources" / f"{digest}.jsonl"
        _write_immutable(path, raw)
        entry.update(snapshot_path=str(path.absolute()), snapshot_digest=digest)
        return path, raw
    # A callback needs an immutable file, not today's source. Its lifetime is supplied
    # by build_history's temporary directory when no durable artifact directory exists.
    raise ValueError("Internal preparation requires a snapshot directory")


def _semantic_aux(value):
    if isinstance(value, dict):
        return {
            key: _semantic_aux(item)
            for key, item in value.items()
            if key
            not in {
                "source_path",
                "source_digest",
                "source_sha256",
                "snapshot_path",
                "path",
                "line",
                "call_line",
                "output_line",
            }
        }
    if isinstance(value, list):
        return [_semantic_aux(item) for item in value]
    return value


def _content(session: dict) -> dict:
    result = {
        "messages": [
            {"role": message["role"], "text": message["text"]} for message in session["messages"]
        ],
        "inputs": session.get("inputs", {}),
    }
    if _DYNAMIC.search(
        "\n".join(message["text"] for message in session["messages"] if message["role"] == "user")
    ):
        result["user_dates"] = [
            message.get("timestamp") for message in session["messages"] if message["role"] == "user"
        ]
    if session.get("evidence"):
        result["evidence"] = _semantic_aux(session["evidence"])
    return result


def _record_tokens(raw: bytes) -> list[str]:
    tokens = []
    for row in _records(raw):
        if row.get("type") in {"session_meta", "turn_context"}:
            continue
        payload = row.get("payload", {})
        if isinstance(payload, dict) and payload.get("type") == "token_count":
            continue
        tokens.append(
            canonical_hash({key: value for key, value in row.items() if key != "timestamp"})
        )
    return tokens


def _fork_chain(entry, by_id):
    chain = []
    identifier = entry.get("session_id")
    current = entry
    while identifier and identifier not in chain:
        chain.append(identifier)
        identifier = current.get("forked_from_id")
        current = by_id.get(identifier, {})
    return chain


def _message_lines(raw, messages):
    """Align normalized Codex messages with frozen record order, never timestamp ties."""
    rows = _records(raw)
    response_messages = any(
        row.get("type") == "response_item"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("type") == "message"
        and row["payload"].get("role") in {"user", "assistant"}
        for row in rows
    )
    lines = []
    for line, row in enumerate(rows, 1):
        payload = row.get("payload", {})
        if not isinstance(payload, dict):
            continue
        role, text = None, ""
        if row.get("type") == "response_item" and payload.get("type") == "message":
            role, text = payload.get("role"), _text(payload.get("content"))
        elif (
            row.get("type") == "event_msg"
            and not response_messages
            and payload.get("type") in {"user_message", "agent_message"}
        ):
            role = "user" if payload["type"] == "user_message" else "assistant"
            text = _text(payload.get("message"))
        if role == "user":
            text = _user_text(text)
            if _CONTROL_PLANE.search(text):
                text = _user_text(_CONTROL_PLANE.sub("", text)).strip()
        if len(lines) < len(messages):
            expected = messages[len(lines)]
            if (
                role == expected["role"]
                and text == expected["text"]
                and row.get("timestamp") == expected.get("timestamp")
            ):
                lines.append(line)
    return lines if len(lines) == len(messages) else []


def _inherited_goal_proof(task, session):
    """Fingerprint exact copied goal context and its underlying observed inputs.

    Native call identities bind copied source observations even when a fork rewrites
    serialized timestamps. Different selected-source subsets do not create a new
    goal when the full available source catalog is demonstrably the same copy.
    """
    indices = task.get("provenance", {}).get("source_message_indices")
    if not indices:
        return None
    interval = session["messages"][min(indices) : max(indices) + 1]
    users = [
        session["messages"][index]
        for index in indices
        if session["messages"][index]["role"] == "user"
    ]
    end_dates = [
        stamp for message in interval if (stamp := _stamp(message.get("timestamp"))) is not None
    ]
    if not users or not end_dates:
        return None
    lines = session.get("_source_message_lines", [])
    end_line = lines[max(indices)] if len(lines) == len(session["messages"]) else None
    observations = []
    for record in session.get("evidence", {}).get("records", []):
        output_line = record.get("output_line")
        if isinstance(output_line, int):
            if end_line is None:
                return None
            if output_line > end_line:
                continue
        stamp = _stamp(record.get("output_timestamp"))
        if stamp is None or stamp > max(end_dates):
            continue
        call = record.get("originating_call_id") or record.get("call_id")
        if not isinstance(call, str) or not call:
            return None  # no proof that timestamps refer to the same inherited read
        observations.append(
            {
                "call_id": call,
                "request_hash": record.get("request_hash"),
                "text_hash": hashlib.sha256(record.get("text", "").encode()).hexdigest(),
                **{
                    key: record.get(key)
                    for key in (
                        "query",
                        "kind",
                        "kinds",
                        "available_kinds",
                        "limits",
                        "mutation_scope",
                        "observation_type",
                        "file_coverage",
                    )
                },
            }
        )
    complete_inputs = {
        key: value
        for key, value in task.get("inputs", {}).items()
        if key not in {"sources/observations.json", "sources/coverage.json"}
    }
    proof = {
        "requests_hash": canonical_hash([message["text"] for message in users]),
        "goal_interval_hash": canonical_hash(
            [{key: message[key] for key in ("role", "text")} for message in interval]
        ),
        "source_observations_hash": canonical_hash(observations),
        "complete_inputs_hash": canonical_hash(complete_inputs),
        "source_observation_count": len(observations),
    }
    # Without shared native reads there is no basis for treating capture-time changes
    # as a fork serialization artifact rather than materially different starting state.
    if not observations:
        proof["source_free_timestamps"] = [message.get("timestamp") for message in interval]
    if _DYNAMIC.search("\n".join(message["text"] for message in users)):
        recovery = task.get("provenance", {}).get("recovery", {})
        proof["local_day"] = recovery.get("target_date")
        proof["timezone"] = recovery.get("timezone")
    return proof


def _compiler_semantics(identity):
    """Recovery/transport changes revalidate cached model decisions, not resubmit them."""
    if not isinstance(identity, dict):
        return identity
    result = dict(identity)
    if isinstance(result.get("compiler"), dict):
        # A compiler's resource timeout is not model conditioning. Completed,
        # validated decisions remain reusable when an unfinished source needs longer.
        result["compiler"] = {
            key: value for key, value in result["compiler"].items() if key != "timeout_seconds"
        }
    if isinstance(result.get("implementation"), dict):
        result["implementation"] = {
            name: digest
            for name, digest in result["implementation"].items()
            if name not in {"history.py", "recovery.py", "session_io.py"}
        }
    return result


def _cache_identity(identity):
    return {**identity, "compiler": _compiler_semantics(identity.get("compiler"))}


def _compiler_response(compiled):
    """Recover only model decisions from legacy derived benchmark checkpoints."""
    tasks = []
    for task in compiled["tasks"]:
        original = {
            key: copy.deepcopy(value)
            for key, value in task.items()
            if key not in {"inputs", "provenance", "historical", "readiness", "reference"}
        }
        provenance = task.get("provenance", {})
        recovery = provenance.get("recovery", {})
        if "source_message_indices" in provenance:
            original["source_message_indices"] = provenance["source_message_indices"]
        original["source_evidence_ids"] = provenance.get("source_evidence_ids", [])
        original["required_evidence_kinds"] = recovery.get(
            "declared_required_kinds", recovery.get("required_kinds", [])
        )
        if "declared_execution_requirements" in recovery:
            original["execution_requirements"] = recovery["declared_execution_requirements"]
        if "declared_missing_inputs" in recovery:
            original["missing_inputs"] = recovery["declared_missing_inputs"]
        tasks.append(original)
    return {"tasks": tasks, "exclusions": copy.deepcopy(compiled.get("preparation_exclusions", []))}


def _compiler_decisions(compiled):
    return {
        "tasks": [
            {
                **{
                    key: task.get(key)
                    for key in (
                        "id",
                        "category",
                        "brief",
                        "criteria",
                        "requires_starting_state",
                    )
                },
                "source_message_indices": task.get("provenance", {}).get("source_message_indices"),
                "source_evidence_ids": task.get("provenance", {}).get("source_evidence_ids", []),
                "execution_requirements": task.get("provenance", {})
                .get("recovery", {})
                .get("declared_execution_requirements", task.get("execution_requirements")),
            }
            for task in compiled["tasks"]
        ],
        "exclusions": compiled.get("preparation_exclusions", []),
    }


def _cache_index(artifact_dir, inventory):
    """Find legacy cache keys without weakening compiler/source identity checks."""
    result = {}
    for path in sorted((artifact_dir / "cache").glob("*.json")):
        try:
            cached = read_json(path)
            identity = _cache_identity(cached["identity"])
            result.setdefault(canonical_hash(identity), []).append((path, cached))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            inventory.setdefault("cache_warnings", []).append(
                {"path": str(path), "error": type(exc).__name__}
            )
    return result


def build_history(
    inventory: dict,
    *,
    name: str = "Personal benchmark — session history",
    compiler: Callable[[str], str] | None = None,
    progress: Callable[[dict], None] | None = None,
    artifact_dir: Path | None = None,
    resume: bool = False,
    compiler_identity=None,
    workers: int = 1,
    prepare_session: Callable[[dict, Path], dict] | None = None,
) -> dict:
    """Compile every eligible frozen source with checked, content-addressed checkpoints."""
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 8:
        raise ValueError("preparation workers must be between 1 and 8")
    if resume and compiler is not None and compiler_identity is None:
        raise ValueError("Resuming model preparation requires an explicit compiler identity")
    with tempfile.TemporaryDirectory(prefix="personalbench-build-") as temporary:
        work = Path(artifact_dir) if artifact_dir is not None else Path(temporary)
        return _build_history(
            inventory,
            name=name,
            compiler=compiler,
            progress=progress,
            artifact_dir=work,
            resume=resume,
            compiler_identity=compiler_identity,
            workers=workers,
            prepare_session=prepare_session,
        )


def _refresh_scope_summary(entry: dict, session: dict, inventory: dict) -> None:
    """Recompute audit-only request counts from current normalization of frozen bytes."""
    entry["request_origin_counts"] = dict(session.get("request_origin_counts", {}))
    users = [message for message in session["messages"] if message["role"] == "user"]
    dated = [
        (message, stamp)
        for message in users
        if (stamp := _stamp(message.get("timestamp"))) is not None
    ]
    entry["last_activity"] = max((stamp for _, stamp in dated), default=None)
    if entry["last_activity"] is not None:
        entry["last_activity"] = entry["last_activity"].isoformat()
    since, as_of = _stamp(inventory.get("since")), _stamp(inventory.get("as_of"))
    if since is None or as_of is None:
        return
    window = [(message, stamp) for message, stamp in dated if since <= stamp <= as_of]
    entry["window_request_origin_counts"] = dict(
        Counter(message.get("request_origin", "human") for message, _ in window)
    )
    eligible = [
        stamp
        for message, stamp in window
        if inventory.get("include_automations", False)
        or message.get("request_origin") != "automation"
    ]
    if eligible:
        entry["last_window_activity"] = max(eligible).isoformat()
    else:
        entry.pop("last_window_activity", None)


def _build_history(
    inventory,
    *,
    name,
    compiler,
    progress,
    artifact_dir,
    resume,
    compiler_identity,
    workers,
    prepare_session,
):
    tasks, seen_content, seen_tasks = [], {}, {}
    inherited_tasks = {}
    candidates = []
    # Previously completed inventory rows can be restored from immutable cache records.
    for entry in inventory["sessions"]:
        if entry["status"] not in (
            {"selected", "prepared", "compiled", "duplicate", "covered_by", "error"}
            if resume
            else {"selected"}
        ):
            continue
        if entry["status"] == "error" and not entry.get("snapshot_path"):
            continue
        entry["status"] = "selected"
        entry.pop("error", None)
        entry.pop("error_stage", None)
        stage = "integrity"
        try:
            path, raw = _frozen_source(entry, inventory, artifact_dir)
            entry.update(_metadata(raw))
            stage = "import"
            session = _normalize(raw)
            _refresh_scope_summary(entry, session, inventory)
            session["_source_message_lines"] = _message_lines(raw, session["messages"])
            stage = "preparation"
            if prepare_session is not None:
                session = prepare_session(session, path)
            digest = canonical_hash(_content(session))
            entry.update(source_digest=session["source_digest"], content_digest=digest)
            if digest in seen_content:
                entry.update(status="duplicate", duplicate_of=seen_content[digest])
                if progress:
                    progress(entry)
                continue
            seen_content[digest] = entry["path"]
            normalized_path = artifact_dir / "normalized" / f"{canonical_hash(session)}.json"
            _write_immutable(
                normalized_path,
                (
                    json.dumps(session, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
                ).encode(),
            )
            entry["normalized_session"] = str(normalized_path.absolute())
            candidates.append((entry, session, _record_tokens(raw)))
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            entry.update(status="error", error=f"{type(exc).__name__}: {exc}", error_stage=stage)
            if progress:
                progress(entry)
    # Eliminate literal inherited prefixes, including tool evidence, before model calls.
    complete = []
    for item in sorted(candidates, key=lambda item: len(item[2]), reverse=True):
        entry, session, tokens = item
        parent = next(
            (
                other
                for other in complete
                if len(tokens) < len(other[2])
                and tokens == other[2][: len(tokens)]
                and session.get("inputs", {}) == other[1].get("inputs", {})
                and _content(session)["messages"]
                == _content(other[1])["messages"][: len(session["messages"])]
                and (
                    not _DYNAMIC.search(
                        "\n".join(m["text"] for m in session["messages"] if m["role"] == "user")
                    )
                    or [m.get("timestamp") for m in session["messages"] if m["role"] == "user"]
                    == [m.get("timestamp") for m in other[1]["messages"] if m["role"] == "user"][
                        : sum(m["role"] == "user" for m in session["messages"])
                    ]
                )
            ),
            None,
        )
        if parent:
            entry.update(
                status="covered_by",
                duplicate_of=parent[0]["path"],
                reason="Exact inherited source prefix, including tool observations",
            )
            if progress:
                progress(entry)
        else:
            complete.append(item)

    cancelled = Event()
    legacy_cache = _cache_index(artifact_dir, inventory) if resume else {}
    recovery_implementation = hashlib.sha256(
        (Path(__file__).parent / "recovery.py").read_bytes()
    ).hexdigest()

    def compile_one(item):
        with execution_cancellation(cancelled):
            check_cancellation()
            return compile_bound(item)

    def compile_bound(item):
        entry, session, _ = item
        identity = {
            "version": PREPARATION_VERSION,
            "content": entry["content_digest"],
            "compiler": _compiler_semantics(compiler_identity) if compiler else "fallback",
            "name": name,
            "window": {key: inventory.get(key) for key in ("since", "as_of")},
        }
        cache_key = canonical_hash(identity)
        cache_path = artifact_dir / "cache" / f"{cache_key}.json"
        error_stage = "preparation"
        try:
            cached = None
            if resume and cache_path.exists():
                error_stage = "integrity"
                cached = read_json(cache_path)
            elif resume and cache_key in legacy_cache:
                choices = legacy_cache[cache_key]
                previous = entry.get("compiled_benchmark")
                cached = next(
                    (value for _, value in choices if value.get("benchmark_path") == previous),
                    choices[0][1],
                )
                error_stage = "integrity"
            if cached is not None:
                if not isinstance(cached, dict) or not isinstance(cached.get("identity"), dict):
                    raise ValueError("Invalid preparation cache manifest")
                if _cache_identity(cached.get("identity", {})) != identity:
                    raise ValueError("Preparation cache identity mismatch")
                compiled_path = Path(cached["benchmark_path"])
                if compiled_path.is_symlink() or not compiled_path.resolve().is_relative_to(
                    artifact_dir.resolve()
                ):
                    raise ValueError("Preparation cache points outside its artifact directory")
                stored = read_json(compiled_path)
                if canonical_hash(stored) != cached["benchmark_hash"]:
                    raise ValueError("Preparation cache benchmark digest mismatch")
                validation_attempts = cached.get("validation_attempts", 1)
                if compiler is not None:
                    compiler_response = copy.deepcopy(
                        cached.get("compiler_response") or _compiler_response(stored)
                    )
                    compiled = build_benchmark(
                        session,
                        name=name,
                        compiler=lambda _: json.dumps(compiler_response, ensure_ascii=False),
                        strict=True,
                    )
                else:
                    compiler_response = None
                    compiled = build_benchmark(session, name=name)
                if _compiler_decisions(compiled) != _compiler_decisions(stored):
                    raise ValueError(
                        "Cached compiler decisions changed during deterministic recovery"
                    )
            else:
                compiler_response = None
                repair_error = None
                for validation_attempts in (1, 2):

                    def checked_compiler(prompt, repair_error=repair_error):
                        nonlocal compiler_response
                        check_cancellation()
                        if repair_error is not None:
                            prompt += (
                                "\n\nYour previous preparation response failed Personalbench validation. "
                                "Return a corrected complete JSON response meeting the original "
                                "schema and source mappings. Validation error: " + repair_error
                            )
                        response = compiler(prompt)
                        try:
                            compiler_response = json.loads(response)
                        except (ValueError, TypeError):
                            compiler_response = None
                        return response

                    try:
                        compiled = build_benchmark(
                            session,
                            name=name,
                            compiler=checked_compiler if compiler else None,
                            strict=bool(compiler),
                        )
                        break
                    except ValueError as exc:
                        if (
                            not compiler
                            or validation_attempts == 2
                            or isinstance(exc, RunnerError)
                            or re.search(
                                r"credentials|authentication|model access|secret|timeout|budget",
                                str(exc),
                                re.IGNORECASE,
                            )
                        ):
                            raise
                        repair_error = str(exc)
            compiled_path = (
                artifact_dir / "compiled" / f"{cache_key}.{canonical_hash(compiled)}.json"
            )
            _write_immutable(
                compiled_path,
                (
                    json.dumps(compiled, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
                ).encode(),
            )
            write_json(
                cache_path,
                {
                    "identity": identity,
                    "benchmark_path": str(compiled_path.absolute()),
                    "benchmark_hash": canonical_hash(compiled),
                    "validation_attempts": validation_attempts,
                    "compiler_response": compiler_response,
                    "recovery_implementation": recovery_implementation,
                },
            )
            return (
                item,
                compiled,
                None,
                {
                    "preparation_cache_key": cache_key,
                    "compiled_benchmark": str(compiled_path.absolute()),
                    "validation_attempts": validation_attempts,
                    "recovery_implementation": recovery_implementation,
                },
            )
        except (OSError, UnicodeError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            return item, None, f"{type(exc).__name__}: {exc}", {"error_stage": error_stage}

    results, handled = [], set()
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = {}

    def retain(future):
        item, compiled, error, metadata = future.result()
        handled.add(future)
        entry = item[0]
        entry.update(metadata)
        if error:
            entry.update(
                status="error", error=error, error_stage=metadata.get("error_stage", "preparation")
            )
        else:
            entry["status"] = "prepared"
        results.append((item, compiled, error))
        if progress:
            progress(entry)

    try:
        for item in complete:
            futures[pool.submit(compile_one, item)] = item
        for future in as_completed(futures):
            retain(future)
    except BaseException:
        # Cancel native assessment process groups before waiting for worker shutdown.
        cancelled.set()
        for future in futures:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        active = [future for future in futures if not future.cancelled()]
        _, unfinished = wait(active, timeout=5)
        for future in active:
            if future.done() and future not in handled:
                try:
                    retain(future)
                except (KeyboardInterrupt, Exception) as exc:  # noqa: BLE001 -- preserve original failure during cleanup
                    futures[future][0]["cleanup_error"] = type(exc).__name__
        for future, item in futures.items():
            if future not in handled:
                entry = item[0]
                entry["status"] = "selected"
                entry["preparation_interrupted"] = True
                if progress:
                    try:
                        progress(entry)
                    except (KeyboardInterrupt, Exception) as exc:  # noqa: BLE001 -- preserve original failure during cleanup
                        entry["checkpoint_error"] = type(exc).__name__
        pool.shutdown(wait=not unfinished, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)
    # Explicit ancestor definitions precede copied descendants; inventory order is
    # the stable tie-breaker within each depth.
    order = {entry["path"]: index for index, entry in enumerate(inventory["sessions"])}
    by_id = {
        entry["session_id"]: entry for entry in inventory["sessions"] if entry.get("session_id")
    }
    scoring = None
    for (entry, session, _), compiled, error in sorted(
        results,
        key=lambda result: (len(_fork_chain(result[0][0], by_id)), order[result[0][0]["path"]]),
    ):
        if error:
            continue
        scoring = compiled["scoring"]
        entry.update(duplicate_tasks=[], excluded_tasks=[], task_ids=[])
        entry["preparation_exclusions"] = compiled.get("preparation_exclusions", [])
        for task in compiled["tasks"]:
            indices = task["provenance"].get("source_message_indices")
            mapped = (
                [session["messages"][index] for index in indices]
                if indices is not None
                else session["messages"]
                if len(compiled["tasks"]) == 1
                else []
            )
            users = [message for message in mapped if message["role"] == "user"]
            if (
                not inventory.get("include_automations", False)
                and users
                and all(message.get("request_origin") == "automation" for message in users)
            ):
                entry["excluded_tasks"].append(
                    {
                        "source_task_id": task["id"],
                        "reason": "Mapped requests are automation-origin only; human coding-agent scope excludes them",
                    }
                )
                continue
            users = [
                message
                for message in users
                if inventory.get("include_automations", False)
                or message.get("request_origin") != "automation"
            ]
            dates = [
                stamp
                for message in users
                if (stamp := _stamp(message.get("timestamp"))) is not None
            ]
            if (
                dates
                and inventory.get("since")
                and not any(
                    parse_as_of(inventory["since"]) <= stamp <= parse_as_of(inventory["as_of"])
                    for stamp in dates
                )
            ):
                entry["excluded_tasks"].append(
                    {
                        "source_task_id": task["id"],
                        "reason": "No mapped user request or clarification in history window",
                        "last_user_activity": max(dates).isoformat(),
                    }
                )
                continue
            if not dates:
                task["readiness"] = "incomplete"
                task["provenance"].setdefault("warnings", []).append(
                    "Goal user activity cannot be dated within the history window."
                )
            fork_chain = _fork_chain(entry, by_id)
            fork_proof = _inherited_goal_proof(task, session)
            fork_key = canonical_hash(fork_proof) if fork_proof is not None else None
            inherited = next(
                (
                    candidate
                    for candidate in inherited_tasks.get(fork_key, [])
                    if entry.get("session_id") != candidate[1].get("session_id")
                    and set(fork_chain) & set(_fork_chain(candidate[1], by_id))
                ),
                None,
            )
            if inherited is not None:
                original, original_entry = inherited
                alternate = (
                    artifact_dir
                    / "duplicates"
                    / f"{entry['source_digest']}.{canonical_hash(task)}.json"
                )
                detail = {
                    "retained_task_id": original["id"],
                    "source_path": entry["path"],
                    "source_snapshot": entry.get("snapshot_path"),
                    "method": "explicit-fork-exact-goal-copy",
                    "proof": fork_proof,
                    "fork_lineage": fork_chain,
                    "retained_lineage": _fork_chain(original_entry, by_id),
                    "alternate_compiled_definition": task,
                }
                _write_immutable(
                    alternate,
                    (
                        json.dumps(detail, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
                    ).encode(),
                )
                entry["duplicate_tasks"].append(
                    {
                        "source_task_id": task["id"],
                        "duplicate_of": original["id"],
                        "method": detail["method"],
                        "proof": fork_proof,
                        "alternate_definition": str(alternate.absolute()),
                    }
                )
                original["provenance"].setdefault("duplicate_sources", []).append(
                    {
                        "source_path": entry["path"],
                        "source_digest": entry["source_digest"],
                        "source_message_indices": indices,
                        "method": detail["method"],
                        "proof": fork_proof,
                        "alternate_definition": str(alternate.absolute()),
                    }
                )
                continue
            dynamic_dates = (
                [message.get("timestamp") for message in users]
                if _DYNAMIC.search("\n".join(message["text"] for message in users))
                else []
            )
            exact = canonical_hash(
                {
                    **{key: task.get(key) for key in ("brief", "inputs", "criteria", "category")},
                    "dates": dynamic_dates,
                }
            )
            requirements = task.get("requirements")
            if (
                not isinstance(requirements, list)
                or not requirements
                or not all(isinstance(item, str) for item in requirements)
            ):
                requirements = [criterion["description"] for criterion in task["criteria"]]
            mapped_key = (
                canonical_hash(
                    {
                        "requests": [" ".join(message["text"].split()) for message in users],
                        "inputs": task.get("inputs", {}),
                        "requirements": sorted(requirements),
                        "category": task.get("category"),
                        "dates": dynamic_dates,
                    }
                )
                if users
                else None
            )
            original = seen_tasks.get(exact) or (seen_tasks.get(mapped_key) if mapped_key else None)
            if original:
                entry["duplicate_tasks"].append(
                    {"source_task_id": task["id"], "duplicate_of": original["id"]}
                )
                original["provenance"].setdefault("duplicate_sources", []).append(
                    {
                        "source_path": entry["path"],
                        "source_digest": entry["source_digest"],
                        "source_message_indices": indices,
                    }
                )
                continue
            original_id = task["id"]
            task["id"] = f"s-{entry['source_digest'][:16]}-{original_id}"[:80]
            if any(previous["id"] == task["id"] for previous in tasks):
                task["id"] = f"s-{entry['source_digest'][:16]}-{canonical_hash(task)[:20]}"
            task["provenance"].update(
                source_path=entry["path"],
                source_snapshot=entry.get("snapshot_path"),
                history_last_activity=entry.get("last_window_activity"),
                source_task_id=original_id,
            )
            tasks.append(task)
            entry["task_ids"].append(task["id"])
            seen_tasks[exact] = task
            if fork_key is not None:
                inherited_tasks.setdefault(fork_key, []).append((task, entry))
            if mapped_key:
                seen_tasks[mapped_key] = task
        if not entry["task_ids"] and not entry["duplicate_tasks"]:
            entry.update(
                status="excluded",
                reason="No eligible independent goals after date and relevance filtering",
            )
        else:
            entry["status"] = "compiled"
        entry["readiness"] = {
            state: sum(
                task["readiness"] == state for task in tasks if task["id"] in entry["task_ids"]
            )
            for state in ("ready", "incomplete", "unsupported")
        }
        if progress:
            progress(entry)
    if not tasks:
        raise ValueError("No tasks compiled from the selected history; inspect the inventory")
    if compiler:
        # Apply deterministic scoring policy only after cached model decisions and dedupe.
        # Preparation checkpoints remain reusable across success-policy revisions.
        for task in tasks:
            _apply_validity_gate(task)
        scoring = {**scoring, "success_contract": _SUCCESS_CONTRACT}
    return validate_benchmark(
        {
            "schema_version": 1,
            "name": name,
            "tasks": tasks,
            "scoring": scoring,
            "history": inventory,
        }
    )
