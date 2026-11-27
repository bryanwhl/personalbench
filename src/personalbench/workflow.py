"""One checkpointed history-to-report workflow using the same public build/run primitives."""

from __future__ import annotations

import fcntl
import hashlib
import os
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from .curation import CURATION_VERSION, CurationError, curate_benchmark
from .engine import evaluate, model_callback
from .history import build_history, discover_sessions, parse_as_of
from .privacy import private_directory, read_json, write_json
from .recovery import extract_evidence
from .runners import adapter_identity
from .schema import canonical_hash, validate_config


def _preparation_identity(config):
    return {
        "compiler": config,
        "implementation": {
            name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
            for name in ("imports.py", "history.py", "recovery.py", "session_io.py")
        },
    }


def _assessor_config(candidate, explicit, role):
    config = dict(explicit if explicit is not None else candidate)
    if explicit is None:
        config.pop("instructions", None)
        config["name"] = f"{candidate['name']} — isolated {role}"
    config.update(tools="none", sandbox="read-only")
    return validate_config(config)


def evaluate_history(
    output: Path,
    configuration: dict,
    *,
    compiler_config: dict | None = None,
    judge_config: dict | None = None,
    curator_config: dict | None = None,
    days: int = 120,
    include_automations: bool | None = None,
    as_of: str | None = None,
    source: str = "all",
    roots: list[Path] | None = None,
    exclude: list[Path] | None = None,
    exclude_session_ids: list[str] | None = None,
    workers: int = 3,
    preparation_workers: int = 3,
    max_preparation_seconds: float = 1800,
    max_total_seconds: float = 1800,
    stop_after_cost_usd: float | None = None,
    resume: bool = False,
    refresh_preparation: bool = False,
    allow_unsafe_local: bool = False,
    progress=None,
):
    """Freeze sources, recover inputs, compile all goals, then evaluate the fixed suite.

    Preparation and execution have separate explicit budgets. Missing historical data is
    an audited coverage gap; model execution or preparation errors remain visible.
    """
    if refresh_preparation and not resume:
        raise ValueError("Refreshing preparation requires --resume of an existing frozen workflow")
    validate_config(configuration)
    compiler_config = _assessor_config(configuration, compiler_config, "compiler")
    judge_config = _assessor_config(configuration, judge_config, "judge")
    curator_config = _assessor_config(compiler_config, curator_config, "curator")
    if not allow_unsafe_local:
        raise ValueError("History evaluation requires --allow-unsafe-local for trusted tasks")
    for label, value in (("workers", workers), ("preparation_workers", preparation_workers)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 8:
            raise ValueError(f"{label} must be between 1 and 8")
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise ValueError("days must be a positive integer")
    if not 0 < max_total_seconds <= 86400:
        raise ValueError("max_total_seconds must be between zero and 86400")
    if stop_after_cost_usd is not None and not stop_after_cost_usd > 0:
        raise ValueError("stop_after_cost_usd must be positive")
    if not 0 < max_preparation_seconds <= 86400:
        raise ValueError("max_preparation_seconds must be between zero and 86400")
    output = private_directory(output)
    descriptor = os.open(output / ".workflow.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another process is already running this workflow") from exc
        manifest_path = output / "workflow.json"
        previous_manifest = read_json(manifest_path) if manifest_path.exists() else None
        if include_automations is None:
            include_automations = (
                previous_manifest["identity"].get("include_automations", True)
                if previous_manifest and resume
                else False
            )
        if not isinstance(include_automations, bool):
            raise TypeError("include_automations must be boolean")
        identity = {
            "configuration": configuration,
            "compiler": compiler_config,
            "judge": judge_config,
            "curator": curator_config,
            "days": days,
            "include_automations": include_automations,
            "source": source,
            "roots": [str(Path(p).expanduser().resolve()) for p in roots] if roots else None,
            "exclude": [str(Path(p).expanduser().resolve()) for p in exclude or []],
            "exclude_session_ids": sorted(set(exclude_session_ids or [])),
            "workers": workers,
            "preparation_workers": preparation_workers,
            "preparation_identity": _preparation_identity(compiler_config),
        }
        if manifest_path.exists():
            if not resume:
                raise ValueError("Workflow output already exists; use --resume or a new output")
            manifest = previous_manifest
            # Legacy windows included automation runs. Plain resume preserves that
            # frozen scope; refresh explicitly introduces the current curation stage.
            manifest["identity"].setdefault("include_automations", True)
            manifest["identity"].setdefault("curator", curator_config)
            if manifest["identity"] != identity:
                old_settings = {
                    key: value
                    for key, value in manifest["identity"].items()
                    if key != "preparation_identity"
                }
                new_settings = {
                    key: value for key, value in identity.items() if key != "preparation_identity"
                }
                roles = {"compiler", "curator"}
                old_scope = {k: v for k, v in old_settings.items() if k not in roles}
                new_scope = {k: v for k, v in new_settings.items() if k not in roles}
                extensions = []
                budget_increase = refresh_preparation and old_scope == new_scope
                for role in sorted(roles):
                    old_role = dict(old_settings[role])
                    new_role = dict(new_settings[role])
                    before = old_role.pop("timeout_seconds", 120)
                    after = new_role.pop("timeout_seconds", 120)
                    budget_increase = budget_increase and old_role == new_role and after >= before
                    if after != before:
                        extensions.append(
                            {
                                "role": role,
                                "previous_timeout_seconds": before,
                                "timeout_seconds": after,
                                "reason": "Explicit assessor timeout extension; completed decisions retained",
                            }
                        )
                if old_settings != new_settings and not budget_increase:
                    raise ValueError("Cannot resume with changed configuration or scope")
                if budget_increase:
                    manifest.setdefault("preparation_budget_changes", []).extend(extensions)
                if (
                    refresh_preparation
                    or not (output / manifest.get("benchmark_path", "benchmark.json")).exists()
                ):
                    manifest["identity"] = identity
                    write_json(manifest_path, manifest)
            if as_of and parse_as_of(as_of).isoformat() != manifest["as_of"]:
                raise ValueError("Cannot resume with a changed as-of window")
        elif resume:
            raise ValueError("No workflow exists at this output path")
        else:
            manifest = {
                "schema_version": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "as_of": parse_as_of(as_of).isoformat(),
                "identity": identity,
                "stage": "discovery",
            }
            write_json(manifest_path, manifest)
        inventory_path = output / "inventory.json"
        if inventory_path.exists():
            inventory = read_json(inventory_path)
        else:
            inventory = discover_sessions(
                days=days,
                as_of=parse_as_of(manifest["as_of"]),
                source=source,
                roots=roots,
                exclude=exclude,
                exclude_session_ids=exclude_session_ids,
                include_automations=include_automations,
                snapshot_dir=output / "sources",
            )
            write_json(inventory_path, inventory)
        benchmark_path = output / manifest.get("benchmark_path", "benchmark.json")
        run_directory = output / manifest.get("run_directory", "run")
        refresh_preparation = refresh_preparation or manifest.get("refresh_pending", False)
        if refresh_preparation:
            manifest["refresh_pending"] = True
            # Reclassify unchanged data-only gaps with current parsers, never replace snapshots.
            for row in inventory["sessions"]:
                if (
                    row.get("status") == "error"
                    and row.get("error_stage") == "import"
                    and not row.get("snapshot_path")
                ):
                    latest = discover_sessions(
                        days=days,
                        as_of=parse_as_of(manifest["as_of"]),
                        roots=[Path(row["path"]).parent],
                        include_automations=include_automations,
                    )
                    matching = next(
                        (entry for entry in latest["sessions"] if entry["path"] == row["path"]),
                        None,
                    )
                    if (
                        matching
                        and matching.get("original_source_digest")
                        == row.get("original_source_digest")
                        and matching.get("status") == "excluded"
                    ):
                        row.clear()
                        row.update(matching)

        preparation_path = output / "preparation.json"
        reuse_report = None
        reuse_benchmark = None
        if refresh_preparation or not benchmark_path.exists():
            manifest["stage"] = "preparation"
            write_json(manifest_path, manifest)
            calls = (
                read_json(preparation_path).get("calls", []) if preparation_path.exists() else []
            )
            preparation_started = time.monotonic()
            inventory.setdefault("include_automations", include_automations)
            previous_elapsed = manifest.get("preparation_elapsed_seconds", 0)
            deadline = preparation_started + max(0, max_preparation_seconds - previous_elapsed)
            compiler = model_callback(
                compiler_config,
                allow_unsafe_local=allow_unsafe_local,
                calls=calls,
                deadline=deadline,
            )

            def prepare_session(session, path):
                session["evidence"] = extract_evidence(path)
                session["history_window"] = {key: inventory[key] for key in ("since", "as_of")}
                return session

            def checkpoint(entry):
                manifest["preparation_elapsed_seconds"] = (
                    previous_elapsed + time.monotonic() - preparation_started
                )
                write_json(manifest_path, manifest)
                write_json(inventory_path, inventory)
                write_json(preparation_path, {"calls": list(calls)})
                if progress:
                    progress(
                        {
                            "stage": "preparation",
                            "session": entry["path"],
                            "status": entry["status"],
                        }
                    )

            try:
                benchmark = build_history(
                    inventory,
                    name=f"Personal work — {days} days ending {manifest['as_of'][:10]}",
                    compiler=compiler,
                    artifact_dir=output / "preparation",
                    resume=resume,
                    compiler_identity=identity["preparation_identity"],
                    workers=preparation_workers,
                    prepare_session=prepare_session,
                    progress=checkpoint,
                )
            except ValueError:
                manifest["preparation_elapsed_seconds"] = (
                    previous_elapsed + time.monotonic() - preparation_started
                )
                manifest.update(
                    stage="preparation_incomplete",
                    source_status=dict(Counter(row["status"] for row in inventory["sessions"])),
                )
                write_json(preparation_path, {"calls": list(calls)})
                write_json(inventory_path, inventory)
                write_json(manifest_path, manifest)
                if any(
                    row.get("error_stage") in {"preparation", "integrity"}
                    for row in inventory["sessions"]
                ):
                    return {"workflow": manifest, "report": None}
                raise
            manifest["preparation_elapsed_seconds"] = (
                previous_elapsed + time.monotonic() - preparation_started
            )
            preparation_errors = [
                row
                for row in inventory["sessions"]
                if row.get("status") == "error"
                and row.get("error_stage") in {"preparation", "integrity"}
            ]
            if preparation_errors:
                manifest.update(
                    stage="preparation_incomplete",
                    source_status=dict(Counter(row["status"] for row in inventory["sessions"])),
                )
                write_json(output / "benchmark.draft.json", benchmark)
                write_json(preparation_path, {"calls": list(calls)})
                write_json(inventory_path, inventory)
                write_json(manifest_path, manifest)
                return {"workflow": manifest, "report": None}
            write_json(output / "benchmark.uncurated.json", benchmark)
            manifest["stage"] = "curation"
            write_json(manifest_path, manifest)

            def curation_checkpoint(event):
                manifest["preparation_elapsed_seconds"] = (
                    previous_elapsed + time.monotonic() - preparation_started
                )
                write_json(preparation_path, {"calls": list(calls)})
                write_json(manifest_path, manifest)
                if progress:
                    progress({"stage": "curation", **event})

            try:
                benchmark = curate_benchmark(
                    benchmark,
                    curator=model_callback(
                        curator_config,
                        allow_unsafe_local=allow_unsafe_local,
                        calls=calls,
                        deadline=deadline,
                    ),
                    artifact_dir=output / "curation",
                    curator_identity=curator_config,
                    resume=resume,
                    progress=curation_checkpoint,
                )
            except CurationError as exc:
                manifest.update(
                    stage="curation_incomplete",
                    curation_error=exc.as_dict(),
                    source_status=dict(Counter(row["status"] for row in inventory["sessions"])),
                    preparation_elapsed_seconds=previous_elapsed
                    + time.monotonic()
                    - preparation_started,
                )
                write_json(preparation_path, {"calls": list(calls)})
                write_json(manifest_path, manifest)
                return {"workflow": manifest, "report": None}
            manifest.pop("curation_error", None)
            manifest["curation"] = {
                key: value
                for key, value in benchmark["history"]["semantic_curation"].items()
                if key not in {"families"}
            }
            manifest["curation_version"] = CURATION_VERSION
            manifest["preparation_elapsed_seconds"] = (
                previous_elapsed + time.monotonic() - preparation_started
            )
            new_hash = canonical_hash(benchmark)
            if (
                refresh_preparation
                and benchmark_path.exists()
                and new_hash != canonical_hash(read_json(benchmark_path))
            ):
                # Reuse is scoped to this immediate predecessor, never an older
                # revision left in the manifest after an interrupted refresh.
                manifest.pop("reused_evidence_from", None)
                if (run_directory / "report.json").exists():
                    previous_report = read_json(run_directory / "report.json")
                    if previous_report.get("run_status") in {"complete", "interrupted"}:
                        reuse_report = run_directory / "report.json"
                        reuse_benchmark = benchmark_path
                        manifest["reused_evidence_from"] = {
                            "report": str(reuse_report),
                            "benchmark": str(reuse_benchmark),
                        }
                manifest.setdefault("revisions", []).append(
                    {
                        "benchmark_path": str(benchmark_path),
                        "run_directory": str(run_directory),
                        "benchmark_hash": canonical_hash(read_json(benchmark_path)),
                    }
                )
                revision = manifest.get("revision", 1) + 1
                benchmark_path = output / f"benchmark.v{revision}.json"
                run_directory = output / f"run.v{revision}"
                manifest["revision"] = revision
            manifest.update(
                benchmark_path=benchmark_path.name,
                run_directory=run_directory.name,
                refresh_pending=False,
            )
            write_json(benchmark_path, benchmark)
            write_json(inventory_path, inventory)
            write_json(preparation_path, {"calls": list(calls)})
        else:
            benchmark = read_json(benchmark_path)
        manifest.update(stage="evaluation", benchmark_hash=canonical_hash(benchmark))
        write_json(manifest_path, manifest)
        report_path = run_directory / "report.json"
        if (
            not report_path.exists()
            and reuse_report is None
            and manifest.get("reused_evidence_from")
        ):
            reuse_report = Path(manifest["reused_evidence_from"]["report"])
            reuse_benchmark = Path(manifest["reused_evidence_from"]["benchmark"])
        if not report_path.exists() and reuse_benchmark is not None:
            previous_scoring = read_json(reuse_benchmark).get("scoring", {})
            if previous_scoring != benchmark.get("scoring", {}):
                manifest["reuse_skipped"] = {
                    "reason": "Scoring contract changed; the new revision requires fresh attempts",
                    "source_benchmark": str(reuse_benchmark),
                    "previous_scoring": previous_scoring,
                    "scoring": benchmark.get("scoring", {}),
                }
                manifest.pop("reused_evidence_from", None)
                reuse_report = reuse_benchmark = None
                write_json(manifest_path, manifest)
        if not report_path.exists() and reuse_report is not None:
            previous = read_json(reuse_report)
            changed = []
            for role, config, runtime in (
                ("candidate", configuration, previous.get("runtime", {})),
                (
                    "judge",
                    judge_config,
                    previous.get("scoring_policy", {}).get("judge_runtime") or {},
                ),
            ):
                expected = adapter_identity(config)
                if any(runtime.get(key) != value for key, value in expected.items()):
                    changed.append(role)
            if changed:
                manifest["reuse_skipped"] = {
                    "reason": "Native adapter or input boundary changed; fresh evidence is required",
                    "source_report": str(reuse_report),
                    "roles": changed,
                }
                manifest.pop("reused_evidence_from", None)
                reuse_report = reuse_benchmark = None
                write_json(manifest_path, manifest)
        report = evaluate(
            benchmark,
            configuration,
            run_directory,
            judge_config=judge_config,
            workers=workers,
            progress=progress,
            resume=resume and report_path.exists(),
            allow_unsafe_local=allow_unsafe_local,
            max_total_seconds=max_total_seconds,
            stop_after_cost_usd=stop_after_cost_usd,
            reuse_report=reuse_report if not report_path.exists() else None,
            reuse_benchmark=reuse_benchmark if not report_path.exists() else None,
        )
        rows = {row["task_id"]: row for row in report["summary"]["tasks"]}
        gaps = [
            task["id"]
            for task in benchmark["tasks"]
            if task.get("readiness", "ready") == "ready"
            and not rows.get(task["id"], {}).get("complete", False)
        ]
        stage = report["run_status"]
        if stage == "complete" and gaps:
            stage = "evaluation_incomplete"
        manifest.update(
            stage=stage,
            evaluation_gaps=gaps,
            source_status=dict(Counter(row["status"] for row in inventory["sessions"])),
            goal_readiness=dict(Counter(t.get("readiness", "ready") for t in benchmark["tasks"])),
            report=str(run_directory / "report.html"),
        )
        write_json(manifest_path, manifest)
        return {"workflow": manifest, "report": report}
    finally:
        os.close(descriptor)
