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
    assert entries[str(empty)]["status"] == "error"
    assert entries[str(malformed)]["status"] == "error"
    assert set(checkpoint) == {"compiled", "duplicate", "error"}
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
    assert inventory["sessions"][0]["status"] == "error"
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
