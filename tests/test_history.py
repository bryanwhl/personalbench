"""History coverage checks using synthetic session records, never personal transcripts."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from personalbench.cli import main, parser
from personalbench.history import build_history, discover_sessions, parse_as_of

NOW = datetime(2026, 11, 26, 20, 0, tzinfo=UTC)


def session(root, name, stamp, request="Explain a stack", response="Last in first out"):
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {
                    "type": "event_msg",
                    "timestamp": stamp,
                    "payload": {"type": "user_message", "message": request},
                },
                {
                    "type": "event_msg",
                    "timestamp": stamp,
                    "payload": {"type": "agent_message", "message": response},
                },
            ]
        )
    )
    return path


def compiler(_):
    return json.dumps(
        {
            "tasks": [
                {
                    "id": "stack",
                    "category": "explanation",
                    "brief": "Explain a stack",
                    "requires_starting_state": False,
                    "source_message_indices": [0, 1],
                    "criteria": [{"id": "lifo", "description": "Explains last in first out"}],
                }
            ]
        }
    )


def test_window_uses_recorded_activity_not_filename_or_mtime(tmp_path):
    old_name = session(tmp_path, "rollout-2026-08-20.jsonl", "2026-11-25T10:00:00Z")
    os.utime(old_name, (1, 1))
    stale = session(tmp_path, "copied-today.jsonl", "2026-09-01T10:00:00Z")
    future = session(tmp_path, "future.jsonl", "2026-12-01T10:00:00Z")
    undated = session(tmp_path, "undated.jsonl", "not a timestamp")
    result = discover_sessions(roots=[tmp_path], as_of=NOW)
    by_name = {row["path"].split("/")[-1]: row for row in result["sessions"]}
    assert by_name[old_name.name]["status"] == "selected"
    assert by_name[stale.name]["reason"] == "outside history window"
    assert by_name[future.name]["reason"] == "activity after as-of"
    assert "timestamp" in by_name[undated.name]["reason"]


def test_history_deduplicates_transcripts_and_audits_failures(tmp_path):
    first = session(tmp_path, "first.jsonl", "2026-11-25T10:00:00Z")
    second = session(tmp_path, "fork.jsonl", "2026-11-26T10:00:00Z")
    empty = tmp_path / "empty.jsonl"
    empty.write_text(json.dumps({"type": "event_msg", "timestamp": "2026-11-25T10:00:00Z"}))
    malformed = tmp_path / "broken.jsonl"
    malformed.write_text("{")
    inventory = discover_sessions(roots=[tmp_path], as_of=NOW)
    checkpoint = []
    result = build_history(
        inventory,
        compiler=compiler,
        artifact_dir=tmp_path / "private",
        progress=lambda row: checkpoint.append(row["status"]),
    )
    assert len(result["tasks"]) == 1
    assert result["tasks"][0]["readiness"] == "ready"
    entries = {row["path"]: row for row in inventory["sessions"]}
    assert {entries[str(first)]["status"], entries[str(second)]["status"]} == {
        "compiled",
        "duplicate",
    }
    assert entries[str(empty)]["status"] == "excluded"
    assert entries[str(malformed)]["status"] == "error"
    assert set(checkpoint) == {"prepared", "compiled", "duplicate"}
    compiled = next(row for row in entries.values() if row["status"] == "compiled")
    assert json.loads(Path(compiled["normalized_session"]).read_text())["messages"]
    assert json.loads(Path(compiled["compiled_benchmark"]).read_text())["tasks"]


def test_distinct_sessions_keep_unique_goal_ids_and_missing_inputs(tmp_path):
    session(tmp_path, "stack.jsonl", "2026-11-25T10:00:00Z", "Explain stack")
    session(tmp_path, "parser.jsonl", "2026-11-24T10:00:00Z", "Fix parser")
    result = build_history(discover_sessions(roots=[tmp_path], as_of=NOW))
    assert len(result["tasks"]) == 2
    assert len({task["id"] for task in result["tasks"]}) == 2
    assert all(task["readiness"] == "incomplete" for task in result["tasks"])
    assert all(task["provenance"]["source_path"] for task in result["tasks"])


def test_same_compiled_task_is_deduplicated_with_source_provenance(tmp_path):
    session(tmp_path, "stack.jsonl", "2026-11-25T10:00:00Z", "Explain stack")
    session(tmp_path, "stack-again.jsonl", "2026-11-24T10:00:00Z", "Explain stack please")
    result = build_history(discover_sessions(roots=[tmp_path], as_of=NOW), compiler=compiler)
    assert len(result["tasks"]) == 1
    assert len(result["tasks"][0]["provenance"]["duplicate_sources"]) == 1
    assert sum(len(row.get("duplicate_tasks", [])) for row in result["history"]["sessions"]) == 1


def test_exclusions_and_explicit_roots_do_not_expand_scope(tmp_path):
    selected = session(tmp_path / "scope", "selected.jsonl", "2026-11-25T10:00:00Z")
    session(tmp_path / "elsewhere", "other.jsonl", "2026-11-25T10:00:00Z")
    result = discover_sessions(roots=[selected.parent], as_of=NOW, exclude=[selected])
    assert len(result["sessions"]) == 1
    assert result["sessions"][0]["reason"] == "explicit exclusion"


def test_cli_default_build_is_sixty_day_corpus_with_audit(tmp_path, capsys):
    root = tmp_path / "sessions"
    session(root, "selected.jsonl", "2026-11-25T10:00:00Z")
    out = tmp_path / "out.json"
    assert (
        main(
            ["build", "--session-root", str(root), "--as-of", NOW.isoformat(), "--output", str(out)]
        )
        == 0
    )
    result = json.loads(out.read_text())
    assert result["history"]["days"] == 60
    assert out.with_suffix(".inventory.json").exists()
    assert parser().parse_args(["sessions"]).source == "all"
    summary = json.loads(capsys.readouterr().out)
    assert summary["history"]["sessions"] == {"compiled": 1}
    assert summary["preparation"] == "fallback_draft"


@pytest.mark.parametrize("days", [0, -1, True, 1.5])
def test_invalid_history_window_rejected(days):
    with pytest.raises(ValueError, match="positive integer"):
        discover_sessions(days=days, roots=[])


def test_as_of_requires_timezone_and_empty_history_remains_explicit():
    with pytest.raises(ValueError, match="timezone"):
        parse_as_of("2026-11-26")
    with pytest.raises(ValueError, match="No tasks"):
        build_history({"sessions": []})


def test_cli_keeps_inventory_when_all_sessions_fail(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    (root / "empty.jsonl").write_text(
        json.dumps({"type": "event_msg", "timestamp": "2026-11-25T10:00:00Z"})
    )
    out = tmp_path / "out.json"
    assert (
        main(
            ["build", "--session-root", str(root), "--as-of", NOW.isoformat(), "--output", str(out)]
        )
        == 2
    )
    inventory = json.loads(out.with_suffix(".inventory.json").read_text())
    assert inventory["sessions"][0]["status"] == "excluded"
    assert not out.exists()


def test_cli_explicit_session_overrides_corpus_discovery(tmp_path):
    path = session(tmp_path, "chosen.jsonl", "2020-01-01T10:00:00Z", "Explain queues")
    out = tmp_path / "out.json"
    assert main(["build", str(path), "--output", str(out)]) == 0
    result = json.loads(out.read_text())
    assert len(result["tasks"]) == 1
    assert "Explain queues" in result["tasks"][0]["brief"]
    assert "history" not in result


def test_old_goals_in_resumed_session_are_context_not_recent_tasks(tmp_path):
    path = session(tmp_path, "resumed.jsonl", "2026-08-01T10:00:00Z", "Explain stack")
    with path.open("a") as stream:
        for row in [
            {
                "type": "event_msg",
                "timestamp": "2026-11-25T10:00:00Z",
                "payload": {"type": "user_message", "message": "Explain queue"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-11-25T10:00:01Z",
                "payload": {"type": "agent_message", "message": "First in first out"},
            },
        ]:
            stream.write("\n" + json.dumps(row))

    def split(prompt):
        assert "Explain stack" in prompt  # earlier context is available to the compiler
        return json.dumps(
            {
                "tasks": [
                    {
                        "id": name,
                        "brief": "Explain " + name,
                        "requires_starting_state": False,
                        "source_message_indices": indices,
                        "criteria": [{"id": "ok", "description": "Accurate explanation"}],
                    }
                    for name, indices in [("stack", [0, 1]), ("queue", [2, 3])]
                ]
            }
        )

    result = build_history(discover_sessions(roots=[tmp_path], as_of=NOW), compiler=split)
    assert len(result["tasks"]) == 1
    assert result["tasks"][0]["brief"] == "Explain queue"
    assert result["history"]["sessions"][0]["excluded_tasks"][0]["source_task_id"] == "stack"


def test_as_of_freezes_resumed_chat_and_prevents_future_answer_leakage(tmp_path):
    path = session(tmp_path / "logs", "resumed.jsonl", "2026-11-25T10:00:00Z")
    with path.open("a") as stream:
        stream.write(
            "\n"
            + json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": "2026-12-01T10:00:00Z",
                    "payload": {"type": "user_message", "message": "FUTURE_ANSWER_MUST_NOT_LEAK"},
                }
            )
        )
    inventory = discover_sessions(
        roots=[path.parent], as_of=NOW, snapshot_dir=tmp_path / "snapshots"
    )
    assert inventory["sessions"][0]["status"] == "selected"
    # The source can change after discovery without modifying the immutable snapshot.
    path.write_text("live source has been replaced")

    def frozen_compiler(prompt):
        assert "FUTURE_ANSWER_MUST_NOT_LEAK" not in prompt
        assert "live source has been replaced" not in prompt
        return compiler(prompt)

    result = build_history(inventory, compiler=frozen_compiler, artifact_dir=tmp_path / "artifacts")
    assert result["tasks"][0]["readiness"] == "ready"
    assert (
        "FUTURE_ANSWER_MUST_NOT_LEAK"
        not in Path(inventory["sessions"][0]["snapshot_path"]).read_text()
    )


def test_metadata_only_recent_activity_does_not_expand_user_window(tmp_path):
    path = session(tmp_path, "old.jsonl", "2026-08-01T10:00:00Z")
    with path.open("a") as stream:
        stream.write(
            "\n"
            + json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": NOW.isoformat(),
                    "payload": {"type": "token_count"},
                }
            )
        )
    inventory = discover_sessions(roots=[tmp_path], as_of=NOW)
    assert inventory["sessions"][0]["reason"] == "outside history window"


def test_source_drift_is_audited_when_discovery_did_not_save_snapshot(tmp_path):
    path = session(tmp_path / "logs", "selected.jsonl", "2026-11-25T10:00:00Z")
    inventory = discover_sessions(roots=[path.parent], as_of=NOW)
    path.write_text(path.read_text() + "\n{}")
    with pytest.raises(ValueError, match="No tasks"):
        build_history(inventory, artifact_dir=tmp_path / "artifacts")
    assert inventory["sessions"][0]["error_stage"] == "integrity"
    assert "Source changed after discovery" in inventory["sessions"][0]["error"]


def test_cached_preparation_resumes_without_new_model_call_or_identity_change(tmp_path):
    path = session(tmp_path / "logs", "selected.jsonl", "2026-11-25T10:00:00Z")
    inventory = discover_sessions(
        roots=[path.parent], as_of=NOW, snapshot_dir=tmp_path / "snapshots"
    )
    calls = []

    def counted(prompt):
        calls.append(prompt)
        return compiler(prompt)

    kwargs = {
        "artifact_dir": tmp_path / "artifacts",
        "compiler": counted,
        "compiler_identity": {"model": "synthetic", "version": 1},
    }
    first = build_history(inventory, **kwargs)
    first_saved = json.loads(json.dumps(first))
    path.unlink()
    resumed = build_history(inventory, resume=True, **kwargs)
    assert len(calls) == 1
    assert resumed == first_saved


def test_failed_compiler_is_retried_from_snapshot_on_resume(tmp_path):
    path = session(tmp_path / "logs", "selected.jsonl", "2026-11-25T10:00:00Z")
    inventory = discover_sessions(
        roots=[path.parent], as_of=NOW, snapshot_dir=tmp_path / "snapshots"
    )

    def broken(_):
        raise RuntimeError("Transient compiler failure")

    kwargs = {"artifact_dir": tmp_path / "artifacts", "compiler_identity": "synthetic-v1"}
    with pytest.raises(ValueError, match="No tasks"):
        build_history(inventory, compiler=broken, **kwargs)
    assert inventory["sessions"][0]["error_stage"] == "preparation"
    path.unlink()
    result = build_history(inventory, compiler=compiler, resume=True, **kwargs)
    assert len(result["tasks"]) == 1
    assert "error" not in inventory["sessions"][0]


def test_literal_inherited_prefix_is_covered_by_complete_source_before_compilation(tmp_path):
    short = session(tmp_path / "logs", "short.jsonl", "2026-11-25T10:00:00Z")
    long = short.parent / "long.jsonl"
    long.write_text(
        short.read_text()
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "timestamp": "2026-11-25T11:00:00Z",
                "payload": {"type": "user_message", "message": "Also explain queues"},
            }
        )
    )
    calls = []

    def complete(prompt):
        calls.append(prompt)
        result = json.loads(compiler(prompt))
        result["tasks"][0]["source_message_indices"] = [0, 1, 2]
        return json.dumps(result)

    inventory = discover_sessions(roots=[short.parent], as_of=NOW)
    result = build_history(inventory, compiler=complete)
    assert len(calls) == 1
    assert len(result["tasks"]) == 1
    assert (
        next(row for row in inventory["sessions"] if row["path"] == str(short))["status"]
        == "covered_by"
    )


def test_identical_daily_briefs_on_different_dates_are_not_merged(tmp_path):
    session(tmp_path, "first.jsonl", "2026-11-25T10:00:00Z", "Summarize today's inbox")
    session(tmp_path, "second.jsonl", "2026-11-26T10:00:00Z", "Summarize today's inbox")
    result = build_history(discover_sessions(roots=[tmp_path], as_of=NOW), compiler=compiler)
    assert len(result["tasks"]) == 2


def test_internal_agent_threads_are_excluded_as_nonhuman_sources(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    path = session(tmp_path, "child.jsonl", "2026-11-25T10:00:00Z")
    metadata = {
        "payload": {
            "id": "child",
            "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}},
        },
    }
    metadata["type"] = "session_meta"
    path.write_text(json.dumps(metadata) + "\n" + path.read_text())
    inventory = discover_sessions(roots=[tmp_path], as_of=NOW)
    assert inventory["sessions"][0]["status"] == "excluded"
    assert "internal subagent" in inventory["sessions"][0]["reason"]


def test_invalid_compiler_response_gets_one_bounded_repair_then_cached_resume(tmp_path):
    path = session(tmp_path / "logs", "selected.jsonl", "2026-11-25T10:00:00Z")
    inventory = discover_sessions(
        roots=[path.parent], as_of=NOW, snapshot_dir=tmp_path / "snapshots"
    )
    calls = []

    def initially_invalid(prompt):
        calls.append(prompt)
        return '{"tasks": ["invalid"]}' if len(calls) == 1 else compiler(prompt)

    kwargs = {
        "compiler": initially_invalid,
        "compiler_identity": "synthetic",
        "artifact_dir": tmp_path / "artifacts",
    }
    result = build_history(inventory, **kwargs)
    assert len(result["tasks"]) == 1
    assert len(calls) == 2
    assert "Validation error:" in calls[1]
    assert inventory["sessions"][0]["validation_attempts"] == 2
    build_history(inventory, resume=True, **kwargs)
    assert len(calls) == 2


def test_provider_failure_does_not_trigger_schema_repair(tmp_path):
    from personalbench.runners import RunnerError

    path = session(tmp_path / "logs", "selected.jsonl", "2026-11-25T10:00:00Z")
    inventory = discover_sessions(roots=[path.parent], as_of=NOW)
    calls = []

    def unavailable(prompt):
        calls.append(prompt)
        raise RunnerError("Authentication unavailable")

    with pytest.raises(ValueError, match="No tasks"):
        build_history(inventory, compiler=unavailable)
    assert len(calls) == 1
    assert inventory["sessions"][0]["error_stage"] == "preparation"


def test_resume_includes_prepared_checkpoint_before_consolidation(tmp_path):
    path = session(tmp_path / "logs", "selected.jsonl", "2026-11-25T10:00:00Z")
    inventory = discover_sessions(
        roots=[path.parent], as_of=NOW, snapshot_dir=tmp_path / "snapshots"
    )
    kwargs = {
        "compiler": compiler,
        "compiler_identity": "synthetic",
        "artifact_dir": tmp_path / "artifacts",
    }
    build_history(inventory, **kwargs)
    inventory["sessions"][0]["status"] = "prepared"
    result = build_history(inventory, resume=True, **kwargs)
    assert len(result["tasks"]) == 1
    assert inventory["sessions"][0]["status"] == "compiled"


def test_summary_only_exports_are_excluded_but_unknown_records_remain_errors(tmp_path):
    (tmp_path / "summary.jsonl").write_text(
        json.dumps(
            {
                "type": "summary",
                "summary": "Synthetic display metadata",
                "leafUuid": "synthetic-id",
            }
        )
    )
    (tmp_path / "unknown.jsonl").write_text(
        json.dumps(
            {
                "type": "unknown-export",
                "timestamp": NOW.isoformat(),
                "user_text": "Preserve this goal",
            }
        )
    )
    entries = {
        Path(row["path"]).name: row
        for row in discover_sessions(roots=[tmp_path], as_of=NOW)["sessions"]
    }
    assert entries["summary.jsonl"]["reason"] == "metadata-only export; no user requests"
    assert entries["unknown.jsonl"]["status"] == "error"


def test_corrupt_immutable_snapshot_is_integrity_error_not_a_source_gap(tmp_path):
    path = session(tmp_path / "logs", "selected.jsonl", "2026-11-25T10:00:00Z")
    inventory = discover_sessions(
        roots=[path.parent], as_of=NOW, snapshot_dir=tmp_path / "snapshots"
    )
    Path(inventory["sessions"][0]["snapshot_path"]).write_text("tampered")
    with pytest.raises(ValueError, match="No tasks"):
        build_history(inventory, artifact_dir=tmp_path / "artifacts")
    assert inventory["sessions"][0]["error_stage"] == "integrity"
    assert "digest mismatch" in inventory["sessions"][0]["error"]


def test_interrupted_preparation_cancels_workers_and_resumes_completed_cache(tmp_path, monkeypatch):
    import time
    from concurrent.futures import as_completed
    from threading import Event

    from personalbench.runners import check_cancellation

    logs = tmp_path / "logs"
    session(logs, "quick.jsonl", "2026-11-25T10:00:00Z", "Explain stack")
    session(logs, "slow.jsonl", "2026-11-25T10:00:00Z", "Explain queue")
    inventory = discover_sessions(roots=[logs], as_of=NOW, snapshot_dir=tmp_path / "snapshots")
    entered, stopped = Event(), Event()

    def cooperative(prompt):
        if "Explain queue" in prompt:
            entered.set()
            try:
                while True:
                    check_cancellation()
                    time.sleep(0.01)
            finally:
                stopped.set()
        assert entered.wait(timeout=1)
        return compiler(prompt)

    def interrupt_after_first(futures):
        yield next(as_completed(futures))
        raise KeyboardInterrupt("Synthetic coordinator interruption")

    kwargs = {
        "compiler_identity": "synthetic",
        "artifact_dir": tmp_path / "artifacts",
        "workers": 2,
    }
    started = time.monotonic()
    with monkeypatch.context() as patcher:
        patcher.setattr("personalbench.history.as_completed", interrupt_after_first)
        with pytest.raises(KeyboardInterrupt):
            build_history(inventory, compiler=cooperative, **kwargs)
    assert time.monotonic() - started < 2
    assert stopped.is_set()
    assert {entry["status"] for entry in inventory["sessions"]} == {"prepared", "selected"}
    calls = []

    def resumed(prompt):
        calls.append(prompt)
        return compiler(prompt)

    result = build_history(inventory, compiler=resumed, resume=True, **kwargs)
    assert len(calls) == 1  # the completed assessment came from its immutable cache
    assert result["tasks"]


def test_recovery_only_changes_revalidate_legacy_cache_without_model_calls(tmp_path, monkeypatch):
    import copy

    from personalbench.schema import canonical_hash

    path = session(tmp_path / "logs", "selected.jsonl", "2026-11-25T10:00:00Z")
    inventory = discover_sessions(
        roots=[path.parent], as_of=NOW, snapshot_dir=tmp_path / "snapshots"
    )
    artifacts = tmp_path / "artifacts"
    old_identity = {
        "compiler": {"model": "synthetic"},
        "implementation": {
            "imports.py": "fixed-prompt",
            "history.py": "old",
            "recovery.py": "old",
            "session_io.py": "old",
        },
    }

    def prepare(value, _):
        value["evidence"] = {"records": []}
        return value

    def permissive(task, _, __):
        task = copy.deepcopy(task)
        task["inputs"] = {"stale-derived.txt": "must never become a starting input"}
        task["provenance"]["source_evidence_ids"] = []
        task["provenance"]["recovery"] = {
            "required_kinds": ["file"],
            "declared_missing_inputs": ["missing.py"],
        }
        task["readiness"] = "ready"
        return task

    monkeypatch.setattr("personalbench.recovery.recover_task_inputs", permissive)
    build_history(
        inventory,
        compiler=compiler,
        compiler_identity=old_identity,
        artifact_dir=artifacts,
        prepare_session=prepare,
    )
    # Simulate a cache generated before identity normalization and raw response storage.
    cache_path = next((artifacts / "cache").glob("*.json"))
    cached = json.loads(cache_path.read_text())
    cached["identity"]["compiler"] = old_identity
    cached.pop("compiler_response")
    legacy_path = cache_path.parent / (canonical_hash(cached["identity"]) + ".json")
    legacy_path.write_text(json.dumps(cached))
    cache_path.unlink()
    checked = []

    def conservative(task, _, __):
        assert task["inputs"] == {}
        assert task["source_evidence_ids"] == []
        assert task["required_evidence_kinds"] == ["file"]
        assert task["missing_inputs"] == ["missing.py"]
        checked.append(task["brief"])
        task = copy.deepcopy(task)
        task["provenance"]["source_evidence_ids"] = []
        task["readiness"] = "incomplete"
        return task

    def unpaid(_):
        raise AssertionError("Recovery-only change must not resubmit the compiler")

    monkeypatch.setattr("personalbench.recovery.recover_task_inputs", conservative)
    new_identity = copy.deepcopy(old_identity)
    new_identity["implementation"].update(
        {"history.py": "new", "recovery.py": "new", "session_io.py": "new"}
    )
    result = build_history(
        inventory,
        compiler=unpaid,
        compiler_identity=new_identity,
        artifact_dir=artifacts,
        prepare_session=prepare,
        resume=True,
    )
    assert checked == ["Explain a stack"]
    assert result["tasks"][0]["readiness"] == "incomplete"
    assert "stale-derived.txt" not in result["tasks"][0]["inputs"]
    assert len(list((artifacts / "compiled").glob("*.json"))) == 2


def test_changed_compiler_semantics_are_not_reused_from_recovery_cache(tmp_path):
    path = session(tmp_path / "logs", "selected.jsonl", "2026-11-25T10:00:00Z")
    inventory = discover_sessions(
        roots=[path.parent], as_of=NOW, snapshot_dir=tmp_path / "snapshots"
    )
    artifacts = tmp_path / "artifacts"
    first_identity = {"compiler": "synthetic", "implementation": {"imports.py": "old"}}
    build_history(
        inventory, compiler=compiler, compiler_identity=first_identity, artifact_dir=artifacts
    )
    calls = []

    def changed(prompt):
        calls.append(prompt)
        return compiler(prompt)

    build_history(
        inventory,
        compiler=changed,
        compiler_identity={"compiler": "synthetic", "implementation": {"imports.py": "new"}},
        artifact_dir=artifacts,
        resume=True,
    )
    assert len(calls) == 1


def test_cached_benchmark_hash_is_checked_before_recovery_or_provider_calls(tmp_path):
    path = session(tmp_path / "logs", "selected.jsonl", "2026-11-25T10:00:00Z")
    inventory = discover_sessions(
        roots=[path.parent], as_of=NOW, snapshot_dir=tmp_path / "snapshots"
    )
    artifacts = tmp_path / "artifacts"
    kwargs = {"artifact_dir": artifacts, "compiler_identity": "synthetic"}
    build_history(inventory, compiler=compiler, **kwargs)
    cache = json.loads(next((artifacts / "cache").glob("*.json")).read_text())
    compiled_path = Path(cache["benchmark_path"])
    corrupted = json.loads(compiled_path.read_text())
    corrupted["tasks"][0]["brief"] = "Tampered brief"
    compiled_path.write_text(json.dumps(corrupted))

    def unpaid(_):
        raise AssertionError("Corrupt cache must not trigger paid preparation")

    with pytest.raises(ValueError, match="No tasks"):
        build_history(inventory, compiler=unpaid, resume=True, **kwargs)
    assert inventory["sessions"][0]["error_stage"] == "integrity"
    assert "benchmark digest mismatch" in inventory["sessions"][0]["error"]


def test_cached_daily_lookup_revalidates_declared_capabilities_and_optional_memory(
    tmp_path, monkeypatch
):
    from personalbench import recovery

    path = session(
        tmp_path / "logs",
        "daily.jsonl",
        "2026-11-25T10:00:00Z",
        "Give me a morning brief with today's calendar and important unread emails.",
    )
    inventory = discover_sessions(
        roots=[path.parent], as_of=NOW, snapshot_dir=tmp_path / "snapshots"
    )
    artifacts = tmp_path / "artifacts"

    def prepare(value, _):
        value["evidence"] = {
            "timezone": "UTC",
            "records": [
                {
                    "id": source_id,
                    "available_kinds": [kind],
                    "kinds": [kind],
                    "timestamp": "2026-11-25T10:00:00Z",
                    "output_timestamp": "2026-11-25T10:00:00Z",
                    "query": {},
                    "text": text,
                    "limits": [],
                    "text_hash": source_id,
                }
                for source_id, kind, text in [
                    ("calendar", "calendar", '{"events":[]}'),
                    ("email", "email", '{"emails":[{"subject":"Synthetic meeting"}]}'),
                ]
            ],
        }
        return value

    def daily(_):
        return json.dumps(
            {
                "tasks": [
                    {
                        "id": "daily",
                        "category": "productivity",
                        "brief": "Prepare a morning brief from calendar and email; disclose unavailable memory.",
                        "requires_starting_state": True,
                        "source_message_indices": [0, 1],
                        "source_evidence_ids": ["calendar", "email"],
                        "required_evidence_kinds": ["calendar", "email", "memory"],
                        "execution_requirements": ["live_accounts"],
                        "criteria": [
                            {
                                "id": "sources",
                                "description": "Use captured calendar and email; disclose unavailable memory.",
                            }
                        ],
                    }
                ],
                "exclusions": [],
            }
        )

    kwargs = {
        "artifact_dir": artifacts,
        "prepare_session": prepare,
        "compiler_identity": "synthetic",
    }
    # Emulate the earlier gate, which could not resolve read-only source replay.
    with monkeypatch.context() as patcher:
        patcher.setattr(recovery, "_pure_daily_request", lambda _: False)
        old = build_history(inventory, compiler=daily, **kwargs)
    assert old["tasks"][0]["readiness"] == "unsupported"

    def unpaid(_):
        raise AssertionError("Deterministic capability resolution must not call the model")

    for _ in range(2):
        # Both pre-fix and already-derived legacy cache variants must reconstruct declarations.
        cache_path = next((artifacts / "cache").glob("*.json"))
        cached = json.loads(cache_path.read_text())
        cached.pop("compiler_response", None)
        cache_path.write_text(json.dumps(cached))
        updated = build_history(inventory, compiler=unpaid, resume=True, **kwargs)
        task = updated["tasks"][0]
        assert task["readiness"] == "ready"
        assert task["execution_requirements"] == []
        assert task["provenance"]["recovery"]["declared_execution_requirements"] == [
            "live_accounts"
        ]
        assert task["provenance"]["recovery"]["declared_required_kinds"] == [
            "calendar",
            "email",
            "memory",
        ]
        assert task["brief"] == old["tasks"][0]["brief"]
        assert task["criteria"] == old["tasks"][0]["criteria"]


def test_compiler_timeout_increase_reuses_legacy_cache_but_model_change_does_not(tmp_path):
    import copy

    from personalbench.schema import canonical_hash

    path = session(tmp_path / "logs", "selected.jsonl", "2026-11-25T10:00:00Z")
    inventory = discover_sessions(
        roots=[path.parent], as_of=NOW, snapshot_dir=tmp_path / "snapshots"
    )
    artifacts = tmp_path / "artifacts"
    original_identity = {
        "compiler": {
            "harness": "codex",
            "model": "synthetic-model-a",
            "instructions": "Keep requirements fixed",
            "reasoning_effort": "medium",
            "timeout_seconds": 180,
        },
        "implementation": {"imports.py": "fixed-prompt"},
    }
    build_history(
        inventory, compiler=compiler, compiler_identity=original_identity, artifact_dir=artifacts
    )
    # A pre-fix checkpoint included the resource timeout in its compiler identity.
    cache_path = next((artifacts / "cache").glob("*.json"))
    cached = json.loads(cache_path.read_text())
    cached["identity"]["compiler"] = original_identity
    legacy_path = cache_path.parent / (canonical_hash(cached["identity"]) + ".json")
    legacy_path.write_text(json.dumps(cached))
    cache_path.unlink()

    def unpaid(_):
        raise AssertionError("Timeout-only change must not resubmit completed compilation")

    increased = copy.deepcopy(original_identity)
    increased["compiler"]["timeout_seconds"] = 300
    result = build_history(
        inventory, compiler=unpaid, compiler_identity=increased, artifact_dir=artifacts, resume=True
    )
    assert result["tasks"][0]["readiness"] == "ready"
    calls = []

    def new_model(prompt):
        calls.append(prompt)
        return compiler(prompt)

    changed = copy.deepcopy(increased)
    changed["compiler"]["model"] = "synthetic-model-b"
    build_history(
        inventory,
        compiler=new_model,
        compiler_identity=changed,
        artifact_dir=artifacts,
        resume=True,
    )
    assert len(calls) == 1


def test_validity_gate_blocks_material_omission_without_requiring_perfect_style():
    from personalbench.grading import grade
    from personalbench.history import _apply_validity_gate

    task = {
        "brief": "Summarize today's captured calendar, important unread emails, and priorities.",
        "inputs": {"calendar.json": '{"events":[{"title":"Team meeting","time":"10:00"}]}'},
        "criteria": [
            {
                "id": name,
                "kind": "judge",
                "description": description,
                "weight": 1 if name == "calendar" else 10,
            }
            for name, description in [
                ("calendar", "Summarize the calendar."),
                ("email", "Summarize important unread emails."),
                ("actions", "Prioritize next actions."),
                ("grounding", "Use captured evidence."),
                ("presentation", "Keep the brief readable."),
            ]
        ],
    }
    original = json.loads(json.dumps(task["criteria"]))
    _apply_validity_gate(task)
    gate = task["criteria"][-1]
    assert gate["critical"] is True
    assert task["criteria"][:-1] == original

    def judge(prompt):
        data = json.loads(prompt.split("\n", 1)[1])
        output = data["criteria"][0]["candidate"]
        omitted = output == "Only email and priorities; calendar component omitted."
        return json.dumps(
            {
                "scores": [
                    {
                        "id": criterion["id"],
                        "score": (0 if omitted else 1)
                        if criterion["id"] == gate["id"]
                        else (
                            0
                            if omitted and criterion["id"] == "calendar"
                            else 0.8
                            if not omitted and criterion["id"] == "presentation"
                            else 1
                        ),
                        "evidence": "Missing a mandatory calendar component"
                        if omitted and criterion["id"] in (gate["id"], "calendar")
                        else "Mandatory content present; style is a separate quality dimension",
                    }
                    for criterion in data["criteria"]
                ]
            }
        )

    missing_core = grade(
        task,
        {"status": "completed", "output": "Only email and priorities; calendar component omitted."},
        judge=judge,
    )
    assert (
        missing_core["quality_pct"] >= 80
    )  # Strong other dimensions cannot rescue a missing core component.
    assert missing_core["success"] is False
    assert missing_core["criteria"][-1]["score"] == 0
    minor_style = grade(
        task,
        {
            "status": "completed",
            "output": "Calendar: Team meeting at10:00. Important email and priorities supplied, with rough formatting.",
        },
        judge=judge,
    )
    assert minor_style["success"] is True
    assert minor_style["quality_pct"] < 100
    _apply_validity_gate(task)
    assert len(task["criteria"]) == len(original) + 1


def test_history_success_policy_is_post_cache_and_requires_no_recompilation(tmp_path):
    from personalbench.history import _VALIDITY_GATE

    path = session(tmp_path / "logs", "selected.jsonl", "2026-11-25T10:00:00Z")
    inventory = discover_sessions(
        roots=[path.parent], as_of=NOW, snapshot_dir=tmp_path / "snapshots"
    )
    artifacts = tmp_path / "artifacts"
    kwargs = {"artifact_dir": artifacts, "compiler_identity": "synthetic"}
    first = build_history(inventory, compiler=compiler, **kwargs)
    first_task = first["tasks"][0]
    assert first["scoring"]["success_contract"] == "mandatory-outcome-v1"
    assert first_task["criteria"][-1] == _VALIDITY_GATE
    stored = json.loads(Path(inventory["sessions"][0]["compiled_benchmark"]).read_text())
    assert all(c["id"] != _VALIDITY_GATE["id"] for c in stored["tasks"][0]["criteria"])

    def no_paid_call(_):
        raise AssertionError("Changing deterministic success policy must not recompile model goals")

    second = build_history(inventory, compiler=no_paid_call, resume=True, **kwargs)
    assert second["tasks"][0]["criteria"] == first_task["criteria"]
    assert len([c for c in second["tasks"][0]["criteria"] if c.get("critical")]) == 1


def fork_goal_fixture(
    tmp_path,
    *,
    related=True,
    source_change=None,
    different_files=False,
    different_requirements=False,
    different_context=False,
    later_snapshots=False,
):
    from personalbench.session_io import read_session_bytes

    logs = tmp_path / "logs"
    request = "Initialize a private repository and commit and push updates automatically."
    for name, stamp, identifier in [
        ("parent.jsonl", "2026-11-25T10:00:00Z", "parent"),
        ("child.jsonl", "2026-11-25T11:00:00Z", "child"),
    ]:
        child = identifier == "child"
        path = session(
            logs,
            name,
            stamp,
            request + (" Use a different root folder." if child and different_requirements else ""),
            "A different accepted implementation plan."
            if child and different_context
            else "Initialized and synchronized.",
        )
        metadata = {
            "payload": {
                "id": identifier,
                "source": "vscode",
                "forked_from_id": "parent" if identifier == "child" and related else None,
            }
        }
        metadata["type"] = "session_meta"
        path.write_text(json.dumps(metadata) + "\n" + path.read_text())

    def prepare(value, snapshot):
        metadata = json.loads(read_session_bytes(snapshot).decode().split("\n")[0])["payload"]
        child = metadata["id"] == "child"
        date = value["messages"][0]["timestamp"]
        value["evidence"] = {
            "timezone": "UTC",
            "records": [
                {
                    "id": f"source-{number}",
                    "call_id": f"copied-call-{number}"
                    + ("-new" if child and source_change == "call" else ""),
                    "originating_call_id": f"copied-call-{number}"
                    + ("-new" if child and source_change == "call" else ""),
                    "request_hash": f"identical-request-{number}",
                    "kinds": ["file"],
                    "available_kinds": ["file"],
                    "timestamp": date,
                    "output_timestamp": date,
                    "query": {},
                    "limits": [],
                    "text": f"Recorded directory listing {number}"
                    + (" changed" if child and source_change == "text" else ""),
                    "text_hash": f"synthetic-hash-{number}",
                }
                for number in (1, 2)
            ],
        }
        if later_snapshots:
            for record in value["evidence"]["records"]:
                record["output_line"] = 2
            value["evidence"]["records"].append(
                {
                    "id": "later-source",
                    "call_id": "later-" + metadata["id"],
                    "request_hash": "later-request",
                    "available_kinds": ["file"],
                    "kinds": ["file"],
                    "output_line": 4,
                    "output_timestamp": date,
                    "timestamp": date,
                    "text": "Different later state in " + metadata["id"],
                    "query": {},
                    "limits": [],
                }
            )
        if different_files:
            value["inputs"] = {"starting.txt": "child state" if child else "parent state"}
        return value

    def compile_fork(prompt):
        child = "2026-11-25T11:00:00Z" in prompt
        return json.dumps(
            {
                "tasks": [
                    {
                        "id": "initialize",
                        "category": "coding",
                        "requires_starting_state": True,
                        "brief": "Set up the private repository."
                        if child
                        else "Initialize the private repository.",
                        "criteria": [
                            {
                                "id": "created",
                                "description": "Private remote exists and syncs."
                                if child
                                else "Repository is private and synchronized.",
                            }
                        ],
                        "source_message_indices": [0, 1],
                        "source_evidence_ids": ["source-1", "source-2"] if child else ["source-1"],
                        "required_evidence_kinds": ["file"],
                        "execution_requirements": ["workspace", "publication"],
                    }
                ],
                "exclusions": [],
            }
        )

    inventory = discover_sessions(roots=[logs], as_of=NOW, snapshot_dir=tmp_path / "snapshots")
    result = build_history(
        inventory,
        compiler=compile_fork,
        compiler_identity="synthetic",
        artifact_dir=tmp_path / "artifacts",
        prepare_session=prepare,
    )
    return result, inventory


def test_exact_inherited_fork_goal_consolidates_compiler_selection_variation(tmp_path):
    result, inventory = fork_goal_fixture(tmp_path)
    assert len(result["tasks"]) == 1
    task = result["tasks"][0]
    assert task["brief"] == "Initialize the private repository."
    assert task["provenance"]["source_evidence_ids"] == ["source-1"]
    child = next(row for row in inventory["sessions"] if row["session_id"] == "child")
    duplicate = child["duplicate_tasks"][0]
    assert duplicate["method"] == "explicit-fork-exact-goal-copy"
    assert duplicate["duplicate_of"] == task["id"]
    alternate = json.loads(Path(duplicate["alternate_definition"]).read_text())
    assert alternate["alternate_compiled_definition"]["brief"] == "Set up the private repository."
    assert alternate["alternate_compiled_definition"]["provenance"]["source_evidence_ids"] == [
        "source-1",
        "source-2",
    ]
    assert alternate["proof"]["source_observation_count"] == 2
    assert {row["status"] for row in inventory["sessions"]} == {"compiled"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"related": False},
        {"source_change": "text"},
        {"source_change": "call"},
        {"different_files": True},
        {"different_requirements": True},
        {"different_context": True},
    ],
)
def test_inherited_goal_dedup_requires_lineage_identical_observations_and_files(tmp_path, kwargs):
    result, _ = fork_goal_fixture(tmp_path, **kwargs)
    assert len(result["tasks"]) == 2


def test_fork_goal_source_scope_uses_record_order_when_timestamps_are_tied(tmp_path):
    result, inventory = fork_goal_fixture(tmp_path, later_snapshots=True)
    assert len(result["tasks"]) == 1
    child = next(row for row in inventory["sessions"] if row["session_id"] == "child")
    assert child["duplicate_tasks"][0]["proof"]["source_observation_count"] == 2
