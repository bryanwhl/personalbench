"""Private, deterministic calendar/mail state for account-free tool evaluations.

Mail search supports implicit AND, quoted phrases, from:, to:, subject:, after:,
before:, newer_than:Nd, older_than:Nd, is:unread/read/starred/important,
in:inbox, label:, and has:attachment. OR/grouping and other operators fail explicitly.
Each runtime directory belongs to one sequential tool server. environment.json is
the atomic authoritative transaction containing state and its operation log.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import shlex
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .privacy import private_directory, read_json, write_json

KIND = "calendar-mail-v1"
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z")
_EVENT_FIELDS = {
    "id",
    "summary",
    "description",
    "location",
    "start",
    "end",
    "attendees",
    "status",
    "transparency",
    "visibility",
    "htmlLink",
    "organizer",
    "creator",
    "created",
    "updated",
    "etag",
    "sequence",
    "eventType",
    "reminders",
    "attachments",
    "hangoutLink",
    "conferenceData",
    "recurringEventId",
    "originalStartTime",
}
_MESSAGE_FIELDS = {
    "id",
    "threadId",
    "from",
    "to",
    "subject",
    "date",
    "body",
    "labels",
    "labelIds",
    "unread",
    "starred",
    "important",
    "attachments",
}


class EnvironmentError(ValueError):
    """A client-safe error, with no runtime filesystem locations."""

    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def _fail(message: str, code: str = "invalid_arguments"):
    raise EnvironmentError(code, message)


def _bounded_json(value, limit):
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > 32 or count > 100_000:
            _fail("JSON nesting or item limit exceeded", "limit_exceeded")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                _fail("JSON object keys must be strings")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            _fail("Values must contain JSON data")
    try:
        encoded = json.dumps(value, allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError):
        _fail("Values must contain finite JSON data")
    if len(encoded) > limit:
        _fail("JSON byte limit exceeded", "limit_exceeded")


def _object(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        _fail("Object contains missing or unsupported fields")


def _string(value, field: str, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        _fail(f"{field} must be a string" + ("" if empty else " with a value"))
    return value


def _timestamp(value):
    if not isinstance(value, str) or not _STAMP.fullmatch(value):
        _fail("Timestamp must be RFC3339 with an explicit timezone")
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except (ValueError, OverflowError):
        _fail("Invalid RFC3339 timestamp")


def _zone(value):
    try:
        return ZoneInfo(_string(value, "timezone"))
    except (ZoneInfoNotFoundError, ValueError):
        _fail("Timezone must be a valid IANA timezone")


def _event_time(value, zone):
    _object(value, {"dateTime", "date", "timeZone"})
    if ("dateTime" in value) == ("date" in value):
        _fail("Event time requires exactly one of dateTime or date")
    local_zone = _zone(value["timeZone"]) if "timeZone" in value else zone
    if "dateTime" in value:
        return _timestamp(value["dateTime"])
    try:
        day = date.fromisoformat(value["date"])
        if day.isoformat() != value["date"]:
            _fail("All-day dates must use YYYY-MM-DD")
        return datetime.combine(day, time(), local_zone).astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        _fail("Invalid all-day event date")


def _event(value, zone, *, require_id=True):
    _object(value, _EVENT_FIELDS, {"start", "end"} | ({"id"} if require_id else set()))
    if "id" in value:
        _string(value["id"], "event id")
    for field in ("summary", "description", "location", "htmlLink"):
        if field in value and value[field] is not None:
            _string(value[field], field, empty=True)
    start, end = _event_time(value["start"], zone), _event_time(value["end"], zone)
    if ("date" in value["start"]) != ("date" in value["end"]) or end <= start:
        _fail("Event end must follow start using the same date/dateTime representation")
    if "status" in value and value["status"] not in ("confirmed", "tentative", "cancelled"):
        _fail("Unsupported event status")
    if "attendees" in value:
        if not isinstance(value["attendees"], list):
            _fail("attendees must be an array")
        for attendee in value["attendees"]:
            if not isinstance(attendee, dict):
                _fail("Each attendee must be an object")
            _string(attendee.get("email"), "attendee email")
    if "recurringEventId" in value:
        _string(value["recurringEventId"], "recurringEventId")
    if "originalStartTime" in value:
        _event_time(value["originalStartTime"], zone)
    for field in ("created", "updated"):
        if field in value:
            _timestamp(value[field])
    return copy.deepcopy(value)


def validate_environment(spec: dict) -> dict:
    """Validate and copy a fixture; grader checks/expectations are never fixture fields."""
    _bounded_json(spec, 2_000_000)
    _object(
        spec,
        {"kind", "fixture_origin", "now", "timezone", "calendars", "messages"},
        {"kind", "fixture_origin", "now", "timezone", "calendars", "messages"},
    )
    if spec["kind"] != KIND or spec["fixture_origin"] not in ("historical", "synthetic"):
        _fail("Unsupported environment kind or fixture origin")
    _timestamp(spec["now"])
    zone = _zone(spec["timezone"])
    if not isinstance(spec["calendars"], list) or not isinstance(spec["messages"], list):
        _fail("calendars and messages must be arrays")
    calendars = set()
    for calendar in spec["calendars"]:
        _object(calendar, {"id", "events"}, {"id", "events"})
        identifier = _string(calendar["id"], "calendar id")
        if identifier in calendars or not isinstance(calendar["events"], list):
            _fail("Calendar IDs must be unique and events must be an array")
        calendars.add(identifier)
        ids = set()
        for event in calendar["events"]:
            _event(event, zone)
            if event["id"] in ids:
                _fail("Event IDs must be unique within a calendar")
            ids.add(event["id"])
    ids = set()
    for message in spec["messages"]:
        _object(
            message, _MESSAGE_FIELDS, {"id", "threadId", "from", "to", "subject", "date", "body"}
        )
        for field in ("id", "threadId", "from"):
            _string(message[field], field)
        for field in ("subject", "body"):
            _string(message[field], field, empty=True)
        recipients = message["to"] if isinstance(message["to"], list) else [message["to"]]
        if not recipients:
            _fail("to requires at least one recipient")
        for recipient in recipients:
            _string(recipient, "to")
        _timestamp(message["date"])
        for field in ("labels", "labelIds"):
            if field in message and (
                not isinstance(message[field], list)
                or any(not isinstance(label, str) for label in message[field])
            ):
                _fail("Mail labels must be arrays of strings")
        for field in ("unread", "starred", "important"):
            if field in message and not isinstance(message[field], bool):
                _fail("Mail flags must be booleans")
        if "attachments" in message and not isinstance(message["attachments"], list):
            _fail("attachments must be an array")
        if message["id"] in ids:
            _fail("Message IDs must be unique")
        ids.add(message["id"])
    return copy.deepcopy(spec)


def _paths(runtime_dir):
    root = Path(runtime_dir).absolute()
    return {"runtime_dir": str(root), "state_path": str(root / "environment.json")}


def _persist(paths, state):
    write_json(Path(paths["state_path"]), state)


def initialize_environment(spec: dict, runtime_dir: Path) -> dict:
    validated = validate_environment(spec)
    root = private_directory(Path(runtime_dir))
    root.chmod(0o700)
    paths = _paths(root)
    _persist(
        paths,
        {
            "kind": KIND,
            "initial_state": validated,
            "final_state": copy.deepcopy(validated),
            "operations": [],
        },
    )
    return paths


def snapshot_environment(runtime_dir: Path) -> dict:
    try:
        state = read_json(Path(_paths(runtime_dir)["state_path"]))
    except (OSError, ValueError, TypeError):
        _fail("Environment state is unavailable", "state_unavailable")
    if not isinstance(state, dict) or state.get("kind") != KIND:
        _fail("Environment state is invalid", "state_unavailable")
    return copy.deepcopy(state)


def _page(items, arguments, context):
    maximum = arguments.get("max_results", 50)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 100:
        _fail("max_results must be between 1 and 100")
    fingerprint = hashlib.sha256(json.dumps([context, items], sort_keys=True).encode()).hexdigest()[
        :24
    ]
    offset = 0
    if arguments.get("page_token") is not None:
        try:
            token = json.loads(base64.urlsafe_b64decode(arguments["page_token"]).decode())
            offset = token["offset"]
            if (
                token["query"] != fingerprint
                or isinstance(offset, bool)
                or not isinstance(offset, int)
                or not 0 <= offset <= len(items)
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError, UnicodeError):
            _fail("Invalid or stale page token")
    following = offset + maximum
    token = (
        base64.urlsafe_b64encode(
            json.dumps({"offset": following, "query": fingerprint}, sort_keys=True).encode()
        ).decode()
        if following < len(items)
        else None
    )
    return copy.deepcopy(items[offset:following]), token


def _mail_matches(message, tokens, spec):
    labels = {label.upper() for label in message.get("labels", message.get("labelIds", ["INBOX"]))}
    for flag in ("unread", "starred", "important"):
        if flag in message:
            (labels.add if message[flag] else labels.discard)(flag.upper())
    for token in tokens:
        if ":" not in token:
            if (
                token.lower()
                not in " ".join(
                    str(message[field]) for field in ("from", "to", "subject", "body")
                ).lower()
            ):
                return False
            continue
        operator, value = token.split(":", 1)
        if not value:
            _fail("Search operators require a value")
        if operator in {"from", "to", "subject"}:
            match = value.lower() in str(message[operator]).lower()
        elif operator in {"after", "before"}:
            try:
                boundary = datetime.combine(
                    date.fromisoformat(value), time(), _zone(spec["timezone"])
                ).astimezone(UTC)
            except ValueError:
                _fail("Search date must use YYYY-MM-DD")
            match = (
                _timestamp(message["date"]) >= boundary
                if operator == "after"
                else _timestamp(message["date"]) < boundary
            )
        elif operator in {"newer_than", "older_than"}:
            if not re.fullmatch(r"[1-9]\d{0,5}d", value):
                _fail("Relative search age must be a positive number of days, such as 7d")
            try:
                boundary = _timestamp(spec["now"]) - timedelta(days=int(value[:-1]))
            except OverflowError:
                _fail("Relative search age is outside the supported date range")
            match = (
                _timestamp(message["date"]) > boundary
                if operator == "newer_than"
                else _timestamp(message["date"]) < boundary
            )
        elif operator == "is" and value in {"unread", "read", "starred", "important"}:
            match = "UNREAD" not in labels if value == "read" else value.upper() in labels
        elif operator == "in" and value == "inbox":
            match = "INBOX" in labels
        elif operator == "label":
            match = value.upper() in labels
        elif operator == "has" and value == "attachment":
            match = bool(message.get("attachments"))
        else:
            _fail("Unsupported mail search operator")
        if not match:
            return False
    return True


def _dispatch(spec, name, arguments, reserved_ids=()):
    zone = _zone(spec["timezone"])
    if name == "environment_info":
        _object(arguments, set())
        return {key: spec[key] for key in ("kind", "now", "timezone")}
    if name == "mail_get_message":
        _object(arguments, {"message_id"}, {"message_id"})
        identifier = _string(arguments["message_id"], "message_id")
        result = next(
            (message for message in spec["messages"] if message["id"] == identifier), None
        )
        if result is None:
            _fail("Unknown message", "not_found")
        return result
    if name == "mail_search":
        _object(arguments, {"query", "max_results", "page_token"}, {"query"})
        query = _string(arguments["query"], "query", empty=True)
        try:
            tokens = shlex.split(query)
        except ValueError:
            _fail("Search query has an unterminated quote")
        if any(
            token in {"OR", "AND"} or any(character in token for character in "{}()")
            for token in tokens
        ):
            _fail("Search grouping/OR is unsupported; use implicit AND")
        # Validate every operator even when the mailbox is empty or an earlier term has no hits.
        probe = {"from": "", "to": "", "subject": "", "body": "", "date": spec["now"]}
        for token in tokens:
            _mail_matches(probe, [token], spec)
        matches = [message for message in spec["messages"] if _mail_matches(message, tokens, spec)]
        matches.sort(key=lambda message: (_timestamp(message["date"]), message["id"]), reverse=True)
        page, token = _page(matches, arguments, [name, query])
        return {"messages": page, "next_page_token": token}
    if name not in {
        "calendar_list_events",
        "calendar_create_event",
        "calendar_update_event",
        "calendar_delete_event",
    }:
        _fail("Unknown tool", "unknown_tool")
    allowed = (
        {"calendar_id", "time_min", "time_max", "page_token", "max_results"}
        if name == "calendar_list_events"
        else {"calendar_id", "event"}
        if name == "calendar_create_event"
        else {"calendar_id", "event_id"}
        if name == "calendar_delete_event"
        else {"calendar_id", "event", "event_id"}
    )
    required = (
        {"event"}
        if name == "calendar_create_event"
        else {"event_id", "event"}
        if name == "calendar_update_event"
        else {"event_id"}
        if name == "calendar_delete_event"
        else set()
    )
    _object(arguments, allowed, required)
    calendar_id = _string(arguments.get("calendar_id", "primary"), "calendar_id")
    calendar = next((item for item in spec["calendars"] if item["id"] == calendar_id), None)
    if calendar is None:
        _fail("Unknown calendar", "not_found")
    if name == "calendar_list_events":
        lower = _timestamp(arguments["time_min"]) if "time_min" in arguments else None
        upper = _timestamp(arguments["time_max"]) if "time_max" in arguments else None
        if lower and upper and upper <= lower:
            _fail("time_max must follow time_min")
        events = [
            event
            for event in calendar["events"]
            if event.get("status") != "cancelled"
            and (lower is None or _event_time(event["end"], zone) > lower)
            and (upper is None or _event_time(event["start"], zone) < upper)
        ]
        events.sort(key=lambda event: (_event_time(event["start"], zone), event["id"]))
        page, token = _page(
            events,
            arguments,
            [name, calendar_id, arguments.get("time_min"), arguments.get("time_max")],
        )
        return {"events": page, "next_page_token": token}
    readonly = {
        "created",
        "updated",
        "etag",
        "htmlLink",
        "creator",
        "organizer",
        "recurringEventId",
        "originalStartTime",
    }
    if name == "calendar_create_event":
        if isinstance(arguments["event"], dict) and readonly & arguments["event"].keys():
            _fail("Read-only event metadata cannot be supplied on create")
        event = _event(arguments["event"], zone, require_id=False)
        ids = {item["id"] for item in calendar["events"]}
        unavailable_ids = ids | set(reserved_ids)
        if "id" not in event:
            number = 1
            while f"event-{number:06d}" in unavailable_ids:
                number += 1
            event["id"] = f"event-{number:06d}"
        if event["id"] in ids:
            _fail("Event already exists", "conflict")
        calendar["events"].append(event)
        return event
    identifier = _string(arguments["event_id"], "event_id")
    event = next((item for item in calendar["events"] if item["id"] == identifier), None)
    if event is None:
        _fail("Unknown event", "not_found")
    if name == "calendar_delete_event":
        calendar["events"].remove(event)
        return {"deleted": True, "id": identifier}
    if (
        not isinstance(arguments["event"], dict)
        or arguments["event"].get("id", identifier) != identifier
    ):
        _fail("Event updates cannot change the event id")
    if any(
        key in arguments["event"] and arguments["event"][key] != event.get(key) for key in readonly
    ):
        _fail("Read-only event metadata cannot be changed")
    updated = _event(event | arguments["event"], zone)
    calendar["events"][calendar["events"].index(event)] = updated
    return updated


def execute_tool(runtime_dir: Path, name: str, arguments: dict) -> dict:
    """Apply one atomic state transaction and audit both successful and rejected calls."""
    if not isinstance(name, str) or not isinstance(arguments, dict):
        _fail("Tool name and arguments must be a string and object")
    _bounded_json(arguments, 64_000)
    state = snapshot_environment(runtime_dir)
    if len(state["operations"]) >= 500:
        _fail("Environment operation limit exceeded", "limit_exceeded")
    candidate = copy.deepcopy(state["final_state"])
    operation = {
        "sequence": len(state["operations"]) + 1,
        "tool": name,
        "arguments": copy.deepcopy(arguments),
    }
    try:
        reserved = {
            operation["result"]["id"]
            for operation in state["operations"]
            if operation["tool"] == "calendar_create_event" and operation["status"] == "ok"
        }
        result = _dispatch(candidate, name, arguments, reserved)
        audit_result = result
        if name == "mail_get_message":
            audit_result = {"id": result["id"]}
        elif name in {"mail_search", "calendar_list_events"}:
            items = result.get("messages", result.get("events", []))
            audit_result = {
                "ids": [item["id"] for item in items],
                "next_page_token": result["next_page_token"],
            }
        operation.update(status="ok", result=copy.deepcopy(audit_result))
        state["final_state"] = candidate
    except EnvironmentError as exc:
        operation.update(status="error", error={"code": exc.code, "message": exc.message})
        state["operations"].append(operation)
        _persist(_paths(runtime_dir), state)
        raise
    state["operations"].append(operation)
    _persist(_paths(runtime_dir), state)
    return copy.deepcopy(result)
