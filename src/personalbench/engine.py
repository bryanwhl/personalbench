"""Checkpointed evaluation orchestration; private answers never enter candidate inputs."""

from __future__ import annotations

import fcntl
import hashlib
import os
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .grading import grade
from .privacy import assert_no_secrets, private_directory, read_json, write_json, write_private
from .report import render_report
from .runners import RunnerError, run_candidate
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
    return {"harness": config["harness"], "version": value}


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
):
    validate_benchmark(benchmark)
    validate_config(configuration)
    if judge_config:
        validate_config(judge_config)
    if not isinstance(repeats, int) or not 1 <= repeats <= 100:
        raise ValueError("repeats must be between 1 and 100")
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
    policy = {
        "personalbench_version": __version__,
        "grader_implementation": {
            name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
            for name in ("grading.py", "scoring.py")
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
    }
    if path.exists():
        if not resume:
            raise ValueError(
                "Output already contains a run; use --resume or a new output directory"
            )
        report = read_json(path)
        if any(report.get(k) != v for k, v in identity.items()):
            raise ValueError(
                "Cannot resume with changed benchmark, configuration, grader, or repeats"
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
    effective = dict(benchmark, repeats=repeats)
    started = time.monotonic()
    previous_elapsed = report.get("elapsed_seconds", 0)
    deadline = started + max(0, max_total_seconds - previous_elapsed)
    report["limits"] = {
        "max_total_seconds": max_total_seconds,
        "stop_after_cost_usd": stop_after_cost_usd,
    }

    def save():
        report["elapsed_seconds"] = previous_elapsed + time.monotonic() - started
        report["summary"] = summarize(effective, report["trials"])
        write_json(path, report)
        write_private(output / "report.html", render_report(report))

    save()
    done = {(t["task_id"], t["repeat"]) for t in report["trials"]}
    judge = (
        model_callback(
            judge_config,
            allow_unsafe_local=allow_unsafe_local,
            calls=report["judge_calls"],
            deadline=deadline,
        )
        if judge_config
        else None
    )
    for task in benchmark["tasks"]:
        for repeat in range(repeats):
            if (task["id"], repeat) in done:
                continue
            total_cost = sum(
                t.get("cost_usd") or 0 for t in report["trials"] + report["judge_calls"]
            )
            elapsed = previous_elapsed + time.monotonic() - started
            if elapsed + 0.1 >= max_total_seconds or (
                stop_after_cost_usd is not None and total_cost >= stop_after_cost_usd
            ):
                report["run_status"] = "budget_stopped"
                save()
                return report
            if task.get("readiness", "ready") != "ready" and baseline_type == "fresh":
                attempt = {
                    "status": "preparation_error",
                    "output": "",
                    "duration_seconds": None,
                    "user_prompts": None,
                    "usage": {},
                    "cost_usd": None,
                    "error": "Task inputs are incomplete or unsupported; no candidate call made",
                    "grading": {
                        "status": "unscorable",
                        "quality_pct": None,
                        "success": None,
                        "criteria": [],
                    },
                }
            else:
                attempt = None
                try:
                    with tempfile.TemporaryDirectory(prefix="personalbench-trial-") as directory:
                        workspace = _workspace(task, directory)
                        if baseline_type == "recorded":
                            attempt = dict(
                                task.get("historical", {}),
                                status="completed",
                                cost_kind="historical",
                            )
                            if not attempt.get("output"):
                                attempt.update(
                                    status="unscorable", error="No historical output available"
                                )
                            # Starting files are not the recorded final artifacts.
                            grading_workspace = None
                        else:
                            bounded = dict(
                                configuration,
                                timeout_seconds=min(
                                    configuration.get("timeout_seconds", 120),
                                    max_total_seconds - elapsed,
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
                        # Retain declared artifact targets only, with path and size checks.
                        artifacts = {}
                        for criterion in task["criteria"]:
                            target = criterion.get("target", "output")
                            if target != "output" and baseline_type == "fresh":
                                target_path = workspace / target
                                if (
                                    target_path.is_file()
                                    and not target_path.is_symlink()
                                    and target_path.resolve().is_relative_to(workspace.resolve())
                                ) and target_path.stat().st_size <= 1_000_000:
                                    artifacts[target] = target_path.read_text(errors="replace")
                        if artifacts:
                            attempt["artifacts"] = artifacts
                except KeyboardInterrupt:
                    if attempt is not None:
                        attempt["grading"] = {
                            "status": "unscorable",
                            "quality_pct": None,
                            "success": None,
                            "criteria": [],
                            "error": "Grading interrupted after candidate execution; saved without retry",
                        }
                        report["trials"].append(
                            {**attempt, "task_id": task["id"], "repeat": repeat}
                        )
                    report["run_status"] = "interrupted"
                    save()
                    raise
            report["trials"].append({**attempt, "task_id": task["id"], "repeat": repeat})
            save()
    report["run_status"] = "complete"
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
