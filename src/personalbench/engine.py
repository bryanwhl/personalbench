"""Checkpointed evaluation orchestration; private answers never enter candidate inputs."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import math
import os
import subprocess
import tempfile
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock

from . import __version__
from .grading import grade
from .implementation import implementation_sources, migrate_comparison_only_policy, snapshot_scoring
from .privacy import assert_no_secrets, private_directory, read_json, write_json, write_private
from .report import render_report
from .reuse import reuse_revision_evidence
from .runners import RunnerError, adapter_identity, execution_cancellation, run_candidate
from .schema import canonical_hash, safe_relative, validate_benchmark, validate_config
from .scoring import summarize


def _version(config):
    if config["harness"] in {"fixture", "command"}:
        return {"harness": config["harness"], "version": "adapter-v1"}
    try:
        value = subprocess.run(
            [config["harness"], "--version"], capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        value = "unavailable"
    return {"harness": config["harness"], "version": value, **adapter_identity(config)}


def model_callback(config, *, allow_unsafe_local=False, calls=None, deadline=None):
    """Fresh harness home/workspace per request; compiler and judge cannot see candidate files."""
    config = dict(validate_config(config), tools="none", sandbox="read-only")

    def call(prompt):
        bounded = config
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining < 0.1:
                raise RunnerError("Total evaluation time budget exhausted before assessment")
            bounded = dict(
                config, timeout_seconds=min(config.get("timeout_seconds", 120), remaining)
            )
        with tempfile.TemporaryDirectory(prefix="personalbench-assessor-") as directory:
            result = run_candidate(
                {"id": "assessment", "brief": prompt, "inputs": {}},
                bounded,
                Path(directory),
                allow_unsafe_local=allow_unsafe_local,
            )
        if calls is not None:
            calls.append({key: value for key, value in result.items() if key != "output"})
        if result["status"] != "completed":
            raise RunnerError(result["error"] or "Assessor failed")
        output = result["output"].strip()
        if output.startswith("```") and output.endswith("```"):
            output = output.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return output

    return call


def _workspace(task, directory):
    root = Path(directory)
    for relative, text in task.get("inputs", {}).items():
        path = root / safe_relative(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _evaluate(
    benchmark,
    configuration,
    output: Path,
    *,
    judge_config=None,
    repeats=1,
    baseline_type="fresh",
    resume=False,
    allow_unsafe_local=False,
    max_total_seconds=1800,
    stop_after_cost_usd=None,
    workers=1,
    progress=None,
    reuse_report=None,
    reuse_benchmark=None,
):
    if (reuse_report is None) != (reuse_benchmark is None):
        raise ValueError("Evidence reuse requires both previous report and full benchmark")
    validate_benchmark(benchmark)
    validate_config(configuration)
    if judge_config:
        validate_config(judge_config)
    if not isinstance(repeats, int) or not 1 <= repeats <= 100:
        raise ValueError("repeats must be between 1 and 100")
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 8:
        raise ValueError("workers must be between 1 and 8")
    if baseline_type not in {"fresh", "recorded"}:
        raise ValueError("Invalid baseline type")
    if baseline_type == "recorded" and repeats != 1:
        raise ValueError("A historical observation cannot be repeated")
    if not 0 < max_total_seconds <= 86400:
        raise ValueError("max_total_seconds must be between 0 and 86400")
    if stop_after_cost_usd is not None and stop_after_cost_usd <= 0:
        raise ValueError("stop_after_cost_usd must be positive")
    if (
        configuration["harness"] != "fixture"
        or judge_config
        or any(c["kind"] == "command" for t in benchmark["tasks"] for c in t["criteria"])
    ) and not allow_unsafe_local:
        raise ValueError("Native evaluation requires --allow-unsafe-local; use trusted tasks only")
    assert_no_secrets(configuration)
    if judge_config:
        assert_no_secrets(judge_config)
    # Candidate transmission excludes historical answers and grading criteria.
    if baseline_type == "fresh":
        assert_no_secrets([{k: t.get(k) for k in ("brief", "inputs")} for t in benchmark["tasks"]])
    output = private_directory(output)
    path = output / "report.json"
    sources = implementation_sources()
    policy = {
        "personalbench_version": __version__,
        "grader_implementation": {
            name: hashlib.sha256(source).hexdigest() for name, source in sources.items()
        },
        "policy": "bounded-ratio-v1",
        "scoring": benchmark.get("scoring", {}),
        "judge": judge_config,
        "judge_runtime": _version(judge_config) if judge_config else None,
    }
    identity = {
        "benchmark_hash": canonical_hash(benchmark),
        "scoring_hash": canonical_hash(policy),
        "configuration": configuration,
        "baseline_type": baseline_type,
        "repeats": repeats,
        "runtime": _version(configuration),
        "execution": {"workers": workers, "scheduler": "bounded-thread-v1"},
    }
    if path.exists():
        if not resume:
            raise ValueError(
                "Output already contains a run; use --resume or a new output directory"
            )
        report = read_json(path)
        if reuse_report is not None and (
            report.get("reuse", {}).get("source_report") != str(Path(reuse_report).absolute())
            or report.get("reuse", {}).get("source_benchmark")
            != str(Path(reuse_benchmark).absolute())
        ):
            raise ValueError("Cannot change evidence reuse source when resuming")
        # Reports created before parallel execution used one serial worker.
        report.setdefault("execution", {"workers": 1, "scheduler": "bounded-thread-v1"})
        if any(report.get(k) != v for k, v in identity.items() if k != "scoring_hash"):
            raise ValueError(
                "Cannot resume with changed benchmark, configuration, grader, repeats, or workers"
            )
        if report.get("scoring_hash") != identity["scoring_hash"]:
            report = migrate_comparison_only_policy(
                report,
                policy,
                sources["scoring.py"],
                output,
                summarize(dict(benchmark, repeats=repeats), report["trials"]),
            )
    elif resume:
        raise ValueError("No run exists at this output path")
    else:
        report = {
            "schema_version": 1,
            **identity,
            "created_at": datetime.now(UTC).isoformat(),
            "task_manifest": [
                {
                    k: task.get(k)
                    for k in (
                        "id",
                        "category",
                        "brief",
                        "criteria",
                        "reference",
                        "provenance",
                        "readiness",
                    )
                }
                for task in benchmark["tasks"]
            ],
            "trials": [],
            "judge_calls": [],
            "scoring_policy": policy,
            "warnings": [
                "Native processes execute trusted tasks locally; this is not a hostile-code sandbox.",
                "One complete brief normally means one user prompt per fresh attempt.",
                "Model-judged scores reflect an inferred rubric, not objective correctness.",
            ],
        }
    if not path.exists() and reuse_report is not None:
        if Path(reuse_report).absolute().parent == output.absolute():
            raise ValueError("Evidence reuse requires a distinct previous run")
        report.update(
            reuse_revision_evidence(
                benchmark,
                identity,
                policy,
                sources,
                Path(reuse_report),
                Path(reuse_benchmark),
                output,
            )
        )
        report["warnings"].append(
            "Unchanged trial evidence was reused from the previous workflow revision, including failed outcomes. Reused evidence is not a new candidate execution."
        )
    snapshot_scoring(output, sources["scoring.py"])
    effective = dict(benchmark, repeats=repeats)
    if "history" in benchmark:
        report["history"] = benchmark["history"]
    started = time.monotonic()
    previous_elapsed = report.get("elapsed_seconds", 0)
    deadline = started + max(0, max_total_seconds - previous_elapsed)
    report["limits"] = {
        "max_total_seconds": max_total_seconds,
        "stop_after_cost_usd": stop_after_cost_usd,
    }

    order = {
        (task["id"], repeat): index
        for index, (task, repeat) in enumerate(
            (task, repeat) for task in benchmark["tasks"] for repeat in range(repeats)
        )
    }
    identities = {(t["task_id"], t["repeat"]) for t in report["trials"]}
    if len(identities) != len(report["trials"]) or not identities.issubset(order):
        raise ValueError("Saved report contains duplicate or unknown trial identities")
    task_by_id = {task["id"]: task for task in benchmark["tasks"]}
    saved_assessments = {}
    retry_candidates = set()
    for row in report["trials"]:
        task = task_by_id[row["task_id"]]
        if row["status"] in {"batch_budget_exceeded", "infrastructure_error"}:
            retry_candidates.add((row["task_id"], row["repeat"]))
        targets = {
            criterion.get("target", "output")
            for criterion in task["criteria"]
            if criterion.get("target", "output") != "output"
        }
        # Command graders can depend on arbitrary workspace state; declared artifact
        # targets suffice only for text/file checks, never an uncaptured command.
        restorable = not any(c["kind"] == "command" for c in task["criteria"]) and targets.issubset(
            row.get("artifacts", {})
        )
        if (
            row["status"] == "completed"
            and restorable
            and (
                row.get("assessment_interrupted")
                or row.get("budget_limited")
                or row.get("grading", {}).get("status") == "grading_error"
            )
        ):
            saved_assessments[(row["task_id"], row["repeat"])] = row
    done = identities - saved_assessments.keys() - retry_candidates
    report.setdefault("retry_history", [])

    def save():
        report["trials"].sort(key=lambda row: order[(row["task_id"], row["repeat"])])
        report["judge_calls"].sort(
            key=lambda row: order.get((row.get("task_id"), row.get("repeat")), -1)
        )
        report["elapsed_seconds"] = previous_elapsed + time.monotonic() - started
        report["summary"] = summarize(effective, report["trials"])
        write_json(path, report)
        write_private(output / "report.html", render_report(report))

    cancelled = Event()
    spending_lock = Lock()

    def measurements(row):
        latest = row.get("latest_attempt", row)
        return {
            key: latest.get(key)
            for key in (
                "status",
                "duration_seconds",
                "user_prompts",
                "usage",
                "cost_usd",
                "cost_kind",
                "error",
                "time_limit_seconds",
            )
        }

    observed_cost = sum(
        row.get("cost_usd") or 0
        for row in (
            [measurements(t) for t in report["trials"] if not t.get("reused_from")]
            + [t for t in report["retry_history"] if not t.get("reused_from")]
            if baseline_type == "fresh"
            else []
        )
        + [row for row in report["judge_calls"] if not row.get("reused_from")]
    )

    def observe_spend(row):
        nonlocal observed_cost
        cost = row.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost):
            with spending_lock:
                observed_cost += cost

    def budget_exhausted():
        with spending_lock:
            cost_stop = stop_after_cost_usd is not None and observed_cost >= stop_after_cost_usd
        return time.monotonic() + 0.1 >= deadline or cost_stop

    def unavailable(reason, status="unscorable"):
        return {
            "status": status,
            "quality_pct": None,
            "success": None,
            "criteria": [],
            "error": reason,
        }

    class TrialInterrupted(BaseException):
        def __init__(self, attempt, calls):
            self.attempt, self.calls = attempt, calls

    class TrialDeferred(Exception):
        pass

    def trial(task, repeat):
        class CallLog(list):
            def append(self, row):
                row = {**row, "task_id": task["id"], "repeat": repeat}
                super().append(row)
                observe_spend(row)

        calls = CallLog()
        attempt = None
        saved = saved_assessments.get((task["id"], repeat))
        with execution_cancellation(cancelled):
            if time.monotonic() + 0.1 >= deadline:
                raise TrialDeferred
            try:
                judge = (
                    model_callback(
                        judge_config,
                        allow_unsafe_local=allow_unsafe_local,
                        calls=calls,
                        deadline=deadline,
                    )
                    if judge_config
                    else None
                )
                with tempfile.TemporaryDirectory(prefix="personalbench-trial-") as directory:
                    workspace = _workspace(task, directory)
                    if saved is not None:
                        attempt = copy.deepcopy(saved)
                        attempt.pop("budget_limited", None)
                        for relative, contents in attempt.get("artifacts", {}).items():
                            artifact = workspace / safe_relative(relative)
                            artifact.parent.mkdir(parents=True, exist_ok=True)
                            artifact.write_text(contents, encoding="utf-8")
                        grading_workspace = workspace if attempt.get("artifacts") else None
                    elif baseline_type == "recorded":
                        attempt = dict(
                            task.get("historical", {}), status="completed", cost_kind="historical"
                        )
                        if not attempt.get("output"):
                            attempt.update(
                                status="unscorable", error="No historical output available"
                            )
                        grading_workspace = None
                    else:
                        remaining = deadline - time.monotonic()
                        if remaining < 0.1:
                            raise TrialDeferred
                        bounded = dict(
                            configuration,
                            timeout_seconds=min(
                                configuration.get("timeout_seconds", 120), remaining
                            ),
                        )
                        attempt = run_candidate(
                            task, bounded, workspace, allow_unsafe_local=allow_unsafe_local
                        )
                        grading_workspace = workspace
                        attempt["time_limit_seconds"] = bounded["timeout_seconds"]
                        if attempt["status"] == "budget_exceeded" and bounded[
                            "timeout_seconds"
                        ] < configuration.get("timeout_seconds", 120):
                            attempt["status"] = "batch_budget_exceeded"
                    if baseline_type == "fresh" and saved is None:
                        observe_spend(attempt)
                    artifacts = {}
                    for criterion in task["criteria"]:
                        target = criterion.get("target", "output")
                        if target != "output" and baseline_type == "fresh":
                            target_path = workspace / target
                            if (
                                target_path.is_file()
                                and not target_path.is_symlink()
                                and target_path.resolve().is_relative_to(workspace.resolve())
                                and target_path.stat().st_size <= 1_000_000
                            ):
                                artifacts[target] = target_path.read_text(errors="replace")
                    if artifacts:
                        attempt["artifacts"] = artifacts
                    if attempt["status"] == "budget_exceeded":
                        attempt["grading"] = {
                            "status": "scored",
                            "quality_pct": 0,
                            "success": False,
                            "criteria": [],
                            "evidence": "Task did not complete within the declared timeout",
                        }
                    else:
                        attempt["grading"] = grade(
                            task,
                            attempt,
                            grading_workspace,
                            judge,
                            success_threshold=benchmark.get("scoring", {}).get(
                                "success_threshold", 80
                            ),
                            allow_unsafe_local=allow_unsafe_local,
                            deadline=deadline,
                        )
                    attempt.pop("assessment_interrupted", None)
                    if time.monotonic() >= deadline and attempt["grading"]["status"] != "scored":
                        attempt["budget_limited"] = True

            except TrialDeferred:
                raise
            except KeyboardInterrupt as exc:
                if attempt is not None:
                    attempt["assessment_interrupted"] = True
                    attempt["grading"] = unavailable(
                        "Grading interrupted after candidate execution; saved without retry"
                    )
                raise TrialInterrupted(attempt, calls) from exc
            except Exception as exc:  # noqa: BLE001 - preserve unexpected worker failures as evidence
                reason = f"{type(exc).__name__}: {exc}"
                if attempt is None:
                    attempt = {
                        "status": "infrastructure_error",
                        "output": "",
                        "duration_seconds": None,
                        "user_prompts": None,
                        "usage": {},
                        "cost_usd": None,
                        "cost_kind": "unknown",
                        "error": reason,
                        "grading": unavailable(reason),
                    }
                else:
                    attempt["grading"] = unavailable(reason, "grading_error")
        return attempt, calls

    def record(task, repeat, attempt, calls):
        key = (task["id"], repeat)
        if key in done or attempt is None:
            return
        previous = next(
            (row for row in report["trials"] if (row["task_id"], row["repeat"]) == key), None
        )
        if previous is not None and previous["status"] in {
            "batch_budget_exceeded",
            "infrastructure_error",
        }:
            report["retry_history"].append(
                {
                    **measurements(previous),
                    "task_id": task["id"],
                    "repeat": repeat,
                    "reason": (
                        "Batch time limit prevented a full attempt; retried on explicit resume"
                        if previous["status"] == "batch_budget_exceeded"
                        else "Infrastructure failure prevented a completed candidate; retried on explicit resume"
                    ),
                }
            )
        prior = [row for row in report["retry_history"] if (row["task_id"], row["repeat"]) == key]
        if prior:
            latest = measurements(attempt)
            attempt["latest_attempt"] = latest
            samples = prior + [latest]
            for field in ("duration_seconds", "user_prompts", "cost_usd"):
                attempt[field] = (
                    sum(row[field] for row in samples)
                    if all(row.get(field) is not None for row in samples)
                    else None
                )
            attempt["cost_kind"] = "accumulated" if attempt["cost_usd"] is not None else "unknown"
            usages = [row.get("usage") or {} for row in samples]
            attempt["usage"] = {
                category: sum(usage[category] for usage in usages)
                for category in set.intersection(*(set(usage) for usage in usages))
            }
        report["trials"] = [
            row for row in report["trials"] if (row["task_id"], row["repeat"]) != key
        ]
        report["trials"].append({**attempt, "task_id": task["id"], "repeat": repeat})
        report["judge_calls"].extend(calls)
        done.add(key)

    jobs = deque()
    for task in benchmark["tasks"]:
        for repeat in range(repeats):
            if (task["id"], repeat) in done:
                continue
            if task.get("readiness", "ready") != "ready" and baseline_type == "fresh":
                reason = task.get("provenance", {}).get("readiness_reason") or (
                    "Task inputs are incomplete or unsupported; no candidate call made"
                )
                record(
                    task,
                    repeat,
                    {
                        "status": "preparation_error",
                        "output": "",
                        "duration_seconds": None,
                        "user_prompts": None,
                        "usage": {},
                        "cost_usd": None,
                        "error": reason,
                        "grading": unavailable(reason),
                    },
                    [],
                )
            else:
                jobs.append((task, repeat))
    report["run_status"] = "running"
    save()
    if progress:
        progress(
            {
                "stage": "evaluation",
                "completed_trials": len(report["trials"]),
                "expected_trials": len(benchmark["tasks"]) * repeats,
            }
        )
    pending = {}
    deferred = False
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="personalbench")
    try:
        while jobs or pending:
            while jobs and len(pending) < workers and not budget_exhausted():
                task, repeat = jobs.popleft()
                pending[executor.submit(trial, task, repeat)] = (task, repeat)
            if not pending:
                break
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in sorted(
                completed, key=lambda item: order[(pending[item][0]["id"], pending[item][1])]
            ):
                task, repeat = pending[future]
                try:
                    attempt, calls = future.result()
                except TrialDeferred:
                    deferred = True
                else:
                    record(task, repeat, attempt, calls)
                del pending[future]
                save()
                if progress:
                    progress(
                        {
                            "stage": "evaluation",
                            "task": task["id"],
                            "completed_trials": len(report["trials"]),
                            "expected_trials": len(benchmark["tasks"]) * repeats,
                        }
                    )
    except BaseException as cause:
        cancelled.set()
        for future in pending:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        for future, (task, repeat) in pending.items():
            if future.cancelled():
                continue
            try:
                attempt, calls = future.result()
            except TrialInterrupted as exc:
                record(task, repeat, exc.attempt, exc.calls)
            except TrialDeferred:
                pass
            except BaseException as exc:  # noqa: BLE001 - preserve worker termination evidence
                reason = f"Worker terminated: {type(exc).__name__}: {exc}"
                record(
                    task,
                    repeat,
                    {
                        "status": "infrastructure_error",
                        "output": "",
                        "duration_seconds": None,
                        "user_prompts": None,
                        "usage": {},
                        "cost_usd": None,
                        "error": reason,
                        "grading": unavailable(reason),
                    },
                    [],
                )
            else:
                record(task, repeat, attempt, calls)
        report["run_status"] = (
            "interrupted" if isinstance(cause, (KeyboardInterrupt, TrialInterrupted)) else "error"
        )
        save()
        if isinstance(cause, TrialInterrupted):
            raise KeyboardInterrupt from None
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    report["run_status"] = (
        "budget_stopped"
        if jobs or deferred or any(t.get("budget_limited") for t in report["trials"])
        else "complete"
    )
    save()
    return report


def evaluate(benchmark, configuration, output: Path, **kwargs):
    """Serialize writers to a run directory across processes, including resumes."""
    output = private_directory(output)
    descriptor = os.open(output / ".run.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another process is already evaluating this output directory") from exc
        return _evaluate(benchmark, configuration, output, **kwargs)
    finally:
        os.close(descriptor)
