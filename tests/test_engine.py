import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from threading import Lock
from unittest.mock import patch

from personalbench.engine import evaluate
from personalbench.runners import run_candidate


def benchmark(ids=("a",)):
    return {
        "schema_version": 1,
        "name": "Synthetic evaluation",
        "tasks": [
            {
                "id": tid,
                "category": "research" if tid == "b" else "coding",
                "brief": "Return the word done",
                "inputs": {"context.txt": "Synthetic context"},
                "criteria": [
                    {
                        "id": "check",
                        "description": "Returns done",
                        "kind": "contains",
                        "value": "done",
                    }
                ],
                "reference": {"time_seconds": 1, "user_prompts": 1, "cost_usd": 1},
                "historical": {
                    "output": "PRIVATE_HISTORICAL_ANSWER",
                    "duration_seconds": 1,
                    "user_prompts": 1,
                    "cost_usd": 1,
                },
            }
            for tid in ids
        ],
    }


def fixture(ids=("a",)):
    return {
        "name": "Synthetic fixture",
        "harness": "fixture",
        "model": "synthetic",
        "responses": {tid: "done" for tid in ids},
        "cost_usd": 0.6,
    }


class ManualClock:
    """Advance evaluation time at defined phase boundaries, independent of CI load."""

    def __init__(self):
        self.value = 100.0
        self.lock = Lock()

    def __call__(self):
        with self.lock:
            return self.value

    def advance(self, seconds):
        with self.lock:
            self.value += seconds


