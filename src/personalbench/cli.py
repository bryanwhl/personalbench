"""Local-first command line interface. Generated data never belongs in the public repo."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .capture import capture_finish, capture_start, capture_watch
from .engine import evaluate, model_callback
from .imports import build_benchmark, import_session
from .privacy import DEFAULT_HOME, read_json, scan_paths, write_json, write_private
from .report import render_report
from .schema import validate_benchmark
from .scoring import compare_reports


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


def parser():
    root = argparse.ArgumentParser(prog="personalbench", description="Your work. Your benchmark.")
    root.add_argument("--version", action="version", version=__version__)
    sub = root.add_subparsers(dest="action", required=True)
    imp = sub.add_parser("import", help="Normalize a selected local session")
    imp.add_argument("session")
    imp.add_argument("--format", choices=["auto", "codex", "claude", "normalized"], default="auto")
    imp.add_argument("--output")
    build = sub.add_parser("build", help="Compile goals and criteria into a benchmark")
    build.add_argument("session")
    build.add_argument("--output")
    build.add_argument("--name", default="Personal benchmark")
    build.add_argument(
        "--compiler", help="Optional model configuration for autonomous goal compilation"
    )
    build.add_argument("--allow-unsafe-local", action="store_true")
    val = sub.add_parser("validate", help="Validate a benchmark without running models")
    val.add_argument("benchmark")
    for name in ("run", "grade-recorded"):
        run = sub.add_parser(
            name, help="Execute fresh attempts" if name == "run" else "Grade recorded work"
        )
        run.add_argument("benchmark")
        run.add_argument("--output")
        run.add_argument("--resume", action="store_true")
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
    sessions.add_argument("--source", choices=["codex", "claude"], default="codex")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.action == "import":
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
        elif args.action == "build":
            session = import_session(Path(args.session).expanduser())
            calls = []
            compiler = (
                model_callback(
                    read_json(Path(args.compiler)),
                    allow_unsafe_local=args.allow_unsafe_local,
                    calls=calls,
                )
                if args.compiler
                else None
            )
            benchmark = build_benchmark(session, name=args.name, compiler=compiler)
            output = _destination(args.output, "benchmark.json")
            write_json(output, benchmark)
            if calls:
                write_json(output.with_suffix(".preparation.json"), {"calls": calls})
            _json(
                {
                    "output": str(output),
                    "tasks": len(benchmark["tasks"]),
                    "ready": sum(t.get("readiness") == "ready" for t in benchmark["tasks"]),
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
                    "coverage": report["summary"]["coverage"],
                }
            )
            if report["summary"]["coverage"]["complete_tasks"] < len(
                read_json(Path(args.benchmark))["tasks"]
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
                        name: bool(shutil.which(name)) for name in ("codex", "claude", "git")
                    },
                    "private_data_home": str(DEFAULT_HOME),
                    "native_execution": "Requires --allow-unsafe-local for trusted tasks",
                    "provider_access": "Not probed; configure and authenticate your chosen harness",
                }
            )
        elif args.action == "sessions":
            source = (
                Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
                if args.source == "codex"
                else Path.home() / ".claude"
            )
            roots = (
                [source / "sessions", source / "archived_sessions"]
                if args.source == "codex"
                else [source / "projects"]
            )
            paths = sorted(
                (p for r in roots for p in r.rglob("*.jsonl")),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            _json(
                {
                    "source": args.source,
                    "sessions": [{"path": str(p), "bytes": p.stat().st_size} for p in paths],
                }
            )
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
