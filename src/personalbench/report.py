"""Render private, self-contained reports without executable candidate content."""

from __future__ import annotations

import html
import json
import math
from collections import Counter


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _percent(value: object) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return f"{value:.1f}%"
    return "Unavailable"


def _json(value: object) -> str:
    return _escape(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def render_report(report: dict) -> str:
    """Return accessible HTML; every user-controlled value is escaped as text."""
    summary = report.get("summary", {})
    config = report.get("configuration", {})
    name = config.get("name", "Personalbench run")
    baseline = report.get("baseline_type", "unknown")
    fixture = config.get("harness") == "fixture" and baseline != "recorded"
    notice = (
        "Synthetic fixture run. These results test Personalbench and provide no evidence about real model performance."
        if fixture
        else "Recorded baseline. Historical outcomes are observations, not controlled reruns."
        if baseline == "recorded"
        else "Fresh run. Results describe this complete setup on these tasks, tools, and budgets."
    )
    scope_notice = (
        f'<p class="notice">{_escape(report["scope_note"])}</p>' if report.get("scope_note") else ""
    )
    cards = []
    for key, label in (
        ("output_quality_pct", "Output quality"),
        ("success_pct", "Success"),
        ("efficiency_pct", "Efficiency"),
    ):
        value = summary.get(key)
        observed_key = (
            "observed_quality_pct" if key == "output_quality_pct" else "observed_success_pct"
        )
        if value is None and key != "efficiency_pct" and summary.get(observed_key) is not None:
            value = summary[observed_key]
            label = "Graded task " + ("quality" if key == "output_quality_pct" else "success")
        cards.append(
            f'<div class="metric"><span>{label}</span><strong>{_percent(value)}</strong></div>'
        )
    cards = "".join(cards)
    definitions = report.get("task_manifest", [])
    titles = (
        {
            task.get("id"): str(task.get("brief", "")).split("\n", 1)[0][:140]
            for task in definitions
            if isinstance(task, dict)
        }
        if isinstance(definitions, list)
        else {}
    )
    coverage = summary.get("coverage", {})
    observed_note = ""
    if (
        coverage.get("scored_trials", 0) < coverage.get("expected_trials", 0)
        and summary.get("observed_quality_pct") is not None
    ):
        observed_note = (
            "<p>Graded subset only: success "
            + _percent(summary.get("observed_success_pct"))
            + "; output quality "
            + _percent(summary.get("observed_quality_pct"))
            + ". These scores exclude unscorable goals and do not establish an overall score.</p>"
        )
    history = report.get("history")
    history_section = ""
    if isinstance(history, dict):
        counts = Counter(row.get("status", "unknown") for row in history.get("sessions", []))
        history_section = (
            "<section><h2>Source coverage</h2><p>History window: "
            + _escape(history.get("since", "unknown"))
            + " to "
            + _escape(history.get("as_of", "unknown"))
            + ". Source sessions: "
            + _escape(sum(counts.values()))
            + ". "
            + _escape(", ".join(f"{status}: {count}" for status, count in sorted(counts.items())))
            + ".</p><p>Task scores cover the declared benchmark only. Excluded sources and "
            "preparation errors do not count as successful tasks.</p>"
            "<details><summary>Source inventory and preparation decisions</summary><pre>"
            + _json(history)
            + "</pre></details></section>"
        )
    rows = []
    for task in summary.get("tasks", []):
        reliability = task.get("reliability", {})
        rows.append(
            "<tr>"
            + "".join(
                f"<td>{value}</td>"
                for value in (
                    _escape(titles.get(task.get("task_id")) or task.get("task_id", "")),
                    _escape(task.get("category", "")),
                    _percent(task.get("output_quality_pct")),
                    _percent(task.get("success_pct")),
                    _percent(task.get("efficiency_pct")),
                    _escape(f"{task.get('scored_repeats', 0)}/{task.get('expected_repeats', 0)}"),
                    "Mixed outcomes"
                    if reliability.get("mixed_outcomes")
                    else "No mixed outcomes observed"
                    if task.get("scored_repeats", 0) > 1
                    else "Single graded repeat"
                    if task.get("scored_repeats", 0) == 1
                    else "No graded repeats",
                )
            )
            + "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="7">No task evidence is available.</td></tr>')
    categories = []
    for category, values in summary.get("categories", {}).items():
        categories.append(
            "<tr>"
            + "".join(
                f"<td>{value}</td>"
                for value in (
                    _escape(category),
                    _escape(values.get("task_count", 0)),
                    _percent(values.get("output_quality_pct")),
                    _percent(values.get("success_pct")),
                    _percent(values.get("efficiency_pct")),
                )
            )
            + "</tr>"
        )
    category_section = (
        (
            '<section><h2>Results by category</h2><div class="table-wrap"><table>'
            '<thead><tr><th scope="col">Category</th><th scope="col">Tasks</th>'
            '<th scope="col">Quality</th><th scope="col">Success</th><th scope="col">Efficiency</th>'
            "</tr></thead><tbody>" + "".join(categories) + "</tbody></table></div></section>"
        )
        if categories
        else ""
    )
    manifest = report.get("task_manifest", [])
    if isinstance(manifest, dict):
        manifest = [
            {"id": tid, **value} for tid, value in manifest.items() if isinstance(value, dict)
        ]
    task_details = []
    for task in manifest:
        if not isinstance(task, dict):
            continue
        details = {
            key: task.get(key)
            for key in ("category", "readiness", "provenance", "reference", "criteria")
            if key in task
        }
        task_details.append(
            f"<details><summary>{_escape(task.get('id', 'Unnamed task'))}: task and evidence</summary>"
            f"<h3>Task brief</h3><pre>{_escape(task.get('brief', ''))}</pre>"
            f"<h3>Origin, grading rules, and reference</h3><pre>{_json(details)}</pre></details>"
        )
    manifest_section = (
        "<section><h2>Task definitions</h2>" + "".join(task_details) + "</section>"
        if task_details
        else ""
    )
    attempts = []
    for index, trial in enumerate(report.get("trials", []), 1):
        grading = trial.get("grading", {})
        evidence = []
        for criterion in grading.get("criteria", []):
            evidence.append(
                f"<li><strong>{_escape(criterion.get('id', ''))}</strong>: "
                f"{_escape(criterion.get('score', 'unknown'))} — "
                f"{_escape(criterion.get('evidence', ''))}</li>"
            )
        output = trial.get("output", "")
        metadata = {
            key: trial.get(key)
            for key in (
                "status",
                "duration_seconds",
                "user_prompts",
                "cost_usd",
                "cost_kind",
                "usage",
                "error",
            )
        }
        attempts.append(
            f"<details><summary>Attempt {index}: {_escape(trial.get('task_id', ''))} "
            f"· repeat {_escape(trial.get('repeat', 'unknown'))}</summary>"
            f"<p>Grading: {_escape(grading.get('status', 'unknown'))}. "
            f"Quality: {_percent(grading.get('quality_pct'))}.</p>"
            f"<p>{_escape(grading.get('error', ''))}</p><ul>{''.join(evidence)}</ul>"
            f"<h3>Measurements</h3><pre>{_json(metadata)}</pre>"
            f"<h3>Candidate output</h3><pre>{_escape(output)}</pre></details>"
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>{_escape(name)} — Personalbench</title>
<style>
:root {{ color-scheme:light dark; font-family:system-ui,sans-serif; line-height:1.6; }}
body {{ max-width:1100px; margin:auto; padding:24px; background:#10191e; color:#edf4f4; }}
a {{ color:#94e5d5; }} h1,h2,h3 {{ line-height:1.2; }} h1 {{ font-size:clamp(2rem,6vw,3.4rem); }}
.muted {{ color:#b8cace; }} .notice {{ padding:16px; border-left:4px solid #90ddc9; background:#1b3036; }}
.metrics {{ display:flex; gap:16px; flex-wrap:wrap; margin:24px 0; }}
.metric {{ flex:1; min-width:180px; background:#1b3036; border:1px solid #40616b; border-radius:8px; padding:20px; }}
.metric span,.metric strong {{ display:block; }} .metric strong {{ font-size:2rem; color:#a4ead8; }}
.table-wrap {{ overflow-x:auto; }} table {{ width:100%; border-collapse:collapse; text-align:left; }}
th,td {{ padding:12px; border-bottom:1px solid #40616b; }} th {{ color:#a4ead8; }}
details {{ border:1px solid #40616b; border-radius:8px; padding:16px; margin:12px 0; }}
summary {{ cursor:pointer; font-weight:650; }} summary:focus-visible {{ outline:3px solid #a4ead8; outline-offset:4px; }}
pre {{ white-space:pre-wrap; overflow-wrap:anywhere; background:#0c1317; padding:16px; border-radius:6px; font-size:0.9rem; }}
section {{ margin:32px 0; }} @media(max-width:500px) {{ body {{ padding:16px; }} th,td {{ padding:8px; }} }}
@media print {{ body {{ color:#111; background:white; }} .metric,.notice,pre {{ color:#111; background:#eee; }} details {{ break-inside:avoid; }} }}
</style></head><body><header><p class="muted">PERSONALBENCH · PRIVATE REPORT</p>
<h1>{_escape(name)}</h1><p>{_escape(report.get("created_at", ""))}</p>
<p class="notice">{_escape(notice)}</p>{scope_notice}</header><main>
<section aria-label="Headline scores"><div class="metrics">{cards}</div>
<p>Graded trials: {_escape(coverage.get("scored_trials", 0))}/{_escape(coverage.get("expected_trials", 0))}.
Complete tasks: {_escape(coverage.get("complete_tasks", 0))}/{_escape(coverage.get("total_tasks", 0))}.</p>
<p class="muted">Unavailable means evidence is missing. Missing results never count as passing results.</p></section>
{observed_note}
{history_section}
<section><h2>Results by task</h2><div class="table-wrap"><table><caption>Tasks have equal weight in the headline scores.</caption>
<thead><tr><th scope="col">Task</th><th scope="col">Category</th><th scope="col">Quality</th><th scope="col">Success</th><th scope="col">Efficiency</th><th scope="col">Graded repeats</th><th scope="col">Reliability</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div></section>
{category_section}
{manifest_section}
<section><h2>How to read these scores</h2><p>{_escape(summary.get("method", "Scores require complete grading evidence."))}</p>
<p>Output quality measures the declared criteria. Success also requires every critical criterion to pass.</p>
<p>Efficiency averages time, user prompts, and cost relative to each task's reference. Each component is capped at 100%.</p>
<p>Failed attempts receive zero efficiency. Missing reference or measurement data makes efficiency unavailable.</p>
<p>{_escape(summary.get("reliability", {}).get("note", "Few repeated runs cannot establish a universal winner."))}</p>
<p>Compare reports only when task definitions, scoring rules, eligible tasks, and baseline types match.
Changes in models, tools, instructions, or budgets compare complete setups.</p></section>
<section><h2>Attempt evidence</h2>{"".join(attempts) or "<p>No attempts were recorded.</p>"}</section>
<section><h2>Reproducibility</h2><details><summary>Configuration and evidence identifiers</summary>
<pre>{_json({"baseline_type": baseline, "benchmark_hash": report.get("benchmark_hash"), "scoring_hash": report.get("scoring_hash"), "configuration": config})}</pre></details></section>
</main><footer><p class="muted">Local HTML. No remote assets, tracking, or external scripts.</p></footer></body></html>"""
