"""Real state transitions and stdio wire checks using synthetic fixtures only."""

import copy
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from personalbench.environments import (
    EnvironmentError,
    execute_tool,
    initialize_environment,
    snapshot_environment,
    validate_environment,
)


def event(
    identifier="existing", start="2026-08-19T20:00:00-07:00", end="2026-08-19T21:00:00-07:00"
):
    return {
        "id": identifier,
        "summary": "Existing appointment",
        "start": {"dateTime": start, "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": end, "timeZone": "America/Los_Angeles"},
    }


def fixture():
    return {
        "kind": "calendar-mail-v1",
        "fixture_origin": "synthetic",
        "now": "2026-08-06T02:25:40Z",
        "timezone": "America/Los_Angeles",
        "calendars": [{"id": "primary", "events": [event()]}],
        "messages": [
            {
                "id": "message-1",
                "threadId": "thread-1",
                "from": "Coordinator <sender@example.test>",
                "to": ["recipient@example.test"],
                "subject": "Dinner invitation",
                "date": "2026-08-05T20:00:00Z",
                "body": "Please join dinner at 17:30. Attachment has details.",
                "labels": ["INBOX", "UNREAD", "IMPORTANT"],
                "attachments": [{"filename": "invite.ics"}],
            },
            {
                "id": "message-2",
                "threadId": "thread-2",
                "from": "other@example.test",
                "to": "recipient@example.test",
                "subject": "Older note",
                "date": "2026-08-01T15:00:00Z",
                "body": "Earlier conversation",
                "unread": False,
            },
        ],
    }


def setup(tmp_path):
    path = tmp_path / "protected-runtime"
    initialize_environment(fixture(), path)
    return path


def test_snapshot_copy_isolation_atomic_single_file_and_reset(tmp_path):
    spec = fixture()
    paths = initialize_environment(spec, tmp_path / "runtime")
    root = Path(paths["runtime_dir"])
    assert {p.name for p in root.iterdir()} == {"environment.json"}
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(Path(paths["state_path"]).stat().st_mode) == 0o600
    spec["calendars"][0]["events"][0]["summary"] = "Mutated caller input"
    snapshot = snapshot_environment(root)
    snapshot["final_state"]["calendars"].clear()
    assert snapshot_environment(root)["initial_state"] == fixture()
    assert snapshot_environment(root)["final_state"] == fixture()
    execute_tool(root, "calendar_delete_event", {"event_id": "existing"})
    initialize_environment(fixture(), root)
    assert snapshot_environment(root)["operations"] == []
    assert snapshot_environment(root)["final_state"] == fixture()


def test_calendar_create_update_delete_preserve_identity_and_audit(tmp_path):
    root = setup(tmp_path)
    new = event(start="2026-08-19T17:30:00-07:00", end="2026-08-19T20:30:00-07:00")
    del new["id"]
    created = execute_tool(root, "calendar_create_event", {"event": new})
    assert created["id"] == "event-000001"
    created["summary"] = "Mutated output"
    updated = execute_tool(
        root,
        "calendar_update_event",
        {"event_id": "event-000001", "event": {"summary": "Updated title"}},
    )
    assert updated["id"] == "event-000001"
    assert updated["start"] == new["start"]
    assert updated["summary"] == "Updated title"
    execute_tool(root, "calendar_delete_event", {"event_id": "event-000001"})
    second = execute_tool(root, "calendar_create_event", {"event": new})
    assert second["id"] == "event-000002"
    snapshot = snapshot_environment(root)
    assert snapshot["initial_state"] == fixture()
    assert snapshot["final_state"]["calendars"][0]["events"][0] == event()
    assert [entry["sequence"] for entry in snapshot["operations"]] == [1, 2, 3, 4]
    assert all(entry["status"] == "ok" for entry in snapshot["operations"])


