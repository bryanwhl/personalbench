"""Synthetic native process verifies the trusted MCP bridge and write authorization."""

import json
import os
import sys
from unittest.mock import patch

import pytest

from personalbench.runners import RunnerError, run_candidate
from personalbench.schema import validate_benchmark


def environment():
    return {
        "kind": "calendar-mail-v1",
        "fixture_origin": "synthetic",
        "now": "2026-08-01T00:00:00Z",
        "timezone": "UTC",
        "calendars": [{"id": "primary", "events": []}],
        "messages": [],
    }


def test_native_mock_bridge_writes_protected_state_and_captures_it(tmp_path):
    root = tmp_path.resolve()
    fake = root / "codex"
    fake.write_text(
        f"#!{sys.executable}\n"
        + """import json,subprocess,sys
args=sys.argv[1:]
assert 'mcp_servers.personalbench.default_tools_approval_mode="approve"' in args
assert 'features.shell_tool=false' in args
assert 'permissions.personalbench.network.enabled=false' in args
prompt=sys.stdin.read()
assert 'expected-secret-grader-value' not in prompt
launch=json.loads(next(a.split('=',1)[1] for a in args if a.startswith('mcp_servers.personalbench.args=')))
assert launch[:3]==['-I','-m','personalbench.mock_server']
server=subprocess.Popen([sys.executable,*launch],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
def rpc(identifier,method,params):
 server.stdin.write(json.dumps({'jsonrpc':'2.0','id':identifier,'method':method,'params':params})+'\\n');server.stdin.flush()
 return json.loads(server.stdout.readline())
assert 'result' in rpc(1,'initialize',{'protocolVersion':'2025-11-25'})
result=rpc(2,'tools/call',{'name':'calendar_create_event','arguments':{'event':{'summary':'Synthetic meeting','start':{'dateTime':'2026-08-01T10:00:00Z'},'end':{'dateTime':'2026-08-01T11:00:00Z'}}}})
assert not result.get('result',{}).get('isError'),result
server.stdin.close();server.wait(timeout=5)
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Created a simulated event'}}))
print(json.dumps({'type':'turn.completed','usage':{}}))
"""
    )
    fake.chmod(0o700)
    workspace = root / "workspace"
    workspace.mkdir()
    # An editable candidate file cannot substitute for state owned by the MCP server.
    (workspace / "environment.json").write_text('{"forged":true}')
    task = {
        "id": "mock",
        "brief": "Create an event in the simulator",
        "environment": environment(),
        "criteria": [{"id": "hidden", "kind": "contains", "value": "expected-secret-grader-value"}],
    }
    with patch.dict(
        os.environ,
        {"PATH": str(root) + os.pathsep + os.environ["PATH"], "CODEX_HOME": str(root / "no-auth")},
    ):
        result = run_candidate(
            task,
            {
                "name": "Synthetic Codex process",
                "harness": "codex",
                "model": "synthetic",
                "tools": "environment",
            },
            workspace,
            allow_unsafe_local=True,
        )
    assert result["status"] == "completed", result["error"]
    captured = result["environment"]
    assert captured["initial_state"]["calendars"][0]["events"] == []
    assert len(captured["final_state"]["calendars"][0]["events"]) == 1
    assert captured["operations"][0]["tool"] == "calendar_create_event"
    assert json.loads((workspace / "environment.json").read_text()) == {"forged": True}


def test_environment_task_rejects_harness_without_tool_bridge(tmp_path):
    with pytest.raises(RunnerError, match="Simulated tasks require"):
        run_candidate(
            {"id": "mock", "brief": "Create event", "environment": environment()},
            {"name": "No tools", "harness": "codex", "model": "synthetic", "tools": "none"},
            tmp_path,
        )


def test_invalid_environment_grader_spec_is_a_validation_error():
    from personalbench.schema import ValidationError

    with pytest.raises(ValidationError):
        validate_benchmark(
            {
                "schema_version": 1,
                "name": "Synthetic invalid criterion",
                "tasks": [
                    {
                        "id": "bad",
                        "category": "productivity",
                        "brief": "Create event",
                        "environment": environment(),
                        "criteria": [
                            {
                                "id": "state",
                                "kind": "environment_state",
                                "value": {"calendar_id": 42, "events": [{}]},
                            }
                        ],
                    }
                ],
            }
        )


def test_state_assertions_require_the_calendar_in_the_fixture():
    from personalbench.schema import ValidationError

    missing = environment()
    missing["calendars"] = []
    with pytest.raises(ValidationError, match="calendar absent"):
        validate_benchmark(
            {
                "schema_version": 1,
                "name": "Synthetic missing calendar",
                "tasks": [
                    {
                        "id": "bad",
                        "category": "productivity",
                        "brief": "Create event",
                        "environment": missing,
                        "criteria": [
                            {
                                "id": "state",
                                "kind": "environment_state",
                                "description": "Calendar must contain the requested meeting",
                                "value": {"events": [{"summary": "Meeting"}]},
                            }
                        ],
                    }
                ],
            }
        )
