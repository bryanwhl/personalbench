"""Local-first command line interface. Generated data never belongs in the public repo."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .capture import capture_finish, capture_start, capture_watch
from .curation import CurationError, curate_benchmark
from .engine import evaluate, model_callback
from .history import _normalize, build_history, discover_sessions, parse_as_of
from .imports import build_benchmark, import_session
from .privacy import DEFAULT_HOME, read_json, scan_paths, write_json, write_private
from .recovery import extract_evidence
from .report import render_report
from .schema import validate_benchmark
from .scoring import compare_reports
from .session_io import read_session_bytes
from .suites import build_replayable_suite
from .workflow import _preparation_identity, evaluate_history


def _destination(value: str | None, default: str) -> Path:
    path = Path(value).expanduser().absolute() if value else DEFAULT_HOME / default
    # Generated artifacts inside Git must go in the conventional ignored directory.
    for ancestor in (path.parent, *path.parents):
        if (ancestor / ".git").exists():
            if not path.is_relative_to(ancestor / ".personalbench"):
                raise ValueError("Keep personal artifacts outside Git or under .personalbench/")
            break
    return path


def _json(value):
    print(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))


def _model_options(parser):
    parser.add_argument("--judge", help="JSON configuration for the automated rubric judge")
    parser.add_argument(
        "--allow-unsafe-local",
        action="store_true",
        help="Authorize native execution on trusted tasks; not a security sandbox",
    )


def _history_options(parser):
    parser.add_argument(
        "--days", type=int, default=120, help="Trailing history window (default: 120 days)"
    )
    parser.add_argument(
        "--include-automations",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Include automation-origin requests (default: human coding-agent work only)",
    )
    parser.add_argument("--as-of", help="ISO timestamp with timezone; default: now")
    parser.add_argument("--source", choices=["all", "codex", "claude"], default="all")
    parser.add_argument(
        "--session-root", action="append", help="Explicit JSONL directory; repeatable"
    )
    parser.add_argument(
        "--exclude-session-id",
        action="append",
        default=[],
        help="Exclude a chat and its descendants",
    )
    parser.add_argument(
        "--exclude-session", action="append", default=[], help="Exclude a session path"
    )


def _excluded_ids(args):
    return sorted(set(args.exclude_session_id))


def _discover(args, snapshot_dir=None):
    return discover_sessions(
        days=args.days,
        as_of=parse_as_of(args.as_of),
        source=args.source,
        roots=[Path(p) for p in args.session_root] if args.session_root else None,
        exclude=[Path(p) for p in args.exclude_session],
        exclude_session_ids=_excluded_ids(args),
        snapshot_dir=snapshot_dir,
        include_automations=bool(args.include_automations),
    )


def parser():
    root = argparse.ArgumentParser(prog="personalbench", description="Your work. Your benchmark.")
    root.add_argument("--version", action="version", version=__version__)
    sub = root.add_subparsers(dest="action", required=True)
    imp = sub.add_parser("import", help="Normalize a selected local session")
    imp.add_argument("session")
    imp.add_argument("--format", choices=["auto", "codex", "claude", "normalized"], default="auto")
    imp.add_argument("--output")
    build = sub.add_parser("build", help="Compile goals and criteria into a benchmark")
    build.add_argument("session", nargs="?", help="One session; omit for the past 120 days")
    _history_options(build)
    build.add_argument("--output")
    build.add_argument("--name", default="Personal benchmark")
    build.add_argument(
        "--compiler", help="Optional model configuration for autonomous goal compilation"
    )
    build.add_argument("--allow-unsafe-local", action="store_true")
    build.add_argument("--curator", help="Global deduplication model config; defaults to compiler")
    build.add_argument("--resume", action="store_true")
    build.add_argument("--workers", type=int, default=3)
    build.add_argument(
        "--max-preparation-seconds",
        type=float,
        default=1800,
        help="Per-invocation preparation budget; prior call usage is retained on resume",
    )
    flow = sub.add_parser("evaluate", help="Distill unique task families from the past 120 days")
    _history_options(flow)
    _model_options(flow)
    flow.add_argument("--config", required=True)
    flow.add_argument(
        "--compiler", help="Compiler config; defaults to candidate model with tools disabled"
    )
    flow.add_argument("--output")
    flow.add_argument("--curator", help="Global deduplication model config; defaults to compiler")
    simulation = flow.add_mutually_exclusive_group()
    simulation.add_argument(
        "--variants", help="Explicit ready simulator adaptations for catalogue goals"
    )
    simulation.add_argument(
        "--clear-variants",
        action="store_true",
        help="Withdraw frozen variants during an explicit preparation refresh",
    )
    flow.add_argument("--resume", action="store_true")
    flow.add_argument(
        "--refresh-preparation",
        action="store_true",
        help="Reaudit frozen inputs after fixes; preserve older benchmark/run revisions",
    )
    flow.add_argument("--workers", type=int, default=3)
    flow.add_argument("--preparation-workers", type=int, default=3)
    flow.add_argument("--max-preparation-seconds", type=float, default=1800)
    flow.add_argument("--max-total-seconds", type=float, default=1800)
    flow.add_argument("--stop-after-cost-usd", type=float)

    val = sub.add_parser("validate", help="Validate a benchmark without running models")
    val.add_argument("benchmark")
    suite = sub.add_parser("suite", help="Export a runnable suite and retain catalogue coverage")
    suite.add_argument("catalogue")
    suite.add_argument("--variants", help="Explicit simulated task adaptations")
    suite.add_argument("--output", required=True)
    for name in ("run", "grade-recorded"):
        run = sub.add_parser(
            name, help="Execute fresh attempts" if name == "run" else "Grade recorded work"
        )
        run.add_argument("benchmark")
        run.add_argument("--output")
        run.add_argument("--resume", action="store_true")
        run.add_argument("--workers", type=int, default=1)
        run.add_argument("--max-total-seconds", type=float, default=1800)
        run.add_argument(
            "--stop-after-cost-usd",
            type=float,
            help="Stop scheduling after observed spend reaches this value; not a hard dollar cap",
        )
        _model_options(run)
        if name == "run":
            run.add_argument("--config", required=True)
            run.add_argument("--repeats", type=int, default=1)
    compare = sub.add_parser(
        "compare", help="Compare compatible runs without changing the benchmark"
    )
    compare.add_argument("reports", nargs="+")
    compare.add_argument("--output")
    report = sub.add_parser("report", help="Render an existing report to private HTML")
    report.add_argument("report")
    report.add_argument("--output")
    cap = sub.add_parser("capture", help="Preserve starting inputs for a new task")
    caps = cap.add_subparsers(dest="capture_action", required=True)
    start = caps.add_parser("start")
    start.add_argument("--source")
    start.add_argument("--output", required=True)
    finish = caps.add_parser("finish")
    finish.add_argument("capture")
    finish.add_argument("session")
    finish.add_argument("--output")
    watch = sub.add_parser("watch", help="Bounded polling of one selected session file")
    watch.add_argument("session")
    watch.add_argument("--output")
    watch.add_argument("--capture")
    watch.add_argument("--duration", type=float, default=60)
    watch.add_argument("--interval", type=float, default=2)
    scan = sub.add_parser("scan", help="Find possible secrets without printing matched values")
    scan.add_argument("path", nargs="?", default=".")
    scan.add_argument("--git", action="store_true", help="Inspect tracked/staged files only")
    sub.add_parser("doctor", help="Check local prerequisites without reading credentials")
    sessions = sub.add_parser("sessions", help="List local session filenames; never print content")
    _history_options(sessions)
    return root


def main(argv=None):
    arguments = list(argv if argv is not None else sys.argv[1:])
    args = parser().parse_args(arguments)
    try:
        if args.action == "suite":
            benchmark = build_replayable_suite(
                read_json(Path(args.catalogue)),
                read_json(Path(args.variants)) if args.variants else None,
            )
            output = Path(args.output).expanduser()
            write_json(output, benchmark)
            _json(
                {
                    "output": str(output),
                    "tasks": len(benchmark["tasks"]),
                    "catalogue_tasks": benchmark["suite"]["total_tasks"],
                    "origins": benchmark["suite"]["origin_counts"],
                }
            )
        elif args.action == "import":
            session = import_session(Path(args.session).expanduser(), format=args.format)
            output = _destination(args.output, "session.json")
            write_json(output, session)
            _json(
                {
                    "output": str(output),
                    "messages": len(session["messages"]),
                    "warnings": session.get("warnings", []),
                }
            )
        elif args.action == "evaluate":
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S-%fZ")
            output = _destination(args.output, f"evaluations/{stamp}")
            if args.resume and (output / "workflow.json").exists():
                frozen = read_json(output / "workflow.json")["identity"]
                if not any(value == "--days" or value.startswith("--days=") for value in arguments):
                    args.days = frozen["days"]
                if args.include_automations is None:
                    args.include_automations = frozen.get("include_automations", True)
            result = evaluate_history(
                output,
                read_json(Path(args.config)),
                compiler_config=read_json(Path(args.compiler)) if args.compiler else None,
                judge_config=read_json(Path(args.judge)) if args.judge else None,
                curator_config=read_json(Path(args.curator)) if args.curator else None,
                simulation_variants=read_json(Path(args.variants)) if args.variants else None,
                clear_variants=args.clear_variants,
                days=args.days,
                include_automations=args.include_automations,
                as_of=args.as_of,
                source=args.source,
                roots=[Path(p) for p in args.session_root] if args.session_root else None,
                exclude=[Path(p) for p in args.exclude_session],
                exclude_session_ids=_excluded_ids(args),
                workers=args.workers,
                preparation_workers=args.preparation_workers,
                max_preparation_seconds=args.max_preparation_seconds,
                max_total_seconds=args.max_total_seconds,
                stop_after_cost_usd=args.stop_after_cost_usd,
                resume=args.resume,
                refresh_preparation=args.refresh_preparation,
                allow_unsafe_local=args.allow_unsafe_local,
                progress=lambda event: print(json.dumps(event), file=sys.stderr),
            )
            report = result.get("report")
            workflow = result["workflow"]
            summary = report["summary"] if report else {}
            _json(
                {
                    "stage": workflow["stage"],
                    "report": workflow.get("report"),
                    "sources": workflow.get("source_status", {}),
                    "goals": workflow.get("goal_readiness", {}),
                    "curation": {
                        key: value
                        for key, value in workflow.get("curation", {}).items()
                        if key
                        in {
                            "original_occurrences",
                            "family_count",
                            "duplicates_removed",
                            "daily_brief_occurrences",
                        }
                    },
                    "success_pct": summary.get("success_pct"),
                    "output_quality_pct": summary.get("output_quality_pct"),
                    "efficiency_pct": summary.get("efficiency_pct"),
                    "observed_success_pct": summary.get("observed_success_pct"),
                    "observed_quality_pct": summary.get("observed_quality_pct"),
                    "coverage": {
                        key: value
                        for key, value in summary.get("coverage", {}).items()
                        if key != "eligible_task_ids"
                    },
                }
            )
            if not report or workflow.get("stage") != "complete":
                return 2
        elif args.action == "build":
            output = _destination(args.output, "benchmark.json")
            preparation_path = output.with_suffix(".preparation.json")
            calls = (
                read_json(preparation_path).get("calls", [])
                if args.resume and preparation_path.exists()
                else []
            )
            preparation_deadline = time.monotonic() + args.max_preparation_seconds
            compiler_config = read_json(Path(args.compiler)) if args.compiler else None
            compiler = (
                model_callback(
                    compiler_config,
                    allow_unsafe_local=args.allow_unsafe_local,
                    calls=calls,
                    deadline=preparation_deadline,
                )
                if args.compiler
                else None
            )
            output = _destination(args.output, "benchmark.json")
            if args.session:
                raw_path = Path(args.session).expanduser()
                session = _normalize(read_session_bytes(raw_path))
                write_json(output.with_suffix(".normalized.json"), session)
                if raw_path.name.endswith((".jsonl", ".jsonl.zst")):
                    session["evidence"] = extract_evidence(raw_path)
                benchmark = build_benchmark(session, name=args.name, compiler=compiler)
            else:
                inventory_path = output.with_suffix(".inventory.json")
                inventory = (
                    read_json(inventory_path)
                    if args.resume and inventory_path.exists()
                    else _discover(args, output.parent / (output.stem + ".sources"))
                )
                write_json(inventory_path, inventory)

                def prepare_session(session, path):
                    session["evidence"] = extract_evidence(path)
                    session["history_window"] = {k: inventory[k] for k in ("since", "as_of")}
                    return session

                def checkpoint(entry):
                    write_json(inventory_path, inventory)
                    if calls:
                        write_json(output.with_suffix(".preparation.json"), {"calls": calls})
                    print(
                        json.dumps({"session": entry["path"], "status": entry["status"]}),
                        file=sys.stderr,
                    )

                benchmark = build_history(
                    inventory,
                    name=args.name,
                    compiler=compiler,
                    progress=checkpoint,
                    artifact_dir=output.parent / (output.stem + ".sessions"),
                    resume=args.resume,
                    workers=args.workers,
                    prepare_session=prepare_session,
                    compiler_identity=_preparation_identity(compiler_config)
                    if args.compiler
                    else None,
                )
            if compiler:
                # Corpus-wide curation is mandatory for newly compiled datasets,
                # including explicitly selected multi-goal sessions.
                from .workflow import _assessor_config

                curator_config = _assessor_config(
                    compiler_config,
                    read_json(Path(args.curator)) if args.curator else None,
                    "curator",
                )
                write_json(output.with_suffix(".uncurated.json"), benchmark)
                try:
                    benchmark = curate_benchmark(
                        benchmark,
                        curator=model_callback(
                            curator_config,
                            allow_unsafe_local=args.allow_unsafe_local,
                            calls=calls,
                            deadline=preparation_deadline,
                        ),
                        artifact_dir=output.parent / (output.stem + ".curation"),
                        curator_identity=curator_config,
                        resume=args.resume,
                        progress=lambda event: print(
                            json.dumps({"stage": "curation", **event}), file=sys.stderr
                        ),
                    )
                except CurationError as exc:
                    write_json(output.with_suffix(".curation-error.json"), exc.as_dict())
                    raise
                finally:
                    write_json(output.with_suffix(".preparation.json"), {"calls": calls})
            write_json(output, benchmark)
            if calls:
                write_json(output.with_suffix(".preparation.json"), {"calls": calls})
            _json(
                {
                    "output": str(output),
                    "tasks": len(benchmark["tasks"]),
                    "ready": sum(t.get("readiness") == "ready" for t in benchmark["tasks"]),
                    "preparation": "model_proposed" if compiler else "fallback_draft",
                    "curation": {
                        key: value
                        for key, value in benchmark.get("history", {})
                        .get("semantic_curation", {})
                        .items()
                        if key in {"original_occurrences", "family_count", "duplicates_removed"}
                    },
                    **(
                        {
                            "history": {
                                "days": inventory["days"],
                                "as_of": inventory["as_of"],
                                "sessions": dict(
                                    Counter(row["status"] for row in inventory["sessions"])
                                ),
                                "inventory": str(inventory_path),
                            }
                        }
                        if not args.session
                        else {}
                    ),
                }
            )
        elif args.action == "validate":
            benchmark = validate_benchmark(read_json(Path(args.benchmark)))
            _json({"valid": True, "tasks": len(benchmark["tasks"])})
        elif args.action in {"run", "grade-recorded"}:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S-%fZ")
            output = _destination(args.output, f"runs/{stamp}")
            config = (
                read_json(Path(args.config))
                if args.action == "run"
                else {"name": "Recorded work", "harness": "fixture", "model": "historical"}
            )
            report = evaluate(
                read_json(Path(args.benchmark)),
                config,
                output,
                judge_config=read_json(Path(args.judge)) if args.judge else None,
                repeats=getattr(args, "repeats", 1),
                baseline_type="fresh" if args.action == "run" else "recorded",
                resume=args.resume,
                workers=args.workers,
                allow_unsafe_local=args.allow_unsafe_local,
                max_total_seconds=args.max_total_seconds,
                stop_after_cost_usd=args.stop_after_cost_usd,
            )
            _json(
                {
                    "report": str(output / "report.html"),
                    "run_status": report["run_status"],
                    "success_pct": report["summary"]["success_pct"],
                    "output_quality_pct": report["summary"]["output_quality_pct"],
                    "efficiency_pct": report["summary"]["efficiency_pct"],
                    "observed_success_pct": report["summary"].get("observed_success_pct"),
                    "observed_quality_pct": report["summary"].get("observed_quality_pct"),
                    "coverage": report["summary"]["coverage"],
                }
            )
            rows = {row["task_id"]: row for row in report["summary"]["tasks"]}
            required = {
                task["id"]
                for task in report["task_manifest"]
                if args.action == "grade-recorded" or task.get("readiness") in {None, "ready"}
            }
            if report["run_status"] != "complete" or any(
                not rows.get(tid, {}).get("complete", False) for tid in required
            ):
                return 2
        elif args.action == "compare":
            result = compare_reports([read_json(Path(p)) for p in args.reports])
            output = _destination(args.output, "comparison.json")
            write_json(output, result)
            _json({"output": str(output), **result})
        elif args.action == "report":
            output = _destination(args.output, "report.html")
            write_private(output, render_report(read_json(Path(args.report))))
            _json({"output": str(output)})
        elif args.action == "capture":
            if args.capture_action == "start":
                output = _destination(args.output, "capture")
                manifest = capture_start(
                    Path(args.source).expanduser() if args.source else None, output
                )
                _json({"capture": str(output), "files": len(manifest.get("files", {}))})
            else:
                session = capture_finish(Path(args.capture), Path(args.session))
                output = _destination(args.output, "captured-session.json")
                write_json(output, session)
                _json({"output": str(output), "messages": len(session["messages"])})
        elif args.action == "watch":
            output = _destination(args.output, "watched-session.json")
            if args.capture:
                capture = Path(args.capture)
            else:
                capture = output.parent / (output.stem + "-capture")
                if not capture.exists():
                    capture_start(None, capture)
            updates = 0
            for session in capture_watch(
                capture,
                Path(args.session),
                interval_seconds=args.interval,
                duration_seconds=args.duration,
            ):
                write_json(output, session)
                updates += 1
            _json(
                {
                    "output": str(output),
                    "updates": updates,
                    "note": "File updates do not establish that a goal has finished",
                }
            )
        elif args.action == "scan":
            issues = scan_paths(Path(args.path), git=args.git)
            _json(
                {
                    "findings": issues,
                    "note": "Pattern scanning cannot certify absence of sensitive information",
                }
            )
            return 1 if issues else 0
        elif args.action == "doctor":
            _json(
                {
                    "version": __version__,
                    "python": sys.version.split()[0],
                    "executables": {
                        name: bool(shutil.which(name))
                        for name in ("codex", "claude", "git", "zstd")
                    },
                    "private_data_home": str(DEFAULT_HOME),
                    "native_execution": "Requires --allow-unsafe-local for trusted tasks",
                    "provider_access": "Not probed; configure and authenticate your chosen harness",
                }
            )
        elif args.action == "sessions":
            _json(_discover(args))
        return 0
    except KeyboardInterrupt:
        print(
            "Interrupted; completed trials were preserved. Use --resume to continue.",
            file=sys.stderr,
        )
        return 130
    except (ValueError, OSError) as exc:
        print(f"personalbench: {exc}", file=sys.stderr)
        return 2