class EngineTests(unittest.TestCase):
    def test_native_boundary_identity_prevents_resuming_unconfined_evidence(self):
        config = {
            "name": "Native identity probe",
            "harness": "codex",
            "model": "synthetic",
            "tools": "workspace",
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            answer = run_candidate(benchmark()["tasks"][0], fixture(), output)
            with (
                patch("personalbench.engine.subprocess.run") as version,
                patch("personalbench.engine.run_candidate", return_value=answer) as candidate,
            ):
                version.return_value.stdout = "synthetic-cli-version"
                first = evaluate(benchmark(), config, output, allow_unsafe_local=True)
                self.assertEqual(first["runtime"]["input_boundary"], "workspace-minimal-read-v1")
                self.assertEqual(len(first["runtime"]["implementation_hash"]), 64)
                self.assertEqual(candidate.call_count, 1)
                candidate.reset_mock()
                with (
                    patch(
                        "personalbench.engine.adapter_identity",
                        return_value={
                            "adapter_version": "legacy",
                            "input_boundary": "unrestricted",
                        },
                    ),
                    self.assertRaisesRegex(ValueError, "Cannot resume"),
                ):
                    evaluate(benchmark(), config, output, resume=True, allow_unsafe_local=True)
                candidate.assert_not_called()

    def test_corpus_inventory_survives_evaluation_and_report(self):
        bench = benchmark()
        bench["history"] = {
            "days": 60,
            "sessions": [{"path": "missing.jsonl", "status": "error"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            result = evaluate(bench, fixture(), output)
            self.assertEqual(result["history"], bench["history"])
            self.assertIn("Source coverage", (output / "report.html").read_text())
            self.assertIn("error: 1", (output / "report.html").read_text())

    def test_resume_skips_completed_attempts_and_rejects_changed_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve() / "run"
            config = fixture()
            first = evaluate(benchmark(), config, output, repeats=2)
            with patch(
                "personalbench.engine.run_candidate",
                side_effect=AssertionError("Repeated a completed trial"),
            ):
                resumed = evaluate(benchmark(), config, output, repeats=2, resume=True)
            self.assertEqual(resumed["trials"], first["trials"])
            changed = dict(config, model="different-model")
            with self.assertRaisesRegex(ValueError, "Cannot resume"):
                evaluate(benchmark(), changed, output, repeats=2, resume=True)
            with self.assertRaisesRegex(ValueError, "Cannot resume"):
                evaluate(benchmark(), config, output, repeats=1, resume=True)
            with self.assertRaisesRegex(ValueError, "already contains"):
                evaluate(benchmark(), config, output, repeats=2)

    def test_budget_stop_preserves_unknown_evidence_and_resumes_remaining(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve() / "run"
            config = fixture(("a", "b"))
            bench = benchmark(("a", "b"))
            stopped = evaluate(bench, config, output, stop_after_cost_usd=0.5)
            self.assertEqual(stopped["run_status"], "budget_stopped")
            self.assertEqual(len(stopped["trials"]), 1)
            self.assertIsNone(stopped["summary"]["success_pct"])
            self.assertEqual(stopped["summary"]["coverage"]["expected_trials"], 2)
            with patch("personalbench.engine.run_candidate", wraps=run_candidate) as runner:
                completed = evaluate(bench, config, output, resume=True)
            self.assertEqual(runner.call_count, 1)
            self.assertEqual(completed["run_status"], "complete")
            self.assertEqual(completed["summary"]["success_pct"], 100)

    def test_interrupt_checkpoint_is_resumable_and_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve() / "run"
            config = fixture(("a", "b"))
            bench = benchmark(("a", "b"))
            first = run_candidate(bench["tasks"][0], config, output)
            with (
                patch("personalbench.engine.run_candidate", side_effect=[first, KeyboardInterrupt]),
                self.assertRaises(KeyboardInterrupt),
            ):
                evaluate(bench, config, output)
            saved = json.loads((output / "report.json").read_text())
            self.assertEqual(saved["run_status"], "interrupted")
            self.assertEqual(len(saved["trials"]), 1)
            self.assertIsNone(saved["summary"]["output_quality_pct"])
            self.assertIn("Unavailable", (output / "report.html").read_text())
            resumed = evaluate(bench, config, output, resume=True)
            self.assertEqual(len(resumed["trials"]), 2)

    def test_candidate_payload_omits_history_and_criteria(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            observed = root / "candidate-input.json"
            script = (
                "import json,sys;from pathlib import Path;data=sys.stdin.read();"
                "Path(sys.argv[1]).write_text(data);"
                "print(json.dumps({'output':'done','usage':{},'cost_usd':0.01}))"
            )
            config = {
                "name": "Synthetic process",
                "harness": "command",
                "model": "synthetic",
                "command": [sys.executable, "-c", script, str(observed)],
            }
            bench = benchmark()
            bench["tasks"][0]["criteria"][0]["description"] = "PRIVATE_GRADER_REQUIREMENT"
            report = evaluate(bench, config, root / "run", allow_unsafe_local=True)
            sent = observed.read_text()
            payload = json.loads(sent)
            self.assertEqual(set(payload["task"]), {"id", "brief", "inputs"})
            self.assertNotIn("PRIVATE_HISTORICAL_ANSWER", sent)
            self.assertNotIn("PRIVATE_GRADER_REQUIREMENT", sent)
            self.assertEqual(report["trials"][0]["cost_kind"], "reported")
            self.assertIn("Fresh run", (root / "run" / "report.html").read_text())
            self.assertNotIn("Synthetic fixture run", (root / "run" / "report.html").read_text())

    def test_symlink_artifact_cannot_pass_file_grader(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            external = root / "outside.txt"
            external.write_text("expected")
            script = (
                "import json,sys;from pathlib import Path;p=json.load(sys.stdin);"
                "Path(p['workspace'],'answer.txt').symlink_to(sys.argv[1]);"
                "print(json.dumps({'output':'done','usage':{}}))"
            )
            config = {
                "name": "Synthetic process",
                "harness": "command",
                "model": "synthetic",
                "command": [sys.executable, "-c", script, str(external)],
            }
            bench = benchmark()
            bench["tasks"][0]["criteria"] = [
                {
                    "id": "file",
                    "description": "Artifact is correct",
                    "kind": "file_contains",
                    "target": "answer.txt",
                    "value": "expected",
                }
            ]
            result = evaluate(bench, config, root / "run", allow_unsafe_local=True)
            self.assertEqual(result["trials"][0]["grading"]["status"], "grading_error")
            self.assertIsNone(result["summary"]["success_pct"])
            self.assertNotIn("artifacts", result["trials"][0])

    def test_timeout_is_failed_task_not_unknown_and_fixture_is_labeled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = {
                "name": "Slow synthetic process",
                "harness": "command",
                "model": "synthetic",
                "timeout_seconds": 0.1,
                "command": [sys.executable, "-c", "import time;time.sleep(10)"],
            }
            result = evaluate(benchmark(), config, root / "timeout", allow_unsafe_local=True)
            self.assertEqual(result["trials"][0]["status"], "budget_exceeded")
            self.assertEqual(result["summary"]["success_pct"], 0)
            self.assertEqual(result["summary"]["efficiency_pct"], 0)
            evaluate(benchmark(), fixture(), root / "fixture")
            self.assertIn("Synthetic fixture run", (root / "fixture" / "report.html").read_text())

    def test_incomplete_inputs_do_not_call_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            bench = copy.deepcopy(benchmark())
            bench["tasks"][0]["readiness"] = "incomplete"
            with patch(
                "personalbench.engine.run_candidate", side_effect=AssertionError("Should not run")
            ):
                result = evaluate(bench, fixture(), Path(directory).resolve() / "run")
            self.assertEqual(result["trials"][0]["status"], "preparation_error")
            self.assertIsNone(result["summary"]["success_pct"])


class BudgetRegressionTests(unittest.TestCase):
    def test_judge_spend_stops_next_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve() / "run"
            bench = benchmark()
            bench["tasks"][0]["criteria"][0].update(kind="judge")
            judge_config = dict(fixture(), name="Synthetic judge")

            def callback(config, *, calls, **kwargs):
                def judge(prompt):
                    calls.append({"status": "completed", "cost_usd": 0.9})
                    return '{"scores":[{"id":"check","score":1,"evidence":"done"}]}'

                return judge

            with patch("personalbench.engine.model_callback", side_effect=callback):
                result = evaluate(
                    bench,
                    dict(fixture(), cost_usd=0.01),
                    output,
                    repeats=2,
                    judge_config=judge_config,
                    allow_unsafe_local=True,
                    stop_after_cost_usd=0.5,
                )
            self.assertEqual(len(result["trials"]), 1)
            self.assertEqual(result["run_status"], "budget_stopped")

    def test_resume_respects_previously_consumed_time(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve() / "run"
            bench, config = benchmark(("a", "b")), fixture(("a", "b"))
            evaluate(bench, config, output, stop_after_cost_usd=0.5)
            report_path = output / "report.json"
            saved = json.loads(report_path.read_text())
            saved["elapsed_seconds"] = 5
            report_path.write_text(json.dumps(saved))
            with patch(
                "personalbench.engine.run_candidate", side_effect=AssertionError("budget spent")
            ):
                result = evaluate(bench, config, output, resume=True, max_total_seconds=5)
            self.assertEqual(result["run_status"], "budget_stopped")
            self.assertEqual(len(result["trials"]), 1)

    def test_tiny_total_budget_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            result = evaluate(
                benchmark(), fixture(), Path(directory).resolve() / "run", max_total_seconds=0.05
            )
            self.assertEqual(result["run_status"], "budget_stopped")
            self.assertEqual(result["trials"], [])


class ParallelEngineTests(unittest.TestCase):
    def test_workers_overlap_checkpoint_completions_and_keep_manifest_order(self):
        import threading

        barrier = threading.Barrier(2)
        b_checkpoint = threading.Event()
        lock = threading.Lock()
        active = 0
        maximum = 0
        snapshots = []
        from personalbench.privacy import write_json as save_json

        def checkpoint(path, value):
            snapshots.append([row["task_id"] for row in value["trials"]])
            if "b" in snapshots[-1]:
                b_checkpoint.set()
            save_json(path, value)

        def runner(task, config, workspace, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                if task["id"] in ("a", "b"):
                    barrier.wait(timeout=2)
                if task["id"] == "a" and not b_checkpoint.wait(timeout=5):
                    raise AssertionError("Completed b was not checkpointed while a was pending")
                return run_candidate(task, config, workspace, **kwargs)
            finally:
                with lock:
                    active -= 1

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("personalbench.engine.run_candidate", side_effect=runner),
            patch("personalbench.engine.write_json", side_effect=checkpoint),
        ):
            result = evaluate(
                benchmark(("a", "b", "c")),
                fixture(("a", "b", "c")),
                Path(directory) / "run",
                workers=2,
            )
        self.assertEqual(maximum, 2)
        self.assertEqual([row["task_id"] for row in result["trials"]], ["a", "b", "c"])
        self.assertIn(["b"], snapshots)
        self.assertEqual(result["summary"]["success_pct"], 100)

    def test_parallel_spend_stops_scheduling_after_the_inflight_wave(self):
        import threading

        barrier = threading.Barrier(2)
        called = []

        def runner(task, config, workspace, **kwargs):
            called.append(task["id"])
            barrier.wait(timeout=2)
            return run_candidate(task, config, workspace, **kwargs)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("personalbench.engine.run_candidate", side_effect=runner),
        ):
            result = evaluate(
                benchmark(("a", "b", "c")),
                fixture(("a", "b", "c")),
                Path(directory) / "run",
                workers=2,
                stop_after_cost_usd=0.5,
            )
        self.assertEqual(set(called), {"a", "b"})
        self.assertEqual(result["run_status"], "budget_stopped")
        self.assertEqual(len(result["trials"]), 2)
        self.assertIsNone(result["summary"]["success_pct"])

    def test_parallel_judges_have_independent_call_logs(self):
        import threading

        logs = []
        barrier = threading.Barrier(2)

        def callback(config, *, calls, **kwargs):
            logs.append(calls)

            def judge(prompt):
                barrier.wait(timeout=2)
                calls.append({"status": "completed", "cost_usd": 0.1})
                return '{"scores":[{"id":"check","score":1,"evidence":"Synthetic check passed"}]}'

            return judge

        bench = benchmark(("a", "b"))
        for task in bench["tasks"]:
            task["criteria"][0]["kind"] = "judge"
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("personalbench.engine.model_callback", side_effect=callback),
        ):
            result = evaluate(
                bench,
                fixture(("a", "b")),
                Path(directory) / "run",
                workers=2,
                judge_config=fixture(),
                allow_unsafe_local=True,
            )
        self.assertEqual(len(logs), 2)
        self.assertIsNot(logs[0], logs[1])
        self.assertEqual([row["task_id"] for row in result["judge_calls"]], ["a", "b"])
        self.assertEqual(result["summary"]["success_pct"], 100)

    def test_worker_failure_is_preserved_without_losing_other_tasks(self):
        def runner(task, config, workspace, **kwargs):
            if task["id"] == "b":
                raise RuntimeError("Synthetic worker failure")
            return run_candidate(task, config, workspace, **kwargs)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("personalbench.engine.run_candidate", side_effect=runner),
        ):
            result = evaluate(
                benchmark(("a", "b")),
                fixture(("a", "b")),
                Path(directory) / "run",
                workers=2,
            )
        self.assertEqual(result["trials"][0]["status"], "completed")
        self.assertEqual(result["trials"][1]["status"], "infrastructure_error")
        self.assertIn("Synthetic worker failure", result["trials"][1]["error"])
        self.assertIsNone(result["trials"][1]["grading"]["success"])

    def test_resume_rejects_worker_changes_and_accepts_legacy_serial_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "parallel"
            evaluate(benchmark(), fixture(), output, workers=2)
            with self.assertRaisesRegex(ValueError, "Cannot resume"):
                evaluate(benchmark(), fixture(), output, workers=1, resume=True)
            with patch(
                "personalbench.engine.run_candidate", side_effect=AssertionError("Duplicate")
            ):
                evaluate(benchmark(), fixture(), output, workers=2, resume=True)
            serial = Path(directory) / "serial"
            evaluate(benchmark(), fixture(), serial)
            path = serial / "report.json"
            old = json.loads(path.read_text())
            del old["execution"]
            path.write_text(json.dumps(old))
            evaluate(benchmark(), fixture(), serial, resume=True)

    def test_parallel_interrupt_reaps_native_children_and_resumes_only_unfinished(self):
        import os
        import signal
        import subprocess
        import time

        def running(pid):
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            return bool(state) and not state.startswith("Z")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.py"
            candidate.write_text(
                "import json,sys,subprocess,time\nfrom pathlib import Path\n"
                'request=json.load(sys.stdin)\nroot=Path(sys.argv[1])\ntid=request["task"]["id"]\n'
                'with (root/"attempts").open("a") as log: log.write(tid+"\\n")\n'
                'if tid!="a" and not (root/"release").exists():\n'
                ' child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"])\n'
                ' (root/(tid+".pid")).write_text(json.dumps([__import__("os").getpid(),child.pid]))\n'
                " time.sleep(30)\n"
                'print(json.dumps({"output":"done","usage":{}}))\n'
            )
            config = {
                "name": "Synthetic interrupt process",
                "harness": "command",
                "model": "synthetic",
                "command": [sys.executable, str(candidate), str(root)],
                "timeout_seconds": 30,
            }
            (root / "benchmark.json").write_text(json.dumps(benchmark(("a", "b", "c"))))
            (root / "config.json").write_text(json.dumps(config))
            controller = root / "evaluate.py"
            controller.write_text(
                "import sys,json\nfrom pathlib import Path\nfrom personalbench.engine import evaluate\n"
                "root=Path(sys.argv[1])\n"
                'evaluate(json.loads((root/"benchmark.json").read_text()),'
                'json.loads((root/"config.json").read_text()),root/"run",workers=2,'
                "allow_unsafe_local=True,resume=len(sys.argv)>2)\n"
            )
            process = subprocess.Popen(
                [sys.executable, str(controller), str(root)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            pids = []
            try:
                deadline = time.monotonic() + 5
                while not all((root / (tid + ".pid")).exists() for tid in ("b", "c")):
                    if time.monotonic() > deadline:
                        self.fail("Parallel synthetic candidates did not start")
                    time.sleep(0.01)
                pids = [
                    pid
                    for tid in ("b", "c")
                    for pid in json.loads((root / (tid + ".pid")).read_text())
                ]
                process.send_signal(signal.SIGINT)
                process.communicate(timeout=5)
                saved = json.loads((root / "run/report.json").read_text())
                self.assertEqual(saved["run_status"], "interrupted")
                self.assertEqual([row["task_id"] for row in saved["trials"]], ["a"])
                deadline = time.monotonic() + 2
                while any(running(pid) for pid in pids) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(
                    any(running(pid) for pid in pids), "Interrupted process group survived"
                )
                (root / "release").touch()
                subprocess.run(
                    [sys.executable, str(controller), str(root), "resume"],
                    capture_output=True,
                    check=True,
                    timeout=5,
                )
                resumed = json.loads((root / "run/report.json").read_text())
                self.assertEqual(resumed["run_status"], "complete")
                self.assertEqual(len(resumed["trials"]), 3)
                self.assertEqual((root / "attempts").read_text().splitlines().count("a"), 1)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
                for pid in pids:
                    if running(pid):
                        os.kill(pid, signal.SIGKILL)

    def test_grader_command_cancellation_reaps_its_process_group(self):
        import os
        import signal
        import subprocess
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor

        from personalbench.grading import grade
        from personalbench.runners import execution_cancellation

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pidfile = root / "grader.pid"
            code = (
                "import subprocess,sys,time\nfrom pathlib import Path\n"
                'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"])\n'
                f"Path({str(pidfile)!r}).write_text(str(child.pid))\n"
                "time.sleep(30)\n"
            )
            task = {
                "criteria": [
                    {
                        "id": "check",
                        "kind": "command",
                        "value": [sys.executable, "check.py"],
                        "tests": {"check.py": code},
                        "timeout_seconds": 30,
                    }
                ]
            }
            cancelled = threading.Event()

            def assess():
                with execution_cancellation(cancelled):
                    return grade(task, {"output": "done"}, root, allow_unsafe_local=True)

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(assess)
                try:
                    deadline = time.monotonic() + 3
                    while not pidfile.exists():
                        if time.monotonic() > deadline:
                            self.fail("Synthetic command grader did not start")
                        time.sleep(0.01)
                    child = int(pidfile.read_text())
                    cancelled.set()
                    with self.assertRaises(KeyboardInterrupt):
                        future.result(timeout=3)
                    state = subprocess.run(
                        ["ps", "-o", "stat=", "-p", str(child)],
                        capture_output=True,
                        text=True,
                        check=False,
                    ).stdout.strip()
                    self.assertTrue(not state or state.startswith("Z"))
                finally:
                    cancelled.set()
                    if pidfile.exists():
                        child = int(pidfile.read_text())
                        try:
                            os.kill(child, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_interrupted_output_assessment_resumes_without_rerunning_candidate(self):
        bench = benchmark()
        bench["tasks"][0]["criteria"][0]["kind"] = "judge"

        def interrupted(config, *, calls, **kwargs):
            def assess(prompt):
                calls.append({"status": "completed", "cost_usd": 0.1})
                raise KeyboardInterrupt

            return assess

        def completed(config, *, calls, **kwargs):
            def assess(prompt):
                calls.append({"status": "completed", "cost_usd": 0.2})
                return '{"scores":[{"id":"check","score":1,"evidence":"done"}]}'

            return assess

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            with (
                patch("personalbench.engine.model_callback", side_effect=interrupted),
                self.assertRaises(KeyboardInterrupt),
            ):
                evaluate(bench, fixture(), output, judge_config=fixture(), allow_unsafe_local=True)
            saved = json.loads((output / "report.json").read_text())
            self.assertTrue(saved["trials"][0]["assessment_interrupted"])
            with (
                patch("personalbench.engine.model_callback", side_effect=completed),
                patch(
                    "personalbench.engine.run_candidate",
                    side_effect=AssertionError("Candidate reran"),
                ),
            ):
                result = evaluate(
                    bench,
                    fixture(),
                    output,
                    judge_config=fixture(),
                    allow_unsafe_local=True,
                    resume=True,
                )
            self.assertEqual(len(result["trials"]), 1)
            self.assertEqual(result["summary"]["success_pct"], 100)
            self.assertEqual([row["cost_usd"] for row in result["judge_calls"]], [0.1, 0.2])
            self.assertEqual(result["trials"][0]["cost_usd"], 0.6)
            self.assertNotIn("assessment_interrupted", result["trials"][0])

    def test_interrupted_file_assessment_restores_captured_candidate_artifact(self):
        bench = benchmark()
        bench["tasks"][0]["criteria"] = [
            {
                "id": "file",
                "kind": "file_contains",
                "description": "Expected file",
                "target": "answer.txt",
                "value": "candidate answer",
            },
        ]

        def candidate(task, config, workspace, **kwargs):
            (workspace / "answer.txt").write_text("candidate answer")
            return run_candidate(task, config, workspace, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            with (
                patch("personalbench.engine.run_candidate", side_effect=candidate),
                patch("personalbench.engine.grade", side_effect=KeyboardInterrupt),
                self.assertRaises(KeyboardInterrupt),
            ):
                evaluate(bench, fixture(), output)
            saved = json.loads((output / "report.json").read_text())
            self.assertEqual(saved["trials"][0]["artifacts"], {"answer.txt": "candidate answer"})
            with patch(
                "personalbench.engine.run_candidate", side_effect=AssertionError("Candidate reran")
            ):
                resumed = evaluate(bench, fixture(), output, resume=True)
            self.assertEqual(resumed["summary"]["success_pct"], 100)

    def test_recorded_historical_cost_does_not_consume_new_judging_budget(self):
        bench = benchmark(("a", "b"))
        for task in bench["tasks"]:
            task["criteria"][0]["kind"] = "judge"
            task["historical"]["cost_usd"] = 100

        def callback(config, *, calls, **kwargs):
            def assess(prompt):
                calls.append({"status": "completed", "cost_usd": 0.1})
                return '{"scores":[{"id":"check","score":1,"evidence":"done"}]}'

            return assess

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("personalbench.engine.model_callback", side_effect=callback),
        ):
            result = evaluate(
                bench,
                fixture(("a", "b")),
                Path(directory) / "run",
                baseline_type="recorded",
                judge_config=fixture(),
                allow_unsafe_local=True,
                stop_after_cost_usd=0.5,
            )
        self.assertEqual(result["run_status"], "complete")
        self.assertEqual(len(result["trials"]), 2)

    def test_shortened_final_attempt_reports_batch_budget_without_false_failure(self):
        clock = ManualClock()
        config = {
            "name": "Slow synthetic",
            "harness": "command",
            "model": "synthetic",
            "timeout_seconds": 10,
            "command": [sys.executable, "-c", "unused synthetic command"],
        }

        def timeout(task, bounded, workspace, **kwargs):
            self.assertAlmostEqual(bounded["timeout_seconds"], 0.2)
            clock.advance(bounded["timeout_seconds"] + 0.01)
            return {
                "status": "budget_exceeded",
                "output": "",
                "duration_seconds": 0.21,
                "user_prompts": 1,
                "usage": {},
                "cost_usd": None,
                "error": "Synthetic timeout",
            }

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("personalbench.engine.time.monotonic", clock),
            patch("personalbench.engine.run_candidate", side_effect=timeout) as invoked,
        ):
            result = evaluate(
                benchmark(),
                config,
                Path(directory) / "run",
                allow_unsafe_local=True,
                max_total_seconds=0.2,
            )
        self.assertEqual(invoked.call_count, 1)
        self.assertEqual(result["run_status"], "budget_stopped")
        self.assertEqual(result["trials"][0]["status"], "batch_budget_exceeded")
        self.assertIsNone(result["summary"]["success_pct"])

    def test_explicit_resume_retries_only_batch_truncation_and_keeps_overhead(self):
        clock = ManualClock()
        config = {
            "name": "Synthetic resumable process",
            "harness": "command",
            "model": "synthetic",
            "timeout_seconds": 10,
            "command": [sys.executable, "-c", "unused synthetic command"],
        }

        def attempt(task, bounded, workspace, **kwargs):
            limited = bounded["timeout_seconds"] < 0.3
            duration = bounded["timeout_seconds"] + 0.01 if limited else 0.3
            clock.advance(duration)
            return {
                "status": "budget_exceeded" if limited else "completed",
                "output": "" if limited else "done",
                "duration_seconds": duration,
                "user_prompts": 1,
                "usage": {},
                "cost_usd": None if limited else 0.1,
                "cost_kind": "unknown" if limited else "synthetic",
                "error": None,
            }

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("personalbench.engine.time.monotonic", clock),
        ):
            output = Path(directory) / "run"
            with patch("personalbench.engine.run_candidate", side_effect=attempt) as invoked:
                first = evaluate(
                    benchmark(), config, output, allow_unsafe_local=True, max_total_seconds=0.2
                )
                self.assertEqual(first["trials"][0]["status"], "batch_budget_exceeded")
                second = evaluate(
                    benchmark(),
                    config,
                    output,
                    allow_unsafe_local=True,
                    max_total_seconds=2,
                    resume=True,
                )
            self.assertEqual(invoked.call_count, 2)
            self.assertEqual(second["run_status"], "complete")
            self.assertEqual(second["summary"]["success_pct"], 100)
            self.assertEqual(len(second["trials"]), 1)
            self.assertEqual(len(second["retry_history"]), 1)
            self.assertEqual(second["trials"][0]["user_prompts"], 2)
            self.assertGreaterEqual(second["trials"][0]["duration_seconds"], 0.5)
            self.assertIsNone(second["trials"][0]["cost_usd"])
            self.assertEqual(second["trials"][0]["latest_attempt"]["cost_usd"], 0.1)
            with patch(
                "personalbench.engine.run_candidate",
                side_effect=AssertionError("Duplicate completed candidate"),
            ) as replay:
                evaluate(
                    benchmark(),
                    config,
                    output,
                    allow_unsafe_local=True,
                    max_total_seconds=2,
                    resume=True,
                )
            self.assertEqual(replay.call_count, 0)
            # A full declared task timeout remains a scored failure, never a retry target.
            failed = Path(directory) / "full-timeout"
            short = dict(config, timeout_seconds=0.1)
            with patch("personalbench.engine.run_candidate", side_effect=attempt):
                result = evaluate(
                    benchmark(), short, failed, allow_unsafe_local=True, max_total_seconds=2
                )
            self.assertEqual(result["trials"][0]["status"], "budget_exceeded")
            with patch(
                "personalbench.engine.run_candidate",
                side_effect=AssertionError("Retried full task timeout"),
            ) as retry:
                resumed = evaluate(
                    benchmark(),
                    short,
                    failed,
                    allow_unsafe_local=True,
                    max_total_seconds=3,
                    resume=True,
                )
            self.assertEqual(retry.call_count, 0)
            self.assertEqual(resumed["summary"]["success_pct"], 0)
            self.assertEqual(resumed["retry_history"], [])

    def test_grading_budget_extension_reuses_candidate_and_preserves_judge_spend(self):
        clock = ManualClock()
        bench = benchmark()
        bench["tasks"][0]["criteria"][0]["kind"] = "judge"

        def slow(config, *, calls, **kwargs):
            def assess(prompt):
                clock.advance(0.25)
                calls.append({"status": "completed", "cost_usd": 0.1})
                return '{"scores":[{"id":"check","score":1,"evidence":"done"}]}'

            return assess

        def fast(config, *, calls, **kwargs):
            def assess(prompt):
                calls.append({"status": "completed", "cost_usd": 0.2})
                return '{"scores":[{"id":"check","score":1,"evidence":"done"}]}'

            return assess

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("personalbench.engine.time.monotonic", clock),
        ):
            output = Path(directory) / "run"
            with patch("personalbench.engine.model_callback", side_effect=slow):
                first = evaluate(
                    bench,
                    fixture(),
                    output,
                    judge_config=fixture(),
                    allow_unsafe_local=True,
                    max_total_seconds=0.2,
                )
            self.assertEqual(first["run_status"], "budget_stopped")
            self.assertTrue(first["trials"][0]["budget_limited"])
            with (
                patch("personalbench.engine.model_callback", side_effect=fast),
                patch(
                    "personalbench.engine.run_candidate",
                    side_effect=AssertionError("Candidate reran"),
                ),
            ):
                final = evaluate(
                    bench,
                    fixture(),
                    output,
                    judge_config=fixture(),
                    allow_unsafe_local=True,
                    max_total_seconds=2,
                    resume=True,
                )
            self.assertEqual(final["run_status"], "complete")
            self.assertEqual(final["summary"]["success_pct"], 100)
            self.assertEqual([row["cost_usd"] for row in final["judge_calls"]], [0.1, 0.2])


class AssessmentRefreshTests(unittest.TestCase):
    def setUp(self):
        from personalbench.implementation import implementation_sources

        self.sources = implementation_sources()
        self.updated = {
            **self.sources,
            "grading.py": self.sources["grading.py"]
            + b"\n# Synthetic assessment-evidence context revision\n",
        }
        self.bench = benchmark(("a", "b"))
        for task in self.bench["tasks"]:
            task["brief"] = f"Return done for {task['id']}"
            task["criteria"][0]["kind"] = "judge"
        self.config = fixture(("a", "b"))
        self.judge = fixture()

    def callback(self, *, revised=False, interrupt=False, observed=None):
        def callback(config, *, calls, **kwargs):
            def judge(prompt):
                payload = json.loads(prompt.split("\n", 1)[1])
                identifier = payload["task_brief"].rsplit(" ", 1)[-1]
                if observed is not None:
                    observed.append(identifier)
                calls.append({"status": "completed", "cost_usd": 0.2 if revised else 0.1})
                if interrupt and identifier == "b":
                    raise KeyboardInterrupt
                score = 1 if revised or identifier == "a" else 0
                return json.dumps(
                    {
                        "scores": [
                            {
                                "id": "check",
                                "score": score,
                                "evidence": "New captured evidence" if revised else "Old context",
                            }
                        ]
                    }
                )

            return judge

        return callback

    def original(self, output, *, benchmark_value=None, **kwargs):
        with (
            patch("personalbench.engine.implementation_sources", return_value=self.sources),
            patch("personalbench.engine.model_callback", side_effect=self.callback()),
        ):
            return evaluate(
                benchmark_value or self.bench,
                self.config,
                output,
                judge_config=self.judge,
                allow_unsafe_local=True,
                **kwargs,
            )

    def test_reassesses_all_completed_outcomes_without_candidate_calls_and_keeps_backup(self):
        from personalbench.grading import grade
        from personalbench.schema import canonical_hash

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            original = self.original(output)
            old_bytes = (output / "report.json").read_bytes()
            self.assertEqual(original["summary"]["success_pct"], 50)
            observed = []
            with (
                patch("personalbench.engine.implementation_sources", return_value=self.updated),
                patch(
                    "personalbench.engine.model_callback",
                    side_effect=self.callback(revised=True, observed=observed),
                ),
                patch(
                    "personalbench.engine.run_candidate",
                    side_effect=AssertionError("Candidate reran"),
                ),
                patch("personalbench.engine.grade", wraps=grade) as graded,
            ):
                refreshed = evaluate(
                    self.bench,
                    self.config,
                    output,
                    judge_config=self.judge,
                    allow_unsafe_local=True,
                    resume=True,
                    refresh_assessments=True,
                )
                self.assertEqual(graded.call_count, 2)
            self.assertEqual(observed, ["a", "b"])
            self.assertEqual(refreshed["summary"]["success_pct"], 100)
            self.assertEqual(
                [row["cost_usd"] for row in refreshed["judge_calls"]], [0.1, 0.2, 0.1, 0.2]
            )
            self.assertEqual(
                [row["output"] for row in refreshed["trials"]],
                [row["output"] for row in original["trials"]],
            )
            self.assertEqual(refreshed["scoring_hash"], canonical_hash(refreshed["scoring_policy"]))
            revision = refreshed["assessment_revisions"][0]
            self.assertEqual(revision["previous_scoring_hash"], original["scoring_hash"])
            self.assertEqual(revision["current_scoring_hash"], refreshed["scoring_hash"])
            self.assertEqual(Path(revision["source_snapshot"]).read_bytes(), old_bytes)
            self.assertFalse(json.loads(old_bytes)["trials"][1]["grading"]["success"])
            self.assertNotIn("assessment_refresh_pending", refreshed)
            with (
                patch("personalbench.engine.implementation_sources", return_value=self.updated),
                patch(
                    "personalbench.engine.model_callback",
                    side_effect=AssertionError("Repeated assessment"),
                ),
                patch(
                    "personalbench.engine.run_candidate",
                    side_effect=AssertionError("Candidate reran"),
                ),
            ):
                idempotent = evaluate(
                    self.bench,
                    self.config,
                    output,
                    judge_config=self.judge,
                    allow_unsafe_local=True,
                    resume=True,
                    refresh_assessments=True,
                )
            self.assertEqual(idempotent["assessment_revisions"], refreshed["assessment_revisions"])
            self.assertEqual(idempotent["trials"], refreshed["trials"])

    def test_interrupted_assessment_refresh_resumes_pending_only_without_candidate_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            self.original(output)
            with (
                patch("personalbench.engine.implementation_sources", return_value=self.updated),
                patch(
                    "personalbench.engine.model_callback",
                    side_effect=self.callback(revised=True, interrupt=True),
                ),
                patch(
                    "personalbench.engine.run_candidate",
                    side_effect=AssertionError("Candidate reran"),
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                evaluate(
                    self.bench,
                    self.config,
                    output,
                    judge_config=self.judge,
                    allow_unsafe_local=True,
                    resume=True,
                    refresh_assessments=True,
                )
            interrupted = json.loads((output / "report.json").read_text())
            self.assertTrue(interrupted["assessment_refresh_pending"])
            self.assertNotIn("assessment_refresh_pending", interrupted["trials"][0])
            self.assertTrue(interrupted["trials"][1]["assessment_refresh_pending"])
            observed = []
            with (
                patch("personalbench.engine.implementation_sources", return_value=self.updated),
                patch(
                    "personalbench.engine.model_callback",
                    side_effect=self.callback(revised=True, observed=observed),
                ),
                patch(
                    "personalbench.engine.run_candidate",
                    side_effect=AssertionError("Candidate reran"),
                ),
            ):
                resumed = evaluate(
                    self.bench,
                    self.config,
                    output,
                    judge_config=self.judge,
                    allow_unsafe_local=True,
                    resume=True,
                )
            self.assertEqual(observed, ["b"])
            self.assertEqual(resumed["summary"]["success_pct"], 100)
            self.assertEqual(len(resumed["assessment_revisions"]), 1)
            self.assertNotIn("assessment_refresh_pending", resumed)

    def test_refresh_rejects_changed_execution_or_scoring_contract_without_modifying_report(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            self.original(output)
            before = (output / "report.json").read_bytes()
            changed_bench = copy.deepcopy(self.bench)
            changed_bench["scoring"] = {"success_threshold": 90}
            cases = [
                (changed_bench, self.config, self.judge, {}, self.updated),
                (self.bench, dict(self.config, model="different"), self.judge, {}, self.updated),
                (self.bench, self.config, dict(self.judge, model="different"), {}, self.updated),
                (self.bench, self.config, self.judge, {"workers": 2}, self.updated),
                (self.bench, self.config, self.judge, {"repeats": 2}, self.updated),
                (
                    self.bench,
                    self.config,
                    self.judge,
                    {},
                    {
                        **self.updated,
                        "scoring.py": self.sources["scoring.py"]
                        + b"\n# changed aggregation source\n",
                    },
                ),
            ]
            for bench, configuration, judge, kwargs, sources in cases:
                with (
                    self.subTest(kwargs=kwargs, model=configuration["model"], judge=judge["model"]),
                    patch("personalbench.engine.implementation_sources", return_value=sources),
                    patch(
                        "personalbench.engine.run_candidate",
                        side_effect=AssertionError("Candidate reran"),
                    ),
                    self.assertRaises(ValueError),
                ):
                    evaluate(
                        bench,
                        configuration,
                        output,
                        judge_config=judge,
                        allow_unsafe_local=True,
                        resume=True,
                        refresh_assessments=True,
                        **kwargs,
                    )
                self.assertEqual((output / "report.json").read_bytes(), before)
                self.assertFalse((output / "assessment-revisions").exists())
            with (
                patch("personalbench.engine.implementation_sources", return_value=self.updated),
                patch(
                    "personalbench.engine._version",
                    return_value={"harness": "fixture", "version": "changed-runtime"},
                ),
                self.assertRaises(ValueError),
            ):
                evaluate(
                    self.bench,
                    self.config,
                    output,
                    judge_config=self.judge,
                    allow_unsafe_local=True,
                    resume=True,
                    refresh_assessments=True,
                )
            self.assertEqual((output / "report.json").read_bytes(), before)

    def test_refresh_rejects_unexecuted_or_uncaptured_command_and_file_state_atomically(self):
        for mode in ("unexecuted", "command", "file", "environment"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "run"
                bench = copy.deepcopy(self.bench)
                kwargs = {}
                if mode == "unexecuted":
                    kwargs["stop_after_cost_usd"] = 0.5
                elif mode == "command":
                    bench["tasks"][0]["criteria"] = [
                        {
                            "id": "cmd",
                            "description": "Run hidden behavioral test",
                            "kind": "command",
                            "value": [sys.executable, "test.py"],
                        }
                    ]
                elif mode == "file":
                    bench["tasks"][0]["criteria"] = [
                        {
                            "id": "file",
                            "description": "Captured file content",
                            "kind": "file_contains",
                            "target": "missing.txt",
                            "value": "done",
                        }
                    ]
                elif mode == "environment":
                    bench["tasks"][0]["environment"] = {
                        "kind": "calendar-mail-v1",
                        "fixture_origin": "synthetic",
                        "now": "2026-11-26T12:00:00Z",
                        "timezone": "UTC",
                        "calendars": [{"id": "primary", "events": []}],
                        "messages": [],
                    }
                    bench["tasks"][0]["criteria"] = [
                        {
                            "id": "state",
                            "description": "Captured environment state",
                            "kind": "environment_state",
                            "value": {"calendar_id": "primary", "absent_event_ids": ["obsolete"]},
                        }
                    ]
                self.original(output, benchmark_value=bench, **kwargs)
                before = (output / "report.json").read_bytes()
                with (
                    patch("personalbench.engine.implementation_sources", return_value=self.updated),
                    patch(
                        "personalbench.engine.run_candidate",
                        side_effect=AssertionError("Candidate reran"),
                    ),
                    self.assertRaises(ValueError),
                ):
                    evaluate(
                        bench,
                        self.config,
                        output,
                        judge_config=self.judge,
                        allow_unsafe_local=True,
                        resume=True,
                        refresh_assessments=True,
                    )
                self.assertEqual((output / "report.json").read_bytes(), before)
                self.assertFalse((output / "assessment-revisions").exists())

    def test_refresh_requires_explicit_resume_and_fresh_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            for kwargs in ({}, {"resume": True, "baseline_type": "recorded"}):
                with (
                    self.subTest(kwargs=kwargs),
                    self.assertRaisesRegex(ValueError, "resume of fresh"),
                ):
                    evaluate(
                        self.bench,
                        self.config,
                        Path(directory) / "run",
                        refresh_assessments=True,
                        **kwargs,
                    )

    def test_refresh_preserves_terminal_timeout_zero_and_only_reassesses_completed_candidate(self):
        def timeout(task, configuration, workspace, **kwargs):
            result = run_candidate(task, configuration, workspace, **kwargs)
            if task["id"] == "a":
                result.update(status="budget_exceeded", output="", error="Synthetic timeout")
            return result

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            with patch("personalbench.engine.run_candidate", side_effect=timeout):
                original = self.original(output)
            original_timeout = copy.deepcopy(original["trials"][0])
            observed = []
            with (
                patch("personalbench.engine.implementation_sources", return_value=self.updated),
                patch(
                    "personalbench.engine.model_callback",
                    side_effect=self.callback(revised=True, observed=observed),
                ),
                patch(
                    "personalbench.engine.run_candidate",
                    side_effect=AssertionError("Candidate reran"),
                ),
            ):
                refreshed = evaluate(
                    self.bench,
                    self.config,
                    output,
                    judge_config=self.judge,
                    allow_unsafe_local=True,
                    resume=True,
                    refresh_assessments=True,
                )
            self.assertEqual(observed, ["b"])
            self.assertEqual(refreshed["trials"][0], original_timeout)
            self.assertEqual(refreshed["summary"]["success_pct"], 50)

    def test_refresh_rejects_corrupt_saved_policy_or_summary_before_creating_backup(self):
        for corrupt in ("policy", "summary", "duplicate"):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "run"
                self.original(output)
                path = output / "report.json"
                report = json.loads(path.read_text())
                if corrupt == "policy":
                    report["scoring_policy"]["policy"] = "tampered"
                elif corrupt == "summary":
                    report["summary"]["success_pct"] = 123
                else:
                    report["trials"].append(copy.deepcopy(report["trials"][0]))
                path.write_text(json.dumps(report))
                before = path.read_bytes()
                with (
                    patch("personalbench.engine.implementation_sources", return_value=self.updated),
                    patch(
                        "personalbench.engine.run_candidate",
                        side_effect=AssertionError("Candidate reran"),
                    ),
                    self.assertRaises(ValueError),
                ):
                    evaluate(
                        self.bench,
                        self.config,
                        output,
                        judge_config=self.judge,
                        allow_unsafe_local=True,
                        resume=True,
                        refresh_assessments=True,
                    )
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse((output / "assessment-revisions").exists())
