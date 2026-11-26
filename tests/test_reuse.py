"""Synthetic prior revisions preserve every unchanged outcome, including failures."""

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from personalbench.engine import evaluate
from personalbench.privacy import write_json
from personalbench.runners import run_candidate


def bench(ready_c=False):
    return {
        "schema_version": 1,
        "name": "Synthetic revision",
        "tasks": [
            {
                "id": tid,
                "category": "coding",
                "brief": "Return done",
                "inputs": {"source.txt": "starting input"},
                "criteria": [
                    {
                        "id": "result",
                        "description": "Return done",
                        "kind": "contains",
                        "value": "done",
                    }
                ],
                "reference": {"time_seconds": 1, "user_prompts": 1, "cost_usd": 1},
                "readiness": "ready" if tid != "c" or ready_c else "unsupported",
            }
            for tid in ("a", "b", "c")
        ],
    }


def config():
    return {
        "name": "Synthetic fixture",
        "harness": "fixture",
        "model": "synthetic",
        "responses": {"a": "done", "b": "wrong", "c": "done"},
        "cost_usd": 0.1,
    }


def previous(tmp_path):
    benchmark = bench()
    path = tmp_path / "previous-benchmark.json"
    write_json(path, benchmark)
    report = evaluate(benchmark, config(), tmp_path / "previous")
    return path, tmp_path / "previous/report.json", report


def test_revision_reuses_success_and_failure_and_only_executes_new_ready_goal(tmp_path):
    source_benchmark, source_report, first = previous(tmp_path)
    with patch("personalbench.engine.run_candidate", wraps=run_candidate) as runner:
        result = evaluate(
            bench(True),
            config(),
            tmp_path / "new",
            reuse_report=source_report,
            reuse_benchmark=source_benchmark,
            stop_after_cost_usd=0.05,
        )
    assert [call.args[0]["id"] for call in runner.call_args_list] == ["c"]
    assert result["reuse"]["reused_trials"] == 2
    assert result["trials"][1]["grading"]["success"] is False
    assert result["trials"][0]["output"] == first["trials"][0]["output"]
    assert result["trials"][0]["cost_usd"] == 0.1
    assert result["trials"][0]["reused_from"]["report"] == str(source_report)
    assert result["run_status"] == "complete"
    assert result["summary"]["success_pct"] == pytest.approx(200 / 3)
    with patch("personalbench.engine.run_candidate", side_effect=AssertionError("Duplicate")):
        evaluate(bench(True), config(), tmp_path / "new", resume=True)


@pytest.mark.parametrize("field", ["inputs", "criteria", "reference", "execution_requirements"])
def test_actual_task_change_prevents_reuse_even_if_id_is_unchanged(tmp_path, field):
    source_benchmark, source_report, _ = previous(tmp_path)
    new = bench()
    changed = new["tasks"][0]
    if field == "inputs":
        changed["inputs"]["source.txt"] = "different starting input"
    elif field == "criteria":
        changed["criteria"][0]["value"] = "different expected output"
    elif field == "reference":
        changed["reference"]["time_seconds"] = 2
    else:
        changed["execution_requirements"] = {"fixture": "new environment"}
    with patch("personalbench.engine.run_candidate", wraps=run_candidate) as runner:
        result = evaluate(
            new,
            config(),
            tmp_path / "new",
            reuse_report=source_report,
            reuse_benchmark=source_benchmark,
        )
    assert [call.args[0]["id"] for call in runner.call_args_list] == ["a"]
    assert result["reuse"]["reused_trials"] == 1


@pytest.mark.parametrize("problem", ["benchmark", "configuration", "judge", "workers"])
def test_reuse_refuses_unverified_benchmark_and_changed_setup(tmp_path, problem):
    source_benchmark, source_report, _ = previous(tmp_path)
    current = config()
    kwargs = {}
    if problem == "benchmark":
        original = json.loads(source_benchmark.read_text())
        original["tasks"][0]["brief"] = "tampered"
        write_json(source_benchmark, original)
    elif problem == "configuration":
        current["model"] = "other-synthetic"
    elif problem == "judge":
        kwargs = {"judge_config": config(), "allow_unsafe_local": True}
    else:
        kwargs = {"workers": 2}
    with (
        patch(
            "personalbench.engine.run_candidate", side_effect=AssertionError("Unexpected paid call")
        ),
        pytest.raises(ValueError),
    ):
        evaluate(
            bench(True),
            current,
            tmp_path / "new",
            reuse_report=source_report,
            reuse_benchmark=source_benchmark,
            **kwargs,
        )


def test_native_id_change_reuses_by_full_task_evidence_not_id_alone(tmp_path):
    original = bench()
    original["tasks"] = original["tasks"][:1]
    profile = {"name": "Synthetic native", "harness": "codex", "model": "synthetic"}
    result = {
        "status": "completed",
        "output": "done",
        "usage": {},
        "duration_seconds": 1,
        "user_prompts": 1,
        "cost_usd": 0.1,
        "cost_kind": "synthetic",
    }
    source_benchmark = tmp_path / "original.json"
    write_json(source_benchmark, original)
    with patch(
        "personalbench.engine._version", return_value={"harness": "codex", "version": "synthetic"}
    ):
        with patch("personalbench.engine.run_candidate", return_value=result):
            evaluate(original, profile, tmp_path / "previous", allow_unsafe_local=True)
        new = copy.deepcopy(original)
        new["tasks"][0]["id"] = "renamed"
        with patch(
            "personalbench.engine.run_candidate",
            side_effect=AssertionError("Duplicate native call"),
        ):
            reused = evaluate(
                new,
                profile,
                tmp_path / "new",
                allow_unsafe_local=True,
                reuse_report=tmp_path / "previous/report.json",
                reuse_benchmark=source_benchmark,
            )
    assert reused["trials"][0]["task_id"] == "renamed"
    assert reused["trials"][0]["reused_from"]["task_id"] == "a"
    assert reused["reuse"]["reused_trials"] == 1


def test_reuse_can_verify_comparison_only_policy_upgrade_without_replaying_old_cases(tmp_path):
    from personalbench.implementation import implementation_sources

    old = implementation_sources()
    new = {
        **old,
        "scoring.py": old["scoring.py"] + b"\n\ndef comparison_helper_only():\n    return 1\n",
    }
    with patch("personalbench.engine.implementation_sources", return_value=old):
        source_benchmark, source_report, first = previous(tmp_path)
    with (
        patch("personalbench.engine.implementation_sources", return_value=new),
        patch("personalbench.engine.run_candidate", wraps=run_candidate) as runner,
    ):
        result = evaluate(
            bench(True),
            config(),
            tmp_path / "new",
            reuse_report=source_report,
            reuse_benchmark=source_benchmark,
        )
    assert [call.args[0]["id"] for call in runner.call_args_list] == ["c"]
    migrated = json.loads(source_report.read_text())
    assert migrated["trials"] == first["trials"]
    assert len(migrated["implementation_migrations"]) == 1
    assert migrated["scoring_hash"] == result["scoring_hash"]
    snapshot = json.loads(Path(result["reuse"]["source_report_snapshot"]).read_text())
    assert snapshot["trials"] == first["trials"]
