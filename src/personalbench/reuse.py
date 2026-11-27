"""Reuse all identical paid evidence across immediate workflow revisions, never best-of."""

from __future__ import annotations

import copy
import fcntl
import os
from collections import defaultdict
from pathlib import Path

from .implementation import migrate_comparison_only_policy
from .privacy import private_directory, read_json, write_json
from .schema import canonical_hash, validate_benchmark
from .scoring import _valid, summarize


def evaluation_signature(task: dict, harness: str) -> str:
    # Include unknown future execution fields conservatively. Native text harnesses
    # do not receive task IDs; command/fixture adapters can branch on those IDs.
    excluded = {"provenance", "historical"}
    if harness in {"codex", "claude"}:
        excluded.add("id")
    return canonical_hash({key: value for key, value in task.items() if key not in excluded})


def reuse_revision_evidence(
    benchmark: dict,
    identity: dict,
    policy: dict,
    sources: dict,
    report_path: Path,
    benchmark_path: Path,
    output: Path,
) -> dict:
    """Validate full previous benchmark and policy before selecting any trial evidence."""
    report_path, benchmark_path = Path(report_path).absolute(), Path(benchmark_path).absolute()
    directory = private_directory(report_path.parent)
    descriptor = os.open(directory / ".run.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Cannot reuse evidence while the previous run is active") from exc
        if report_path.is_symlink():
            raise ValueError("Cannot reuse a symbolic-link report")
        previous = read_json(report_path)
        original = validate_benchmark(read_json(benchmark_path))
        if canonical_hash(original) != previous.get("benchmark_hash"):
            raise ValueError("Evidence reuse needs the exact original full benchmark")
        if previous.get("run_status") not in {"complete", "interrupted"}:
            raise ValueError(
                "Evidence reuse requires a completed or safely interrupted previous run"
            )
        if identity["baseline_type"] != "fresh" or previous.get("baseline_type") != "fresh":
            raise ValueError("Evidence reuse cannot mix recorded and fresh observations")
        for key in ("configuration", "repeats", "runtime"):
            if previous.get(key) != identity[key]:
                raise ValueError(f"Evidence reuse requires identical {key}")
        execution = previous.get("execution", {"workers": 1, "scheduler": "bounded-thread-v1"})
        if execution != identity["execution"]:
            raise ValueError("Evidence reuse requires identical execution settings")
        if canonical_hash(previous.get("scoring_policy", {})) != previous.get("scoring_hash"):
            raise ValueError("Previous evidence has an inconsistent scoring policy")
        source_summary = summarize(dict(original, repeats=previous["repeats"]), previous["trials"])
        if source_summary != previous.get("summary"):
            raise ValueError("Previous evidence summary does not reproduce")
        if previous["scoring_hash"] != identity["scoring_hash"]:
            previous = migrate_comparison_only_policy(
                previous,
                policy,
                sources["scoring.py"],
                directory,
                source_summary,
            )
        if previous.get("scoring_policy") != policy:
            raise ValueError("Evidence reuse requires identical judge and scoring policy")
        report_hash = canonical_hash(previous)
        evidence_dir = private_directory(output / "reuse-evidence")
        snapshots = {}
        for name, value, digest in (
            ("report", previous, report_hash),
            ("benchmark", original, previous["benchmark_hash"]),
        ):
            snapshot = evidence_dir / f"{name}-{digest}.json"
            if snapshot.is_symlink():
                raise ValueError("Reused evidence snapshots cannot follow symbolic links")
            if snapshot.exists():
                if read_json(snapshot) != value:
                    raise ValueError("Reused evidence snapshot changed unexpectedly")
            else:
                write_json(snapshot, value)
            snapshots[name] = str(snapshot)
        harness = identity["configuration"]["harness"]
        by_signature = defaultdict(list)
        for task in original["tasks"]:
            by_signature[evaluation_signature(task, harness)].append(task["id"])
        by_task = defaultdict(list)
        known_ids = {task["id"] for task in original["tasks"]}
        seen = set()
        for row in previous["trials"]:
            key = (row.get("task_id"), row.get("repeat"))
            if key in seen or key[0] not in known_ids:
                raise ValueError("Previous evidence contains ambiguous trial identities")
            seen.add(key)
            by_task[key[0]].append(row)
        reused, judges, history, used = [], [], [], set()
        mapping = {}
        for task in benchmark["tasks"]:
            if task.get("readiness", "ready") != "ready":
                continue
            candidates = by_signature.get(evaluation_signature(task, harness), [])
            if len(candidates) > 1:
                raise ValueError(
                    "Ambiguous duplicate task signatures cannot select reused outcomes"
                )
            if not candidates or candidates[0] in used:
                continue
            old_id = candidates[0]
            used.add(old_id)
            for row in by_task[old_id]:
                grading = row.get("grading", {})
                if (
                    row.get("status") not in {"completed", "budget_exceeded"}
                    or grading.get("status") != "scored"
                    or not _valid(grading.get("quality_pct"), maximum=100)
                    or not isinstance(grading.get("success"), bool)
                ):
                    continue
                repeat = row.get("repeat")
                if (
                    not isinstance(repeat, int)
                    or isinstance(repeat, bool)
                    or not 0 <= repeat < identity["repeats"]
                ):
                    raise ValueError("Previous evidence has invalid repeat identity")
                provenance = {
                    "report": str(report_path),
                    "report_hash": report_hash,
                    "benchmark_hash": previous["benchmark_hash"],
                    "task_id": old_id,
                    "repeat": repeat,
                }
                if row.get("reused_from"):
                    provenance["previous_reuse"] = row["reused_from"]
                reused.append(
                    {**copy.deepcopy(row), "task_id": task["id"], "reused_from": provenance}
                )
                mapping[(old_id, repeat)] = (task["id"], provenance)
        for source, destination in (
            (previous.get("judge_calls", []), judges),
            (previous.get("retry_history", []), history),
        ):
            for row in source:
                match = mapping.get((row.get("task_id"), row.get("repeat")))
                if match:
                    tid, provenance = match
                    destination.append(
                        {**copy.deepcopy(row), "task_id": tid, "reused_from": provenance}
                    )
        return {
            "trials": reused,
            "judge_calls": judges,
            "retry_history": history,
            "reuse": {
                "source_report": str(report_path),
                "source_report_hash": report_hash,
                "source_benchmark": str(benchmark_path),
                "source_benchmark_hash": previous["benchmark_hash"],
                "source_report_snapshot": snapshots["report"],
                "source_benchmark_snapshot": snapshots["benchmark"],
                "reused_trials": len(reused),
                "reused_tasks": len({row["task_id"] for row in reused}),
                "rule": "All matching fully scored trials, including failures; no best-of selection. Reused measurements describe earlier paid attempts, not new model calls.",
            },
        }
    finally:
        os.close(descriptor)
