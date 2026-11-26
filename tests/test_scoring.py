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
    def test_legacy_and_confined_input_boundaries_cannot_compare(self):
        report = {
            "benchmark_hash": "same-benchmark",
            "scoring_hash": "same-scoring",
            "baseline_type": "fresh",
            "runtime": {},
            "summary": summarize({"tasks": [task("a")]}, [trial("a")]),
        }
        confined = copy.deepcopy(report)
        confined["runtime"]["input_boundary"] = "workspace-minimal-read-v1"
        result = compare_reports([report, confined])
        self.assertFalse(result["comparable"])
        self.assertTrue(any("input boundary" in reason for reason in result["reasons"]))

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


class PreparedSubsetComparisonTests(unittest.TestCase):
    def report(self, qualities=(100, 50), successes=(True, False)):
        tasks = [task("a"), task("b"), dict(task("c"), readiness="unsupported")]
        trials = [
            trial(tid, quality=quality, success=success)
            for tid, quality, success in zip(("a", "b"), qualities, successes, strict=True)
        ]
        trials.append(
            {"task_id": "c", "status": "preparation_error", "grading": {"status": "unscorable"}}
        )
        return {
            "benchmark_hash": "fixed-benchmark",
            "scoring_hash": "fixed-scoring",
            "baseline_type": "fresh",
            "configuration": {"model": "synthetic"},
            "task_manifest": [
                {"id": "a"},
                {"id": "b", "readiness": "ready"},
                {"id": "c", "readiness": "unsupported"},
            ],
            "summary": summarize({"tasks": tasks, "repeats": 1}, trials),
        }

    def test_same_frozen_preparation_gaps_allow_labeled_quality_success_comparison(self):
        first = self.report()
        second = self.report((90, 80), (True, True))
        result = compare_reports([first, second])
        self.assertTrue(result["comparable"])
        self.assertEqual(result["comparable_metrics"], ["output_quality_pct", "success_pct"])
        self.assertEqual(result["deltas"][0]["output_quality_pct"], 10)
        self.assertEqual(result["deltas"][0]["success_pct"], 50)
        self.assertIsNone(result["deltas"][0]["efficiency_pct"])
        self.assertEqual(result["metric_scope"]["success_pct"]["task_ids"], ["a", "b"])
        self.assertEqual(
            result["metric_scope"]["success_pct"]["excluded_readiness"], {"c": "unsupported"}
        )
        self.assertIn("Fixed prepared subset 2/3; not a whole-history score", result["warnings"][0])
        self.assertIsNone(first["summary"]["success_pct"])

    def test_missing_ready_candidate_or_judge_evidence_never_becomes_a_subset(self):
        first = self.report()
        for status in ("infrastructure_error", "completed"):
            broken = self.report()
            unscored = trial(
                "b",
                status=status,
                grading={"status": "grading_error", "quality_pct": None, "success": None},
            )
            broken["summary"] = summarize(
                {"tasks": [task("a"), task("b"), task("c")], "repeats": 1},
                [
                    trial("a"),
                    unscored,
                    {
                        "task_id": "c",
                        "status": "preparation_error",
                        "grading": {"status": "unscorable"},
                    },
                ],
            )
            with self.subTest(status=status):
                self.assertFalse(compare_reports([first, broken])["comparable"])
                self.assertFalse(compare_reports([broken, copy.deepcopy(broken)])["comparable"])

    def test_different_frozen_manifests_or_eligible_sets_are_rejected(self):
        first = self.report()
        for change in (
            "readiness",
            "eligible",
            "partial_excluded",
            "repeat_gap",
            "observed_average",
        ):
            second = self.report()
            if change == "readiness":
                second["task_manifest"][2]["readiness"] = "incomplete"
            elif change == "eligible":
                second["summary"]["coverage"]["eligible_task_ids"]["output_quality_pct"] = ["a"]
            elif change == "partial_excluded":
                second["summary"]["tasks"][2].update(
                    scored_repeats=1, observed_quality_pct=100, observed_success_pct=100
                )
            elif change == "repeat_gap":
                second["summary"]["tasks"][1]["scored_repeats"] = 0
            else:
                second["summary"]["observed_quality_pct"] = 99
            with self.subTest(change=change):
                result = compare_reports([first, second])
                self.assertNotIn("output_quality_pct", result["comparable_metrics"])
                self.assertIsNone(
                    result["deltas"][0]["output_quality_pct"] if result["deltas"] else None
                )

    def test_worker_changes_are_disclosed_without_invalidating_fixed_scores(self):
        first = self.report()
        second = self.report()
        first["execution"] = {"workers": 1}
        second["execution"] = {"workers": 3}
        result = compare_reports([first, second])
        self.assertTrue(result["comparable"])
        self.assertEqual(result["changed_execution_fields"], ["workers"])
        self.assertIn("Worker concurrency can affect latency", " ".join(result["caveats"]))
