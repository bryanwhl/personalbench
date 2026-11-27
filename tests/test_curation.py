"""Semantic curation is audited before any candidate results exist."""

import copy
import json

import pytest

from personalbench.curation import CurationError, curate_benchmark
from personalbench.privacy import read_json, write_json


def task(identifier, brief=None, *, category="explanation", readiness="ready"):
    return {
        "id": identifier,
        "brief": brief or f"Explain {identifier}",
        "category": category,
        "criteria": [
            {"id": "specific", "kind": "judge", "description": "Explain requested subject"}
        ],
        "inputs": {},
        "readiness": readiness,
        "historical": {"output": "PRIVATE ORIGINAL ANSWER", "success": True},
    }


def bench(tasks):
    return {"schema_version": 1, "name": "Synthetic curation", "tasks": tasks}


def payload(prompt):
    return json.loads(prompt.split("DATA:\n", 1)[1].split("\nCorrect the complete JSON", 1)[0])


def family(members):
    return {
        "label": "Specific requested operation",
        "reason": "Same technical objective and constraints",
        "member_ids": members,
    }


def singletons(prompt):
    data = payload(prompt)
    if "tasks" in data:
        return json.dumps({"families": [family([t["task_id"]]) for t in data["tasks"]]})
    proposals = data["proposed_families"]
    return json.dumps(
        {
            "families": [family([t["task_id"]]) for p in proposals for t in p["tasks"]],
            "audit": {
                "compared_proposal_ids": [p["proposal_id"] for p in proposals],
                "rationale": "Compared every objective and technical subject globally",
            },
        }
    )


def run(benchmark, root, callback=singletons, **kwargs):
    return curate_benchmark(
        benchmark,
        curator=callback,
        artifact_dir=root,
        curator_identity={"model": "synthetic"},
        **kwargs,
    )


def test_daily_dates_snapshots_and_outcomes_do_not_make_families(tmp_path):
    tasks = [
        task(
            f"daily-{i}",
            f"Daily brief for person {i} on date {i}: calendar and email",
            category="productivity",
            readiness="incomplete",
        )
        for i in range(36)
    ]
    tasks[4]["readiness"] = "ready"
    tasks[4]["inputs"] = {"calendar.txt": "Frozen calendar", "email.txt": "Frozen inbox"}
    tasks[4]["historical"]["success"] = False
    tasks[9]["inputs"] = {"huge.txt": "x" * 9000}
    result = run(bench(tasks), tmp_path, lambda _: pytest.fail("Daily recurrence needs no model"))
    assert result["tasks"] == [tasks[4]]
    summary = result["history"]["semantic_curation"]
    assert summary["original_occurrences"] == 36
    assert summary["family_count"] == 1
    assert len(read_json(summary["occurrences"])["tasks"]) == 36
    assert len(summary["families"][0]["selection"]["cases"]) == 36


def test_global_audit_crosses_batches_and_corrects_local_mistakes(tmp_path):
    tasks = [
        task("mlx-a", "A explain MLX"),
        task("grpo", "B explain GRPO"),
        task("mlx-b", "C explain MLX"),
        task("bug", "D fix parser empty-input crash", category="explanation"),
    ]
    calls = []

    def curator(prompt):
        assert "PRIVATE ORIGINAL ANSWER" not in prompt
        assert "success" not in prompt
        data = payload(prompt)
        calls.append(data)
        if "tasks" in data:
            # Deliberately incorrect local merges must be splittable globally.
            return json.dumps({"families": [family([t["task_id"] for t in data["tasks"]])]})
        return json.dumps(
            {
                "families": [family(["mlx-a", "mlx-b"]), family(["grpo"]), family(["bug"])],
                "audit": {
                    "compared_proposal_ids": [p["proposal_id"] for p in data["proposed_families"]],
                    "rationale": "MLX requests repeat across batches; GRPO algorithm and parser crash are distinct",
                },
            }
        )

    result = run(bench(tasks), tmp_path, curator, batch_size=2)
    assert len(calls) == 3
    assert {t["id"] for t in result["tasks"]} == {"mlx-a", "grpo", "bug"}
    assert result["history"]["semantic_curation"]["audit"]["rationale"].startswith("MLX")
    assert all(t in tasks for t in result["tasks"])


@pytest.mark.parametrize("invalid", ["missing", "duplicate", "unknown", "audit"])
def test_invalid_partition_is_fatal_and_checkpointed(tmp_path, invalid):
    def curator(prompt):
        data = payload(prompt)
        if "tasks" in data:
            return singletons(prompt)
        response = json.loads(singletons(prompt))
        if invalid == "missing":
            response["families"].pop()
        elif invalid == "duplicate":
            response["families"].append(response["families"][0])
        elif invalid == "unknown":
            response["families"][0]["member_ids"] = ["unknown"]
        else:
            response.pop("audit")
        return json.dumps(response)

    with pytest.raises(CurationError) as caught:
        run(bench([task("a"), task("b")]), tmp_path, curator)
    assert caught.value.stage == "curation_incomplete"
    assert caught.value.phase == "global_redundancy_audit"
    assert read_json(caught.value.checkpoint)["stage"] == "curation_incomplete"


