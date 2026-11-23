"""Aggregate goals equally and expose incomplete evidence explicitly."""

from __future__ import annotations

import math
from statistics import mean, median


def _valid(value: object, *, maximum: float | None = None) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
        and (maximum is None or value <= maximum)
    )


def _average_complete(values: list) -> float | None:
    return mean(values) if values and all(value is not None for value in values) else None


def _ratio(reference: object, observed: object) -> float | None:
    if not _valid(reference) or not _valid(observed):
        return None
    if observed == 0:
        return 100.0
    return 100.0 * min(1.0, reference / observed)


def summarize(benchmark: dict, trials: list[dict]) -> dict:
    """Average repeats within each task, then weight every task equally.

    Headline scores require complete evidence. Descriptive observed scores remain
    available separately, with coverage. Efficiency uses the median measurement
    for successful repeats and counts failed repeats as zero efficiency.
    """
    weights = benchmark.get("scoring", {}).get(
        "efficiency_weights", {"time": 1, "prompts": 1, "cost": 1}
    )
    if (
        not isinstance(weights, dict)
        or set(weights) != {"time", "prompts", "cost"}
        or any(not _valid(value) for value in weights.values())
        or sum(weights.values()) <= 0
        or not math.isfinite(sum(weights.values()))
    ):
        raise ValueError(
            "Efficiency weights need finite nonnegative time, prompts, and cost values with a positive sum"
        )

    def efficiency(components: dict) -> float | None:
        # All three measurements remain required even when a component has zero weight.
        if any(value is None for value in components.values()):
            return None
        return sum(components[key] * weight for key, weight in weights.items()) / sum(
            weights.values()
        )

    tasks = benchmark.get("tasks", [])
    task_ids = [task["id"] for task in tasks]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("Benchmark task IDs must be unique")
    grouped: dict[str, list] = {tid: [] for tid in task_ids}
    for trial in trials:
        if trial.get("task_id") not in grouped:
            raise ValueError("Trial references an unknown task")
        grouped[trial["task_id"]].append(trial)
    inferred_repeats = max([len(rows) for rows in grouped.values()] + [1])
    expected_repeats = benchmark.get("repeats", inferred_repeats)
    if (
        not isinstance(expected_repeats, int)
        or isinstance(expected_repeats, bool)
        or expected_repeats < 1
    ):
        raise ValueError("Expected repeat count must be a positive integer")
    rows = []
    for task in tasks:
        attempts = grouped[task["id"]]
        repeat_ids = [attempt.get("repeat", index) for index, attempt in enumerate(attempts)]
        if len(set(repeat_ids)) != len(repeat_ids):
            raise ValueError("Duplicate task repeat identifiers")
        scored = []
        for attempt in attempts:
            grading = attempt.get("grading", {})
            if (
                attempt.get("status") != "infrastructure_error"
                and grading.get("status") == "scored"
                and _valid(grading.get("quality_pct"), maximum=100)
                and isinstance(grading.get("success"), bool)
            ):
                scored.append(attempt)
        complete = len(scored) == expected_repeats and len(attempts) == expected_repeats
        successes = [
            a
            for a in scored
            if a["grading"]["success"] and a.get("status", "completed") == "completed"
        ]
        quality_values = [a["grading"]["quality_pct"] for a in scored]
        observed_quality = mean(quality_values) if quality_values else None
        observed_success = 100 * len(successes) / len(scored) if scored else None
        components = {"time": None, "prompts": None, "cost": None}
        if complete:
            if not successes:
                components = {key: 0.0 for key in components}
            else:
                reference = task.get("reference", {})
                for component, ref_key, observed_key in (
                    ("time", "time_seconds", "duration_seconds"),
                    ("prompts", "user_prompts", "user_prompts"),
                    ("cost", "cost_usd", "cost_usd"),
                ):
                    measurements = [a.get(observed_key) for a in successes]
                    if all(_valid(value) for value in measurements):
                        value = _ratio(reference.get(ref_key), median(measurements))
                        components[component] = (
                            value * len(successes) / len(scored) if value is not None else None
                        )
        rows.append(
            {
                "task_id": task["id"],
                "category": task.get("category", "unspecified"),
                "output_quality_pct": observed_quality if complete else None,
                "quality_pct": observed_quality if complete else None,
                "success_pct": observed_success if complete else None,
                "efficiency_pct": efficiency(components),
                "efficiency_components": components,
                "observed_quality_pct": observed_quality,
                "observed_success_pct": observed_success,
                "expected_repeats": expected_repeats,
                "attempted_repeats": len(attempts),
                "scored_repeats": len(scored),
                "successful_repeats": len(successes),
                "unknown_repeats": max(expected_repeats - len(scored), 0),
                "complete": complete,
                "reliability": {
                    "repeats": len(scored),
                    "successes": len(successes),
                    "quality_min": min(quality_values) if quality_values else None,
                    "quality_max": max(quality_values) if quality_values else None,
                    "mixed_outcomes": bool(successes) and len(successes) != len(scored),
                },
            }
        )
    quality = _average_complete([row["output_quality_pct"] for row in rows])
    success = _average_complete([row["success_pct"] for row in rows])
    efficiency = _average_complete([row["efficiency_pct"] for row in rows])
    eligible = {
        metric: sorted(row["task_id"] for row in rows if row[metric] is not None)
        for metric in ("output_quality_pct", "success_pct", "efficiency_pct")
    }
    return {
        "output_quality_pct": quality,
        "quality_pct": quality,
        "success_pct": success,
        "efficiency_pct": efficiency,
        "tasks": rows,
        "observed_quality_pct": mean(
            [r["observed_quality_pct"] for r in rows if r["observed_quality_pct"] is not None]
        )
        if any(r["observed_quality_pct"] is not None for r in rows)
        else None,
        "observed_success_pct": mean(
            [r["observed_success_pct"] for r in rows if r["observed_success_pct"] is not None]
        )
        if any(r["observed_success_pct"] is not None for r in rows)
        else None,
        "coverage": {
            "total_tasks": len(rows),
            "complete_tasks": sum(r["complete"] for r in rows),
            "attempted_tasks": sum(r["attempted_repeats"] > 0 for r in rows),
            "expected_trials": len(rows) * expected_repeats,
            "scored_trials": sum(r["scored_repeats"] for r in rows),
            "eligible_task_ids": eligible,
        },
        "reliability": {
            "expected_repeats": expected_repeats,
            "mixed_outcome_tasks": sum(r["reliability"]["mixed_outcomes"] for r in rows),
            "note": "Repeated outcomes describe observed stability. Small samples do not establish a universal winner.",
        },
        "categories": {
            category: {
                "task_count": len(category_rows),
                **{
                    metric: _average_complete([row[metric] for row in category_rows])
                    for metric in ("output_quality_pct", "success_pct", "efficiency_pct")
                },
            }
            for category in sorted({row["category"] for row in rows})
            for category_rows in [[row for row in rows if row["category"] == category]]
        },
        "efficiency_weights": weights,
        "method": "Equal task weights; repeats averaged within each task. Efficiency uses successful-repeat medians and zero for failed repeats. Unknown evidence leaves headline scores unavailable.",
    }


