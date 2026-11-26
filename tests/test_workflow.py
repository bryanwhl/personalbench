"""Synthetic end-to-end workflow tests never use personal logs or model accounts."""

import json
from unittest.mock import patch

import pytest

from personalbench.workflow import evaluate_history


def source(root):
    path = root / "synthetic.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {
                    "type": "event_msg",
                    "timestamp": "2026-11-25T10:00:00Z",
                    "payload": {"type": "user_message", "message": "Explain a stack"},
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-11-25T10:00:01Z",
                    "payload": {"type": "agent_message", "message": "PRIVATE ORIGINAL ANSWER"},
                },
            ]
        )
    )
    return path


def callback(config, **kwargs):
    def call(prompt):
        if config["name"] == "compiler":
            return json.dumps(
                {
                    "tasks": [
                        {
                            "id": "stack",
                            "category": "explanation",
                            "brief": "Explain a stack",
                            "source_message_indices": [0, 1],
                            "requires_starting_state": False,
                            "source_evidence_ids": [],
                            "criteria": [
                                {"id": "lifo", "description": "Explains last in first out"}
                            ],
                        }
                    ],
                    "exclusions": [],
                }
            )
        payload = json.loads(prompt.split("\n", 1)[1])
        return json.dumps(
            {
                "scores": [
                    {
                        "id": criterion["id"],
                        "score": 1,
                        "evidence": "Synthetic candidate satisfies this requested criterion",
                    }
                    for criterion in payload["criteria"]
                ]
            }
        )

    return call


def candidate(task, config, workspace, **kwargs):
    assert "PRIVATE ORIGINAL ANSWER" not in task["brief"]
    assert "PRIVATE ORIGINAL ANSWER" not in json.dumps(task.get("inputs", {}))
    return {
        "status": "completed",
        "output": "Last in first out",
        "duration_seconds": 1,
        "user_prompts": 1,
        "usage": {},
        "cost_usd": None,
        "cost_kind": "unknown",
        "error": None,
    }


def config(name):
    return {"name": name, "harness": "fixture", "model": "synthetic-model"}


