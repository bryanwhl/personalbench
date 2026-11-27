"""Outcome assertions over trusted simulator snapshots, never candidate claims."""

from __future__ import annotations

from datetime import datetime


def validate_state_check(value):
    if not isinstance(value, dict) or set(value) - {
        "calendar_id",
        "events",
        "absent_event_ids",
        "preserve_existing",
        "no_extra_events",
    }:
        raise ValueError("Environment state checks need supported calendar assertion fields")
    if not isinstance(value.get("calendar_id", "primary"), str):
        raise TypeError("calendar_id must be text")
    expected = value.get("events", [])
    if (
        not isinstance(expected, list)
        or len(expected) > 100
        or any(not isinstance(e, dict) or not e for e in expected)
    ):
        raise ValueError("events must contain nonempty expected event objects")
    absent = value.get("absent_event_ids", [])
    if not isinstance(absent, list) or any(not isinstance(i, str) or not i for i in absent):
        raise ValueError("absent_event_ids must contain event IDs")
    if not expected and not absent:
        raise ValueError("A state check must assert an event outcome")
    for key in ("preserve_existing", "no_extra_events"):
        if not isinstance(value.get(key, True), bool):
            raise TypeError(f"{key} must be a boolean")


def _matches(actual, expected, key=None):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and _matches(actual[k], v, k) for k, v in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(_matches(a, e) for a, e in zip(actual, expected))
        )
    if key == "dateTime" and isinstance(actual, str) and isinstance(expected, str):
        try:
            a = datetime.fromisoformat(actual)
            e = datetime.fromisoformat(expected)
            return a.utcoffset() is not None and e.utcoffset() is not None and a == e
        except ValueError:
            return False
    return type(actual) is type(expected) and actual == expected


def _events(state, calendar_id):
    for calendar in state.get("calendars", []):
        if calendar.get("id") == calendar_id:
            events = calendar.get("events")
            if not isinstance(events, list) or any(not isinstance(e, dict) for e in events):
                raise ValueError("Malformed captured calendar events")
            ids = [e.get("id") for e in events]
            if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
                raise ValueError("Captured calendar event IDs must be nonempty and unique")
            return events
    raise ValueError("Required simulator calendar was not captured")


def check_environment_state(value, snapshot):
    validate_state_check(value)
    if not isinstance(snapshot, dict) or snapshot.get("kind") != "calendar-mail-v1":
        raise ValueError("Trusted calendar/mail environment snapshot is unavailable")
    calendar_id = value.get("calendar_id", "primary")
    initial = _events(snapshot["initial_state"], calendar_id)
    final = _events(snapshot["final_state"], calendar_id)
    expected = value.get("events", [])
    absent = set(value.get("absent_event_ids", []))
    if any(e["id"] in absent for e in final):
        return False, "An event required to be absent remains in the simulator"
    # Bipartite matching gives each outcome its own event without exponential
    # backtracking when broad and specific expectations overlap.
    candidates = [
        [
            i
            for i, event in enumerate(final)
            if (event.get("status") != "cancelled" or target.get("status") == "cancelled")
            and _matches(event, target)
        ]
        for target in expected
    ]
    order = sorted(range(len(expected)), key=lambda i: len(candidates[i]))

    owner = {}

    def assign(target, visited):
        for index in candidates[target]:
            if index not in visited:
                visited.add(index)
                if index not in owner or assign(owner[index], visited):
                    owner[index] = target
                    return True
        return False

    if not all(assign(target, set()) for target in order):
        return (
            False,
            "The simulator does not contain every requested event with the required fields",
        )
    matched = set(owner)
    selected_ids = {final[i]["id"] for i in matched}
    authorized_updates = {target["id"] for target in expected if isinstance(target.get("id"), str)}
    initial_by_id = {e["id"]: e for e in initial}
    final_by_id = {e["id"]: e for e in final}
    if value.get("preserve_existing", True):
        for identifier, event in initial_by_id.items():
            if (
                identifier not in authorized_updates | absent
                and final_by_id.get(identifier) != event
            ):
                return False, "An unrelated initial event was changed or deleted"
    if value.get("no_extra_events", True) and set(final_by_id) - set(initial_by_id) - selected_ids:
        return False, "The simulator contains additional unexpected events or duplicates"
    return True, "Trusted simulator state satisfies the requested events and preservation checks"