def test_resume_reuses_valid_local_calls_but_retries_failed_global(tmp_path):
    source = bench([task("a"), task("b")])
    calls = []

    def broken(prompt):
        calls.append(payload(prompt))
        return singletons(prompt) if "tasks" in payload(prompt) else '{"families":[]}'

    with pytest.raises(CurationError):
        run(source, tmp_path, broken)
    assert len(calls) == 3
    resumed = []

    def fixed(prompt):
        resumed.append(payload(prompt))
        return singletons(prompt)

    result = run(source, tmp_path, fixed, resume=True)
    assert len(resumed) == 1 and "proposed_families" in resumed[0]
    assert len(list((tmp_path / "attempts").glob("*.json"))) == 4
    cached = run(
        source, tmp_path, lambda _: pytest.fail("Completed curator cache must resume"), resume=True
    )
    assert cached["tasks"] == result["tasks"]


def test_evidence_refresh_reselects_original_without_curator_calls(tmp_path):
    source = bench([task("a", "Explain MLX"), task("b", "Explain MLX")])

    def merge(prompt):
        response = json.loads(singletons(prompt))
        response["families"] = [family(["a", "b"])]
        return json.dumps(response)

    first = run(source, tmp_path, merge)
    assert first["tasks"][0]["id"] == "a"
    changed = copy.deepcopy(source)
    changed["tasks"][1]["inputs"] = {"context.txt": "Complete immutable evidence"}
    second = run(
        changed,
        tmp_path,
        lambda _: pytest.fail("Evidence never changes semantic model inputs"),
        resume=True,
    )
    assert second["tasks"] == [changed["tasks"][1]]


def test_daily_split_is_forced_together_but_automation_stays_distinct(tmp_path):
    source = bench(
        [
            task("daily-a", "Daily brief: calendar and email", category="productivity"),
            task("daily-b", "Morning briefing: calendar and inbox", category="session"),
            task(
                "automation", "Implement daily brief calendar email automation", category="coding"
            ),
        ]
    )
    result = run(source, tmp_path)
    assert {t["id"] for t in result["tasks"]} == {"daily-a", "automation"}
    assert result["history"]["semantic_curation"]["daily_brief_occurrences"] == 2


def test_daily_cannot_merge_with_distinct_coding_operation(tmp_path):
    source = bench(
        [
            task("daily", "Daily brief: calendar and email", category="productivity"),
            task("automation", "Implement daily brief automation", category="coding"),
        ]
    )

    def incorrect(prompt):
        result = json.loads(singletons(prompt))
        result["families"] = [family(["daily", "automation"])]
        return json.dumps(result)

    with pytest.raises(CurationError, match="daily"):
        run(source, tmp_path, incorrect)


def test_cache_tampering_and_context_omission_fail(tmp_path):
    source = bench([task("a"), task("b")])
    run(source, tmp_path)
    cache = next((tmp_path / "cache").glob("*.json"))
    cached = read_json(cache)
    cached["response"]["families"][0]["reason"] = "Tampered"
    write_json(cache, cached)
    with pytest.raises(CurationError, match="hash mismatch"):
        run(source, tmp_path, resume=True)
    with pytest.raises(CurationError, match="context bound"):
        run(source, tmp_path / "small", max_prompt_chars=10)


@pytest.mark.parametrize(
    "brief",
    [
        "Implement a daily brief automation that fetches calendar and email and delivers a notification",
        "Explain how daily brief combines calendar and email",
        "Set up a daily brief with calendar and email",
        "Design a daily brief using calendar and email",
        "Research daily brief calendar and email automation",
        "Evaluate daily brief calendar and email quality",
        "Create a daily brief automation using calendar and email",
        "Prepare a design for the daily brief calendar and email tool",
    ],
)
def test_daily_guard_does_not_force_meta_operations_in_productivity_category(brief):
    from personalbench.curation import _daily_brief

    assert not _daily_brief(task("meta", brief, category="productivity"))


@pytest.mark.parametrize("category", ["explanation", "coding", "productivity", "session"])
@pytest.mark.parametrize(
    "brief",
    [
        "Prepare the requested morning brief: summarize calendar and email",
        "Produce the daily-brief automation's morning brief: calendar and inbox",
        "Daily brief: calendar and email",
    ],
)
def test_daily_guard_uses_requested_production_operation_across_categories(category, brief):
    from personalbench.curation import _daily_brief

    assert _daily_brief(task("produce", brief, category=category))