@pytest.mark.parametrize(
    "name,arguments,code",
    [
        ("calendar_create_event", {"calendar_id": "missing", "event": event()}, "not_found"),
        (
            "calendar_update_event",
            {"event_id": "existing", "event": {"id": "replacement"}},
            "invalid_arguments",
        ),
        (
            "calendar_update_event",
            {"event_id": "existing", "event": {"end": {"dateTime": "2026-08-19T19:00:00-07:00"}}},
            "invalid_arguments",
        ),
        ("calendar_delete_event", {"event_id": "missing"}, "not_found"),
        ("mail_get_message", {"message_id": "missing"}, "not_found"),
        ("calendar_list_events", {"time_min": "2026-08-19T20:00:00"}, "invalid_arguments"),
        ("calendar_list_events", {"max_results": True}, "invalid_arguments"),
        ("calendar_delete_event", {"event_id": "existing", "event": {}}, "invalid_arguments"),
        ("filesystem_read", {"path": "/private/secret-canary"}, "unknown_tool"),
    ],
)
def test_rejected_calls_leave_final_state_unchanged_and_audit_error(
    tmp_path, name, arguments, code
):
    root = setup(tmp_path)
    before = snapshot_environment(root)
    with pytest.raises(EnvironmentError) as error:
        execute_tool(root, name, arguments)
    assert error.value.code == code
    assert "/private/secret-canary" not in error.value.message
    after = snapshot_environment(root)
    assert after["final_state"] == before["final_state"]
    assert after["initial_state"] == before["initial_state"]
    assert after["operations"][-1]["status"] == "error"


def test_calendar_overlap_paging_stale_tokens_and_all_day_timezone(tmp_path):
    spec = fixture()
    spec["calendars"][0]["events"] += [
        event("early", "2026-08-19T17:30:00-07:00", "2026-08-19T20:30:00-07:00"),
        {"id": "all-day", "start": {"date": "2026-08-19"}, "end": {"date": "2026-08-20"}},
    ]
    root = tmp_path / "runtime"
    initialize_environment(spec, root)
    args = {
        "time_min": "2026-08-19T20:00:00-07:00",
        "time_max": "2026-08-19T21:00:00-07:00",
        "max_results": 2,
    }
    first = execute_tool(root, "calendar_list_events", args)
    assert [item["id"] for item in first["events"]] == ["all-day", "early"]
    second = execute_tool(
        root, "calendar_list_events", args | {"page_token": first["next_page_token"]}
    )
    assert [item["id"] for item in second["events"]] == ["existing"]
    assert second["next_page_token"] is None
    execute_tool(
        root, "calendar_update_event", {"event_id": "early", "event": {"summary": "Changed"}}
    )
    with pytest.raises(EnvironmentError, match="stale"):
        execute_tool(root, "calendar_list_events", args | {"page_token": first["next_page_token"]})


@pytest.mark.parametrize(
    "query,expected",
    [
        ("", ["message-1", "message-2"]),
        (
            'from:sender@example.test subject:"Dinner invitation" is:unread in:inbox has:attachment',
            ["message-1"],
        ),
        ("to:recipient@example.test newer_than:2d is:important", ["message-1"]),
        ("older_than:2d is:read", ["message-2"]),
        ("after:2026-08-05 before:2026-08-06", ["message-1"]),
        ('"Earlier conversation"', ["message-2"]),
    ],
)
def test_mail_search_documented_subset(tmp_path, query, expected):
    result = execute_tool(setup(tmp_path), "mail_search", {"query": query})
    assert [item["id"] for item in result["messages"]] == expected


def test_mail_paging_and_read_full_body(tmp_path):
    root = setup(tmp_path)
    first = execute_tool(root, "mail_search", {"query": "", "max_results": 1})
    second = execute_tool(
        root, "mail_search", {"query": "", "max_results": 1, "page_token": first["next_page_token"]}
    )
    assert second["messages"][0]["id"] == "message-2"
    assert (
        execute_tool(root, "mail_get_message", {"message_id": "message-1"})["body"]
        == fixture()["messages"][0]["body"]
    )
    with pytest.raises(EnvironmentError):
        execute_tool(
            root, "mail_search", {"query": "subject:Older", "page_token": first["next_page_token"]}
        )


@pytest.mark.parametrize(
    "query",
    [
        "unknown:value",
        "missingterm unknown:value",
        "OR",
        "{from:a from:b}",
        "newer_than:999999999999999d",
        'subject:"unfinished',
    ],
)
def test_invalid_queries_fail_even_for_empty_mailboxes(tmp_path, query):
    spec = fixture()
    spec["messages"] = []
    root = tmp_path / "runtime"
    initialize_environment(spec, root)
    with pytest.raises(EnvironmentError):
        execute_tool(root, "mail_search", {"query": query})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda spec: spec.update(now="2026-08-01T10:00:00"),
        lambda spec: spec.update(timezone="Invalid/Zone"),
        lambda spec: spec.update(expected_answer="private-grader-canary"),
        lambda spec: spec["messages"].append(copy.deepcopy(spec["messages"][0])),
        lambda spec: spec["calendars"][0]["events"][0]["end"].update(
            dateTime="2026-08-19T19:00:00-07:00"
        ),
    ],
)
def test_validation_rejects_bad_fixture_without_creating_runtime(tmp_path, mutate):
    spec = fixture()
    mutate(spec)
    with pytest.raises(EnvironmentError):
        initialize_environment(spec, tmp_path / "runtime")
    assert not (tmp_path / "runtime").exists()


