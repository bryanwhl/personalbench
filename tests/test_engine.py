import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
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


class EngineTests(unittest.TestCase):
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
