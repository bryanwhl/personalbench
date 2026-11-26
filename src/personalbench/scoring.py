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


def _prepared_subset_scope(reports: list[dict], eligible_sets: list, metric: str) -> dict | None:
    """Authorize only the same frozen preparation subset, never an observed winner subset."""
    manifests = []
    for report in reports:
        manifest = report.get("task_manifest")
        if isinstance(manifest, dict):
            if any(not isinstance(value, dict) for value in manifest.values()):
                return None
            manifest = [{**value, "id": tid} for tid, value in manifest.items()]
        if not isinstance(manifest, list) or not manifest:
            return None
        states = {}
        for task in manifest:
            if not isinstance(task, dict):
                return None
            tid = task.get("id")
            state = task.get("readiness", "ready")
            # Engine manifests represent an absent task.readiness as null.
            state = "ready" if state is None else state
            if (
                not isinstance(tid, str)
                or not tid
                or tid in states
                or not isinstance(state, str)
                or state not in {"ready", "incomplete", "unsupported"}
            ):
                return None
            states[tid] = state
        coverage = report.get("summary", {}).get("coverage", {})
        if coverage.get("total_tasks") != len(states):
            return None
        manifests.append(states)
    if any(states != manifests[0] for states in manifests[1:]):
        return None
    states = manifests[0]
    ready = sorted(tid for tid, state in states.items() if state == "ready")
    excluded = sorted(set(states) - set(ready))
    if not ready or not excluded or any(ids != ready for ids in eligible_sets):
        return None
    observed_metric = {
        "output_quality_pct": "observed_quality_pct",
        "success_pct": "observed_success_pct",
    }.get(metric)
    if observed_metric is None:
        return None
    for report in reports:
        summary = report.get("summary", {})
        rows = summary.get("tasks")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return None
        if any(not isinstance(row.get("task_id"), str) for row in rows):
            return None
        by_id = {row.get("task_id"): row for row in rows}
        if len(by_id) != len(rows) or set(by_id) != set(states):
            return None
        for tid in ready:
            row = by_id[tid]
            expected = row.get("expected_repeats")
            if (
                not isinstance(expected, int)
                or isinstance(expected, bool)
                or expected < 1
                or row.get("complete") is not True
                or row.get("attempted_repeats") != expected
                or row.get("scored_repeats") != expected
                or row.get("unknown_repeats", 0) != 0
                or not _valid(row.get(metric), maximum=100)
            ):
                return None
        # Otherwise a partial excluded goal could leak into observed averages.
        if any(
            by_id[tid].get("scored_repeats", 0) != 0
            or by_id[tid].get("observed_quality_pct") is not None
            or by_id[tid].get("observed_success_pct") is not None
            for tid in excluded
        ):
            return None
        if not _valid(summary.get(observed_metric), maximum=100):
            return None
        if not math.isclose(
            summary[observed_metric], mean(by_id[tid][metric] for tid in ready), abs_tol=1e-9
        ):
            return None
    return {
        "kind": "fixed_prepared_subset",
        "task_ids": ready,
        "evaluated_tasks": len(ready),
        "total_tasks": len(states),
        "excluded_task_ids": excluded,
        "excluded_readiness": {tid: states[tid] for tid in excluded},
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
            "metric_scope": {},
            "warnings": [],
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
    boundaries = {report.get("runtime", {}).get("input_boundary") for report in reports}
    if len(boundaries) != 1:
        reasons.append(
            "Reports must share the same candidate input boundary; legacy unrestricted evidence cannot compare with confined runs"
        )
    if len(baseline_types) != 1 or not baseline_types <= {"fresh", "recorded"}:
        reasons.append("Recorded and fresh runs are different baseline types")
    metric_reasons: dict[str, list[str]] = {}
    comparable_metrics = []
    metric_scope = {}
    metric_values = {}
    for metric in metrics:
        failures = []
        raw_sets = [
            report.get("summary", {}).get("coverage", {}).get("eligible_task_ids", {}).get(metric)
            for report in reports
        ]
        eligible_sets = [
            sorted(ids)
            if isinstance(ids, list) and all(isinstance(tid, str) for tid in ids)
            else None
            for ids in raw_sets
        ]
        same_sets = (
            bool(eligible_sets[0])
            and all(
                ids == eligible_sets[0] and len(set(ids)) == len(ids)
                for ids in eligible_sets
                if ids is not None
            )
            and all(ids is not None for ids in eligible_sets)
        )
        if not same_sets:
            failures.append("Different or missing eligible task sets")
        whole = same_sets and all(
            isinstance(report.get("summary", {}).get("coverage", {}).get("total_tasks"), int)
            and len(eligible_sets[0]) == report["summary"]["coverage"]["total_tasks"]
            for report in reports
        )
        scope = None
        values = [report.get("summary", {}).get(metric) for report in reports]
        if whole:
            scope = {
                "kind": "whole_benchmark",
                "task_ids": eligible_sets[0],
                "evaluated_tasks": len(eligible_sets[0]),
                "total_tasks": len(eligible_sets[0]),
            }
        elif same_sets and metric != "efficiency_pct":
            scope = _prepared_subset_scope(reports, eligible_sets, metric)
            if scope is not None:
                observed = (
                    "observed_quality_pct"
                    if metric == "output_quality_pct"
                    else "observed_success_pct"
                )
                values = [report["summary"][observed] for report in reports]
        if scope is None:
            failures.append(
                "Every benchmark task or the identical fully graded prepared-ready subset "
                "must be eligible for this metric"
            )
        if any(not _valid(value, maximum=100) for value in values):
            failures.append("Incomplete measurement or grading evidence")
        metric_reasons[metric] = reasons + failures
        if not metric_reasons[metric]:
            comparable_metrics.append(metric)
            metric_scope[metric] = scope
            metric_values[metric] = values
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
    executions = [report.get("execution", {}) for report in reports]
    changed_execution_fields = sorted(
        {
            key
            for execution in executions
            for key in execution
            if any(execution.get(key) != executions[0].get(key) for execution in executions)
        }
    )
    if changed_execution_fields:
        caveats.append(
            "Execution scheduling differs: "
            + ", ".join(changed_execution_fields)
            + ". Worker concurrency can affect latency and efficiency."
        )
    if baseline_types == {"recorded"}:
        caveats.append("Recorded results describe historical observations, not controlled reruns.")
    scope_warnings = sorted(
        {
            f"Fixed prepared subset {scope['evaluated_tasks']}/{scope['total_tasks']}; "
            "not a whole-history score. Preparation-incomplete and unsupported goals remain excluded."
            for scope in metric_scope.values()
            if scope["kind"] == "fixed_prepared_subset"
        }
    )
    caveats.extend(scope_warnings)
    deltas = []
    if comparable_metrics:
        for index, report in enumerate(reports[1:], start=1):
            deltas.append(
                {
                    "report_index": index,
                    "baseline_index": 0,
                    **{
                        metric: metric_values[metric][index] - metric_values[metric][0]
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
        "metric_scope": metric_scope,
        "warnings": scope_warnings,
        "deltas": deltas,
        "changed_configuration_fields": changed_fields,
        "changed_execution_fields": changed_execution_fields,
        "caveats": caveats,
    }
