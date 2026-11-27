"""Newline JSON-RPC MCP stdio service for one protected calendar/mail runtime."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .environments import EnvironmentError, _bounded_json, execute_tool


def _schema(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_STRING = {"type": "string"}
_CALENDAR = {"type": "string", "default": "primary"}
_PAGING = {
    "max_results": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
    "page_token": _STRING,
}
_EVENT = {
    "type": "object",
    "description": "Google-style event fields: summary, description, location, start/end ({dateTime: RFC3339, timeZone?: IANA} or {date: YYYY-MM-DD}), attendees. End must follow start. Recurrence is unsupported.",
}
TOOLS = [
    {
        "name": "environment_info",
        "description": "Read the environment's fixed current time and IANA timezone.",
        "inputSchema": _schema({}),
    },
    {
        "name": "calendar_list_events",
        "description": "Read events overlapping the optional exclusive time range. RFC3339 bounds require timezone offsets. Results sort by start then id; use next_page_token for remaining results.",
        "inputSchema": _schema(
            {"calendar_id": _CALENDAR, "time_min": _STRING, "time_max": _STRING, **_PAGING}
        ),
    },
    {
        "name": "calendar_create_event",
        "description": "Create an event in the selected calendar. Returns the event with a deterministic id if none was supplied.",
        "inputSchema": _schema({"calendar_id": _CALENDAR, "event": _EVENT}, ["event"]),
    },
    {
        "name": "calendar_update_event",
        "description": "Merge event fields into an existing event, preserving its id. Supply both start and end when moving a meeting.",
        "inputSchema": _schema(
            {"calendar_id": _CALENDAR, "event_id": _STRING, "event": _EVENT}, ["event_id", "event"]
        ),
    },
    {
        "name": "calendar_delete_event",
        "description": "Delete one existing event from the selected calendar.",
        "inputSchema": _schema({"calendar_id": _CALENDAR, "event_id": _STRING}, ["event_id"]),
    },
    {
        "name": "mail_search",
        "description": "Search mail using implicit AND, quoted phrases, from:, to:, subject:, after:/before:YYYY-MM-DD (environment timezone), newer_than:/older_than:Nd, is:unread/read/starred/important, in:inbox, label:, has:attachment. No OR/grouping. Dates sort newest first; use next_page_token. Mail flags use labels UNREAD/STARRED/IMPORTANT or explicit booleans.",
        "inputSchema": _schema({"query": _STRING, **_PAGING}, ["query"]),
    },
    {
        "name": "mail_get_message",
        "description": "Read a complete mail message by id, including body, date, recipients and labels.",
        "inputSchema": _schema({"message_id": _STRING}, ["message_id"]),
    },
]
MAX_REQUEST_BYTES = 128_000
for _tool in TOOLS:
    _read = _tool["name"] in {
        "environment_info",
        "calendar_list_events",
        "mail_search",
        "mail_get_message",
    }
    _tool["annotations"] = {
        "readOnlyHint": _read,
        "destructiveHint": not _read,
        "openWorldHint": False,
    }


def _request(request, runtime_dir):
    if (
        not isinstance(request, dict)
        or request.get("jsonrpc") != "2.0"
        or not isinstance(request.get("method"), str)
    ):
        return {
            "jsonrpc": "2.0",
            "id": request.get("id") if isinstance(request, dict) else None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }
    if "id" not in request:
        return None
    identifier, method, params = request["id"], request["method"], request.get("params", {})
    if identifier is not None and (
        isinstance(identifier, bool) or not isinstance(identifier, (str, int, float))
    ):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request id"},
        }
    response = {"jsonrpc": "2.0", "id": identifier}
    if not isinstance(params, dict):
        return response | {"error": {"code": -32602, "message": "Invalid params"}}
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in ("2025-11-25", "2025-06-18") else "2025-11-25"
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "personalbench-calendar-mail", "version": "1.0.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            data = execute_tool(runtime_dir, params.get("name"), params.get("arguments", {}))
            result = {
                "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                "structuredContent": data,
                "isError": False,
            }
        except EnvironmentError as exc:
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"error": {"code": exc.code, "message": exc.message}}),
                    }
                ],
                "isError": True,
            }
        except (OSError, ValueError, TypeError):
            result = {
                "content": [{"type": "text", "text": "Environment operation unavailable"}],
                "isError": True,
            }
    else:
        return response | {"error": {"code": -32601, "message": "Method not found"}}
    return response | {"result": result}


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("Usage: python -m personalbench.mock_server RUNTIME_DIR", file=sys.stderr)
        return 2
    while True:
        line = sys.stdin.readline(MAX_REQUEST_BYTES + 1)
        if not line:
            break
        oversized = len(line.encode()) > MAX_REQUEST_BYTES
        if oversized:
            while not line.endswith("\n"):
                line = sys.stdin.readline(MAX_REQUEST_BYTES + 1)
                if not line:
                    break
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": "Request byte limit exceeded"},
                    }
                ),
                flush=True,
            )
            continue
        try:
            request = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            _bounded_json(request, MAX_REQUEST_BYTES)
        except (ValueError, TypeError, RecursionError):
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
        else:
            response = _request(request, Path(arguments[0]))
        if response is not None:
            print(json.dumps(response, ensure_ascii=False, allow_nan=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
