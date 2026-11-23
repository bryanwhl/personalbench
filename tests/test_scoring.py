import copy
import unittest

from personalbench.scoring import compare_reports, summarize


def task(tid):
    return {"id": tid, "reference": {"time_seconds": 10, "user_prompts": 1, "cost_usd": 1}}


def trial(tid, *, quality=100, success=True, repeat=0, **extra):
    return {
        "task_id": tid,
        "repeat": repeat,
        "status": "completed",
        "duration_seconds": 20,
        "user_prompts": 2,
        "cost_usd": 2,
        "grading": {"status": "scored", "quality_pct": quality, "success": success},
        **extra,
    }


class ScoringTests(unittest.TestCase):
    def test_equal_task_weights_and_bounded_reference_ratios(self):
        benchmark = {"tasks": [task("a"), task("b")]}
        summary = summarize(
            benchmark, [trial("a", quality=100), trial("b", quality=40, success=False)]
        )
        self.assertEqual(summary["output_quality_pct"], 70)
        self.assertEqual(summary["success_pct"], 50)
        self.assertEqual(summary["efficiency_pct"], 25)
        self.assertEqual(
            summarize(
                {"tasks": [task("a")]},
                [trial("a", duration_seconds=1, user_prompts=1, cost_usd=0.1)],
            )["efficiency_pct"],
            100,
        )

    def test_repeats_median_and_mixed_outcomes(self):
        results = [
            trial("a", repeat=0, duration_seconds=1),
            trial("a", repeat=1, duration_seconds=19),
            trial("a", repeat=2, success=False),
        ]
        summary = summarize({"tasks": [task("a")]}, results)
        self.assertAlmostEqual(summary["success_pct"], 100 * 2 / 3)
        # Successful time median is 10 => 100%; prompt and cost components 50%.
        self.assertAlmostEqual(summary["efficiency_pct"], (100 + 50 + 50) / 3 * 2 / 3)
        self.assertEqual(summary["reliability"]["mixed_outcome_tasks"], 1)

    def test_missing_measurement_and_failed_efficiency(self):
        summary = summarize({"tasks": [task("a")]}, [trial("a", cost_usd=None)])
        self.assertIsNone(summary["efficiency_pct"])
        summary = summarize({"tasks": [task("a")]}, [trial("a", success=False, cost_usd=None)])
        self.assertEqual(summary["efficiency_pct"], 0)
        for value in (float("nan"), float("inf"), -1, True):
            self.assertIsNone(
                summarize({"tasks": [task("a")]}, [trial("a", cost_usd=value)])["efficiency_pct"]
            )

    def test_zero_measurements_are_bounded_speedups(self):
        self.assertAlmostEqual(
            summarize({"tasks": [task("a")]}, [trial("a", duration_seconds=0)])["efficiency_pct"],
            200 / 3,
        )
        zero = task("a")
        zero["reference"]["cost_usd"] = 0
        self.assertIsNotNone(
            summarize({"tasks": [zero]}, [trial("a", cost_usd=0)])["efficiency_pct"]
        )

    def test_missing_and_grading_errors_never_improve_headline(self):
        benchmark = {"tasks": [task("a"), task("b")]}
        error = trial(
            "b", grading={"status": "grading_error", "quality_pct": None, "success": None}
        )
        summary = summarize(benchmark, [trial("a"), error])
        self.assertIsNone(summary["output_quality_pct"])
        self.assertIsNone(summary["success_pct"])
        self.assertIsNone(summary["efficiency_pct"])
        self.assertEqual(summary["coverage"]["scored_trials"], 1)
        self.assertEqual(summary["coverage"]["expected_trials"], 2)
        self.assertEqual(summary["observed_quality_pct"], 100)
        self.assertIsNone(summarize(benchmark, [trial("a")])["success_pct"])
        partial = summarize(
            {"tasks": [task("a"), task("b")]}, [trial("a"), trial("a", repeat=1), trial("b")]
        )
        self.assertEqual(partial["coverage"]["expected_trials"], 4)
        self.assertIsNone(partial["success_pct"])

    def test_invalid_and_duplicate_trials(self):
        for attempts in ([trial("other")], [trial("a"), trial("a")]):
            with self.assertRaises(ValueError):
                summarize({"tasks": [task("a")]}, attempts)
        result = summarize({"tasks": [task("a")]}, [trial("a", quality=float("nan"))])
        self.assertIsNone(result["output_quality_pct"])

    def report(self):
        return {
            "benchmark_hash": "b",
            "scoring_hash": "s",
            "baseline_type": "fresh",
            "configuration": {"name": "a", "model": "synthetic"},
            "summary": summarize({"tasks": [task("a")]}, [trial("a")]),
        }

    def test_comparison_requires_same_complete_evidence(self):
        first = self.report()
        second = copy.deepcopy(first)
        second["configuration"]["name"] = "b"
        second["summary"]["output_quality_pct"] = 80
        result = compare_reports([first, second])
        self.assertTrue(result["comparable"])
        self.assertEqual(result["deltas"][0]["output_quality_pct"], -20)
        self.assertIn("name", result["changed_configuration_fields"])
        for key, value in (
            ("benchmark_hash", "other"),
            ("scoring_hash", "other"),
            ("baseline_type", "recorded"),
        ):
            changed = copy.deepcopy(second)
            changed[key] = value
            result = compare_reports([first, changed])
            self.assertFalse(result["comparable"])
            self.assertEqual(result["deltas"], [])
        changed = copy.deepcopy(second)
        changed["summary"]["coverage"]["eligible_task_ids"]["success_pct"] = ["other"]
        self.assertNotIn("success_pct", compare_reports([first, changed])["comparable_metrics"])
        first["summary"]["efficiency_pct"] = None
        comparison = compare_reports([first, second])
        self.assertTrue(comparison["comparable"])
        self.assertIn("output_quality_pct", comparison["comparable_metrics"])
        self.assertIsNone(comparison["deltas"][0]["efficiency_pct"])
        self.assertFalse(compare_reports([{}, {}])["comparable"])

    def test_recorded_comparison_has_caveat(self):
        first = self.report()
        first["baseline_type"] = "recorded"
        result = compare_reports([first, copy.deepcopy(first)])
        self.assertTrue(result["comparable"])
        self.assertIn("historical observations", " ".join(result["caveats"]))

    def test_configured_component_weights_and_categories(self):
        a, b = task("a"), task("b")
        a["category"], b["category"] = "research", "coding"
        benchmark = {
            "tasks": [a, b],
            "scoring": {"efficiency_weights": {"time": 2, "prompts": 1, "cost": 1}},
        }
        result = summarize(benchmark, [trial("a", duration_seconds=10), trial("b", success=False)])
        self.assertAlmostEqual(result["tasks"][0]["efficiency_pct"], 75)
        self.assertAlmostEqual(result["efficiency_pct"], 37.5)
        self.assertEqual(result["categories"]["research"]["success_pct"], 100)
        self.assertEqual(result["categories"]["coding"]["success_pct"], 0)
        benchmark["scoring"]["efficiency_weights"]["time"] = float("nan")
        with self.assertRaises(ValueError):
            summarize(benchmark, [])