def compare_reports(reports: list[dict]) -> dict:
    """Compare each metric separately without letting missing cost hide quality."""
    metrics = ("output_quality_pct", "success_pct", "efficiency_pct")
    if len(reports) < 2:
        return {
            "comparable": False,
            "reasons": ["At least two reports are required"],
            "comparable_metrics": [],
            "metric_reasons": {},
            "deltas": [],
        }
    first = reports[0]
    reasons: list[str] = []
    for key in ("benchmark_hash", "scoring_hash"):
        if (
            not isinstance(first.get(key), str)
            or not first[key]
            or any(report.get(key) != first[key] for report in reports)
        ):
            reasons.append(f"Reports must share the same {key}")
    baseline_types = {report.get("baseline_type") for report in reports}
    if len(baseline_types) != 1 or not baseline_types <= {"fresh", "recorded"}:
        reasons.append("Recorded and fresh runs are different baseline types")
    metric_reasons: dict[str, list[str]] = {}
    comparable_metrics = []
    for metric in metrics:
        failures = []
        eligible_sets = [
            report.get("summary", {}).get("coverage", {}).get("eligible_task_ids", {}).get(metric)
            for report in reports
        ]
        if not eligible_sets[0] or any(
            not isinstance(ids, list) or sorted(ids) != sorted(eligible_sets[0])
            for ids in eligible_sets
        ):
            failures.append("Different or missing eligible task sets")
        if any(
            not _valid(report.get("summary", {}).get(metric), maximum=100) for report in reports
        ):
            failures.append("Incomplete measurement or grading evidence")
        for report in reports:
            coverage = report.get("summary", {}).get("coverage", {})
            if (
                not isinstance(coverage.get("total_tasks"), int)
                or not eligible_sets[0]
                or len(eligible_sets[0]) != coverage["total_tasks"]
            ):
                failures.append("Every benchmark task must be eligible for this metric")
                break
        metric_reasons[metric] = reasons + failures
        if not metric_reasons[metric]:
            comparable_metrics.append(metric)
    configurations = [report.get("configuration", {}) for report in reports]
    changed_fields = sorted(
        {
            key
            for config in configurations
            for key in config
            if any(config.get(key) != configurations[0].get(key) for config in configurations)
        }
    )
    caveats = [
        "Results compare complete setups. Changes in tools, instructions, budgets, and models can all affect outcomes."
    ]
    if baseline_types == {"recorded"}:
        caveats.append("Recorded results describe historical observations, not controlled reruns.")
    deltas = []
    if comparable_metrics:
        for index, report in enumerate(reports[1:], start=1):
            deltas.append(
                {
                    "report_index": index,
                    "baseline_index": 0,
                    **{
                        metric: report["summary"][metric] - first["summary"][metric]
                        if metric in comparable_metrics
                        else None
                        for metric in metrics
                    },
                }
            )
    return {
        "comparable": bool(comparable_metrics),
        "reasons": reasons,
        "comparable_metrics": comparable_metrics,
        "metric_reasons": metric_reasons,
        "deltas": deltas,
        "changed_configuration_fields": changed_fields,
        "caveats": caveats,
    }