def test_input_size_depth_and_nonfinite_bounds_do_not_change_state(tmp_path):
    root = setup(tmp_path)
    before = snapshot_environment(root)
    value = {}
    for _ in range(40):
        value = {"nested": value}
    for arguments in (value, {"query": "a" * 65_000}, {"query": float("nan")}):
        with pytest.raises(EnvironmentError):
            execute_tool(root, "mail_search", arguments)
    assert snapshot_environment(root) == before
    validated = validate_environment(fixture())
    validated["messages"].clear()
    assert fixture()["messages"]


def test_actual_stdio_initialize_list_calls_errors_and_persistence(tmp_path):
    root = setup(tmp_path)
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "calendar_create_event", "arguments": {"event": event("created")}},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "mail_search", "arguments": {"query": "is:unread"}},
        },
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "mail_get_message", "arguments": {"message_id": "missing"}},
        },
        {"jsonrpc": "2.0", "id": 6, "method": "ping"},
    ]
    completed = subprocess.run(
        [sys.executable, "-m", "personalbench.mock_server", str(root)],
        input="\n".join(json.dumps(item) for item in requests) + "\n",
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert len(responses) == 6
    assert responses[0]["result"]["protocolVersion"] == "2025-06-18"
    tools = responses[1]["result"]["tools"]
    assert {tool["name"] for tool in tools} == {
        "environment_info",
        "calendar_list_events",
        "calendar_create_event",
        "calendar_update_event",
        "calendar_delete_event",
        "mail_search",
        "mail_get_message",
    }
    assert "private-grader-canary" not in json.dumps(tools)
    assert str(root) not in completed.stdout
    assert responses[2]["result"]["structuredContent"]["id"] == "created"
    assert responses[3]["result"]["structuredContent"]["messages"][0]["id"] == "message-1"
    assert responses[4]["result"]["isError"] is True
    assert responses[5]["result"] == {}
    snapshot = snapshot_environment(root)
    assert len(snapshot["operations"]) == 3
    assert any(
        item["id"] == "created" for item in snapshot["final_state"]["calendars"][0]["events"]
    )
    assert completed.stderr == ""


def test_stdio_recovers_after_bad_json_and_oversized_request(tmp_path):
    root = setup(tmp_path)
    completed = subprocess.run(
        [sys.executable, "-m", "personalbench.mock_server", str(root)],
        input="{broken\n"
        + "x" * 129_000
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"})
        + "\n",
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert responses[0]["error"]["code"] == -32700
    assert responses[1]["error"]["code"] == -32600
    assert responses[2]["result"] == {}
    assert snapshot_environment(root)["operations"] == []


def test_recurring_instance_metadata_is_preserved_but_cannot_be_forged(tmp_path):
    spec = fixture()
    existing = spec["calendars"][0]["events"][0]
    existing.update(
        recurringEventId="recurrence-parent",
        originalStartTime=copy.deepcopy(existing["start"]),
        location=None,
    )
    root = tmp_path / "runtime"
    initialize_environment(spec, root)
    updated = execute_tool(
        root, "calendar_update_event", {"event_id": "existing", "event": {"summary": "New title"}}
    )
    assert updated["recurringEventId"] == "recurrence-parent"
    assert updated["originalStartTime"] == existing["originalStartTime"]
    assert updated["location"] is None
    before = snapshot_environment(root)["final_state"]
    with pytest.raises(EnvironmentError, match="Read-only"):
        execute_tool(
            root,
            "calendar_update_event",
            {"event_id": "existing", "event": {"recurringEventId": "forged"}},
        )
    with pytest.raises(EnvironmentError, match="Read-only"):
        execute_tool(root, "calendar_create_event", {"event": existing})
    assert snapshot_environment(root)["final_state"] == before


def test_event_ids_are_deterministic_across_fresh_trials(tmp_path):
    roots = [tmp_path / "first", tmp_path / "second"]
    ids = []
    for root in roots:
        initialize_environment(fixture(), root)
        value = event()
        del value["id"]
        ids.append(execute_tool(root, "calendar_create_event", {"event": value})["id"])
    assert ids == ["event-000001", "event-000001"]