def test_workflow_freezes_prepares_runs_and_resumes_without_calls(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    path = source(sessions)
    output = tmp_path / "evaluation"
    kwargs = {
        "compiler_config": config("compiler"),
        "judge_config": config("judge"),
        "roots": [sessions],
        "as_of": "2026-11-26T20:00:00Z",
        "allow_unsafe_local": True,
        "workers": 2,
    }
    with (
        patch("personalbench.workflow.model_callback", side_effect=callback),
        patch("personalbench.engine.model_callback", side_effect=callback),
        patch("personalbench.engine.run_candidate", side_effect=candidate) as invoked,
    ):
        first = evaluate_history(output, config("candidate"), **kwargs)
        assert invoked.call_count == 1
        assert first["report"]["summary"]["success_pct"] == 100
    assert (output / "inventory.json").exists()
    assert (output / "run/report.html").exists()
    path.write_text("source changed after frozen evaluation")
    with (
        patch("personalbench.workflow.model_callback", side_effect=AssertionError("recompiled")),
        patch("personalbench.engine.run_candidate", side_effect=AssertionError("repeated attempt")),
    ):
        resumed = evaluate_history(output, config("candidate"), resume=True, **kwargs)
    assert resumed["report"]["trials"] == first["report"]["trials"]
    from personalbench.cli import main

    for name in ("candidate", "compiler", "judge"):
        (tmp_path / f"{name}.json").write_text(json.dumps(config(name)))
    assert (
        main(
            [
                "evaluate",
                "--config",
                str(tmp_path / "candidate.json"),
                "--compiler",
                str(tmp_path / "compiler.json"),
                "--judge",
                str(tmp_path / "judge.json"),
                "--session-root",
                str(sessions),
                "--as-of",
                kwargs["as_of"],
                "--output",
                str(output),
                "--workers",
                "2",
                "--resume",
                "--allow-unsafe-local",
            ]
        )
        == 0
    )
    with pytest.raises(ValueError, match="changed configuration"):
        evaluate_history(output, config("different"), resume=True, **kwargs)


def test_workflow_requires_explicit_trusted_execution(tmp_path):
    with pytest.raises(ValueError, match="allow-unsafe-local"):
        evaluate_history(tmp_path / "no-run", config("candidate"))


def test_preparation_failure_is_resumable_without_starting_candidate(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    source(sessions)
    output = tmp_path / "evaluation"
    kwargs = {
        "compiler_config": config("compiler"),
        "judge_config": config("judge"),
        "roots": [sessions],
        "as_of": "2026-11-26T20:00:00Z",
        "allow_unsafe_local": True,
    }
    with (
        patch("personalbench.workflow.model_callback", return_value=lambda _: "invalid json"),
        patch(
            "personalbench.engine.run_candidate",
            side_effect=AssertionError("candidate before preparation"),
        ),
    ):
        failed = evaluate_history(output, config("candidate"), **kwargs)
    assert failed["report"] is None
    assert failed["workflow"]["stage"] == "preparation_incomplete"
    assert not (output / "benchmark.json").exists()
    with (
        patch("personalbench.workflow.model_callback", side_effect=callback),
        patch("personalbench.engine.model_callback", side_effect=callback),
        patch("personalbench.engine.run_candidate", side_effect=candidate),
    ):
        repaired = evaluate_history(output, config("candidate"), resume=True, **kwargs)
    assert repaired["report"]["summary"]["success_pct"] == 100


def test_invalid_limits_fail_before_reading_sources_or_creating_output(tmp_path):
    with pytest.raises(ValueError, match="workers"):
        evaluate_history(
            tmp_path / "untouched", config("candidate"), workers=0, allow_unsafe_local=True
        )
    assert not (tmp_path / "untouched").exists()


def test_refresh_revalidates_recovery_without_repeating_compilation_or_overwriting_run(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    source(sessions)
    output = tmp_path / "evaluation"
    kwargs = {
        "compiler_config": config("compiler"),
        "judge_config": config("judge"),
        "roots": [sessions],
        "as_of": "2026-11-26T20:00:00Z",
        "allow_unsafe_local": True,
    }
    with (
        patch("personalbench.workflow.model_callback", side_effect=callback),
        patch("personalbench.engine.model_callback", side_effect=callback),
        patch("personalbench.engine.run_candidate", side_effect=candidate),
    ):
        evaluate_history(output, config("candidate"), **kwargs)
    original = (output / "run/report.json").read_bytes()

    def corrected_recovery(task, evidence, messages):
        import copy

        task = copy.deepcopy(task)
        task["readiness"] = "incomplete"
        task["provenance"]["readiness_reason"] = "Synthetic missing historical file detected"
        return task

    with (
        patch(
            "personalbench.workflow.model_callback",
            return_value=lambda _: (_ for _ in ()).throw(AssertionError("recompiled")),
        ),
        patch("personalbench.recovery.recover_task_inputs", side_effect=corrected_recovery),
        patch(
            "personalbench.engine.run_candidate", side_effect=AssertionError("ran unavailable goal")
        ),
    ):
        refreshed = evaluate_history(
            output, config("candidate"), resume=True, refresh_preparation=True, **kwargs
        )
    assert refreshed["workflow"]["revision"] == 2
    assert refreshed["report"]["summary"]["coverage"]["scored_trials"] == 0
    assert (output / "run/report.json").read_bytes() == original
    assert (output / "run.v2/report.json").exists()


def test_default_assessors_do_not_inherit_candidate_answer_instructions():
    from personalbench.workflow import _assessor_config

    candidate_config = {
        **config("candidate"),
        "instructions": "Return only a JSON array",
        "tools": "workspace",
        "sandbox": "workspace-write",
    }
    isolated = _assessor_config(candidate_config, None, "judge")
    assert "instructions" not in isolated
    assert isolated["model"] == candidate_config["model"]
    assert isolated["tools"] == "none"
    assert isolated["sandbox"] == "read-only"
    explicit = {**config("judge"), "instructions": "Use a strict assessment rubric"}
    assert (
        _assessor_config(candidate_config, explicit, "judge")["instructions"]
        == explicit["instructions"]
    )


def test_ready_infrastructure_error_is_nonzero_incomplete_and_resumable(tmp_path):
    from personalbench.cli import main

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    source(sessions)
    output = tmp_path / "evaluation"
    kwargs = {
        "compiler_config": config("compiler"),
        "judge_config": config("judge"),
        "roots": [sessions],
        "as_of": "2026-11-26T20:00:00Z",
        "allow_unsafe_local": True,
    }
    error = {
        "status": "infrastructure_error",
        "output": "",
        "duration_seconds": 1,
        "user_prompts": 1,
        "usage": {},
        "cost_usd": None,
        "cost_kind": "unknown",
        "error": "Synthetic transient transport failure",
    }
    with (
        patch("personalbench.workflow.model_callback", side_effect=callback),
        patch("personalbench.engine.model_callback", side_effect=callback),
        patch("personalbench.engine.run_candidate", return_value=error),
    ):
        first = evaluate_history(output, config("candidate"), **kwargs)
    assert first["report"]["run_status"] == "complete"
    assert first["workflow"]["stage"] == "evaluation_incomplete"
    assert len(first["workflow"]["evaluation_gaps"]) == 1
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(config("candidate")))
    with patch("personalbench.cli.evaluate_history", return_value=first):
        assert (
            main(
                [
                    "evaluate",
                    "--config",
                    str(candidate_path),
                    "--output",
                    str(output),
                    "--allow-unsafe-local",
                    "--resume",
                ]
            )
            == 2
        )
    with (
        patch(
            "personalbench.workflow.model_callback",
            side_effect=AssertionError("Recompiled frozen task"),
        ),
        patch("personalbench.engine.model_callback", side_effect=callback),
        patch("personalbench.engine.run_candidate", side_effect=candidate) as invoked,
    ):
        repaired = evaluate_history(output, config("candidate"), resume=True, **kwargs)
    assert invoked.call_count == 1
    assert repaired["workflow"]["stage"] == "complete"
    assert repaired["workflow"]["evaluation_gaps"] == []
    assert len(repaired["report"]["retry_history"]) == 1
    assert repaired["report"]["trials"][0]["user_prompts"] == 2


def test_refresh_does_not_reuse_stale_non_immediate_revision(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    source(sessions)
    output = tmp_path / "evaluation"
    kwargs = {
        "compiler_config": config("compiler"),
        "judge_config": config("judge"),
        "roots": [sessions],
        "as_of": "2026-11-26T20:00:00Z",
        "allow_unsafe_local": True,
    }
    with (
        patch("personalbench.workflow.model_callback", side_effect=callback),
        patch("personalbench.engine.model_callback", side_effect=callback),
        patch("personalbench.engine.run_candidate", side_effect=candidate),
    ):
        evaluate_history(output, config("candidate"), **kwargs)

    def readiness(state):
        def recover(task, evidence, messages):
            import copy

            result = copy.deepcopy(task)
            result["readiness"] = state
            result["provenance"]["readiness_reason"] = "Synthetic preparation change"
            return result

        return recover

    with (
        patch(
            "personalbench.workflow.model_callback",
            return_value=lambda _: (_ for _ in ()).throw(AssertionError("Recompiled cached task")),
        ),
        patch("personalbench.recovery.recover_task_inputs", side_effect=readiness("incomplete")),
        patch(
            "personalbench.engine.run_candidate",
            side_effect=AssertionError("Executed unavailable goal"),
        ),
    ):
        second = evaluate_history(
            output, config("candidate"), resume=True, refresh_preparation=True, **kwargs
        )
    assert second["workflow"]["reused_evidence_from"]["report"] == str(output / "run/report.json")
    previous_path = output / "run.v2/report.json"
    previous = json.loads(previous_path.read_text())
    previous["run_status"] = "budget_stopped"
    previous_path.write_text(json.dumps(previous))
    with (
        patch(
            "personalbench.workflow.model_callback",
            return_value=lambda _: (_ for _ in ()).throw(AssertionError("Recompiled cached task")),
        ),
        patch("personalbench.recovery.recover_task_inputs", side_effect=readiness("unsupported")),
        patch(
            "personalbench.engine.run_candidate",
            side_effect=AssertionError("Executed unavailable goal"),
        ) as invoked,
    ):
        third = evaluate_history(
            output, config("candidate"), resume=True, refresh_preparation=True, **kwargs
        )
    assert invoked.call_count == 0
    assert third["workflow"]["revision"] == 3
    assert third["workflow"].get("reused_evidence_from") is None
    assert third["report"].get("reuse") is None


def test_run_cli_accepts_fixed_preparation_gaps_but_rejects_ungraded_ready_tasks(tmp_path):
    from personalbench.cli import main

    benchmark = {
        "schema_version": 1,
        "name": "Synthetic mixed readiness",
        "tasks": [
            {
                "id": tid,
                "category": "coding",
                "brief": "Return done",
                "readiness": state,
                "criteria": [
                    {
                        "id": "result",
                        "kind": "contains",
                        "description": "Return done",
                        "value": "done",
                    }
                ],
                "historical": {"output": "done"} if tid == "a" else {},
            }
            for tid, state in [("a", "ready"), ("b", "unsupported")]
        ],
    }
    benchmark_path = tmp_path / "benchmark.json"
    benchmark_path.write_text(json.dumps(benchmark))
    candidate_path = tmp_path / "candidate.json"
    candidate_config = {
        "name": "Synthetic fixture",
        "harness": "fixture",
        "model": "synthetic",
        "responses": {"a": "done"},
    }
    candidate_path.write_text(json.dumps(candidate_config))
    output = tmp_path / "valid"
    args = ["run", str(benchmark_path), "--config", str(candidate_path), "--output", str(output)]
    assert main(args) == 0
    with patch(
        "personalbench.engine.run_candidate",
        side_effect=AssertionError("Repeated completed candidate"),
    ):
        assert main([*args, "--resume"]) == 0
    # A fully graded wrong answer is a valid benchmark result, not an operational error.
    candidate_config["responses"]["a"] = "wrong"
    candidate_path.write_text(json.dumps(candidate_config))
    assert (
        main(
            [
                "run",
                str(benchmark_path),
                "--config",
                str(candidate_path),
                "--output",
                str(tmp_path / "graded-failure"),
            ]
        )
        == 0
    )
    # Missing fixture response simulates an ungraded ready-candidate infrastructure error.
    candidate_config["responses"] = {}
    candidate_path.write_text(json.dumps(candidate_config))
    assert (
        main(
            [
                "run",
                str(benchmark_path),
                "--config",
                str(candidate_path),
                "--output",
                str(tmp_path / "operational-failure"),
            ]
        )
        == 2
    )
    # Recorded grading still requires evidence for every declared task.
    assert (
        main(["grade-recorded", str(benchmark_path), "--output", str(tmp_path / "recorded")]) == 2
    )


@pytest.mark.parametrize("already_pending", [False, True])
def test_scoring_contract_change_runs_fresh_without_reusing_incompatible_policy(
    tmp_path, already_pending
):
    from personalbench import workflow

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    source(sessions)
    output = tmp_path / "evaluation"
    kwargs = {
        "compiler_config": config("compiler"),
        "judge_config": config("judge"),
        "roots": [sessions],
        "as_of": "2026-11-26T20:00:00Z",
        "allow_unsafe_local": True,
    }
    with (
        patch("personalbench.workflow.model_callback", side_effect=callback),
        patch("personalbench.engine.model_callback", side_effect=callback),
        patch("personalbench.engine.run_candidate", side_effect=candidate),
    ):
        initial = evaluate_history(output, config("candidate"), **kwargs)
    previous_benchmark = output / "benchmark.json"
    original_benchmark = previous_benchmark.read_bytes()
    previous_report = output / "run/report.json"
    original_report = previous_report.read_bytes()
    old_criteria = initial["report"]["task_manifest"][0]["criteria"]
    real_build = workflow.build_history

    def new_contract(*args, **build_kwargs):
        benchmark = real_build(*args, **build_kwargs)
        benchmark["scoring"]["success_contract"] = "synthetic-new-contract"
        return benchmark

    if already_pending:
        # Reproduce a refresh that wrote the revision manifest/benchmark, then
        # failed before a run report could be created due to incompatible reuse.
        new_benchmark = json.loads(original_benchmark)
        new_benchmark["scoring"]["success_contract"] = "synthetic-new-contract"
        (output / "benchmark.v2.json").write_text(json.dumps(new_benchmark))
        manifest = json.loads((output / "workflow.json").read_text())
        manifest.update(
            benchmark_path="benchmark.v2.json",
            run_directory="run.v2",
            revision=2,
            stage="evaluation",
            reused_evidence_from={
                "report": str(previous_report),
                "benchmark": str(previous_benchmark),
            },
        )
        (output / "workflow.json").write_text(json.dumps(manifest))
    with (
        patch("personalbench.workflow.build_history", side_effect=new_contract),
        patch("personalbench.workflow.model_callback", side_effect=callback),
        patch("personalbench.engine.model_callback", side_effect=callback),
        patch("personalbench.engine.run_candidate", side_effect=candidate) as invoked,
    ):
        result = evaluate_history(
            output,
            config("candidate"),
            resume=True,
            refresh_preparation=not already_pending,
            **kwargs,
        )
    assert invoked.call_count == 1
    assert result["workflow"]["stage"] == "complete"
    assert result["workflow"].get("reused_evidence_from") is None
    assert "Scoring contract changed" in result["workflow"]["reuse_skipped"]["reason"]
    assert result["report"].get("reuse") is None
    assert result["report"]["task_manifest"][0]["criteria"] == old_criteria
    assert previous_report.read_bytes() == original_report
    assert previous_benchmark.read_bytes() == original_benchmark


def test_refresh_discards_native_evidence_from_a_legacy_input_boundary(tmp_path):
    from personalbench import workflow

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    source(sessions)
    output = tmp_path / "evaluation"
    kwargs = {
        "compiler_config": config("compiler"),
        "judge_config": config("judge"),
        "roots": [sessions],
        "as_of": "2026-11-26T20:00:00Z",
        "allow_unsafe_local": True,
    }
    with (
        patch("personalbench.workflow.model_callback", side_effect=callback),
        patch("personalbench.engine.model_callback", side_effect=callback),
        patch("personalbench.engine.run_candidate", side_effect=candidate),
    ):
        evaluate_history(output, config("candidate"), **kwargs)
    original_report = (output / "run/report.json").read_bytes()
    original_benchmark = (output / "benchmark.json").read_bytes()
    real_build = workflow.build_history

    def refreshed(*args, **build_kwargs):
        benchmark = real_build(*args, **build_kwargs)
        benchmark["tasks"][0]["brief"] += " Use the preserved inputs only."
        return benchmark

    def boundary(configuration):
        return (
            {"input_boundary": "synthetic-confined-v1"}
            if configuration["name"] == "candidate"
            else {}
        )

    with (
        patch("personalbench.workflow.build_history", side_effect=refreshed),
        patch("personalbench.workflow.adapter_identity", side_effect=boundary),
        patch("personalbench.workflow.model_callback", side_effect=callback),
        patch("personalbench.engine.model_callback", side_effect=callback),
        patch("personalbench.engine.run_candidate", side_effect=candidate) as invoked,
    ):
        result = evaluate_history(
            output, config("candidate"), resume=True, refresh_preparation=True, **kwargs
        )
    assert invoked.call_count == 1
    assert result["workflow"]["stage"] == "complete"
    assert result["workflow"]["reuse_skipped"]["roles"] == ["candidate"]
    assert "input boundary changed" in result["workflow"]["reuse_skipped"]["reason"]
    assert result["report"].get("reuse") is None
    assert (output / "run/report.json").read_bytes() == original_report
    assert (output / "benchmark.json").read_bytes() == original_benchmark
