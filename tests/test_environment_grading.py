"""Simulator outcomes cannot be replaced by a convincing completion claim."""

import pytest

from personalbench.environment_grading import check_environment_state
from personalbench.grading import grade


def event(identifier="new", hour="17:30", **extra):
    return {
        "id": identifier,
        "summary": "Conference",
        "start": {"dateTime": f"2026-08-19T{hour}:00-07:00"},
        "end": {"dateTime": "2026-08-19T20:30:00-07:00"},
        **extra,
    }


def snapshot(final):
    initial = {
        "calendars": [
            {"id": "primary", "events": [event("existing", "16:00", summary="Unrelated")]}
        ]
    }
    return {
        "kind": "calendar-mail-v1",
        "initial_state": initial,
        "final_state": {"calendars": [{"id": "primary", "events": final}]},
        "operations": [],
    }


def check():
    return {
        "calendar_id": "primary",
        "events": [{"summary": "Conference", "start": {"dateTime": "2026-08-20T00:30:00Z"}}],
    }


def preserved():
    return event("existing", "16:00", summary="Unrelated")


def test_correct_instant_preserves_unrelated_events():
    assert check_environment_state(check(), snapshot([preserved(), event()]))[0]


@pytest.mark.parametrize(
    "final",
    [
        [preserved()],
        [preserved(), event(hour="18:30")],
        [event()],
        [preserved(), event(), event("duplicate")],
        [event("existing", "16:00", summary="Changed"), event()],
    ],
)
def test_wrong_missing_duplicate_or_unrelated_mutation_fails(final):
    assert not check_environment_state(check(), snapshot(final))[0]


def test_text_claim_without_state_cannot_pass():
    task = {
        "criteria": [
            {"id": "mutation", "kind": "environment_state", "critical": True, "value": check()}
        ]
    }
    with_state = grade(
        task, {"status": "completed", "output": "Done", "environment": snapshot([preserved()])}
    )
    assert with_state["status"] == "scored" and with_state["success"] is False
    missing = grade(task, {"status": "completed", "output": "I created the event correctly"})
    assert missing["success"] is None


def test_cannot_overwrite_unrelated_event_to_satisfy_new_event_goal():
    assert not check_environment_state(check(), snapshot([event("existing")]))[0]


def test_explicit_existing_id_authorizes_only_that_reschedule():
    desired = check()
    desired["events"][0]["id"] = "existing"
    assert check_environment_state(desired, snapshot([event("existing")]))[0]


def test_cancelled_record_does_not_satisfy_active_event_creation():
    assert not check_environment_state(check(), snapshot([preserved(), event(status="cancelled")]))[
        0
    ]


def test_critical_state_check_overrides_fluent_high_rubric_score():
    task = {
        "criteria": [
            {
                "id": "mutation",
                "kind": "environment_state",
                "critical": True,
                "weight": 1,
                "value": check(),
            },
            {
                "id": "report",
                "kind": "contains",
                "description": "Clear report",
                "weight": 99,
                "value": "Done",
            },
        ]
    }
    result = grade(
        task, {"status": "completed", "output": "Done", "environment": snapshot([preserved()])}
    )
    assert result["quality_pct"] == 99 and result["success"] is False


def test_judge_receives_verified_environment_evidence():
    seen = []
    task = {
        "brief": "Create an event in the simulator",
        "criteria": [
            {"id": "report", "kind": "judge", "description": "Reports actual simulator outcomes"}
        ],
    }

    def judge(prompt):
        seen.append(prompt)
        return '{"scores":[{"id":"report","score":1,"evidence":"Simulator state confirms report"}]}'

    result = grade(
        task,
        {
            "status": "completed",
            "output": "Created the event in the mock account",
            "environment": snapshot([preserved(), event()]),
        },
        judge=judge,
    )
    assert result["success"] is True
    assert "simulated_environment_evidence" in seen[0] and "final_state" in seen[0]


def test_broad_and_specific_expected_events_use_distinct_matching():
    value = {"events": [{"summary": "Conference"}, {"summary": "Conference", "location": "Room A"}]}
    assert check_environment_state(
        value, snapshot([preserved(), event("a", location="Room A"), event("b", location="Room B")])
    )[0]
    assert not check_environment_state(
        value, snapshot([preserved(), event("a", location="Room A")])
    )[0]


def test_explicit_delete_does_not_allow_other_deletions():
    value = {"events": [], "absent_event_ids": ["existing"]}
    assert check_environment_state(value, snapshot([]))[0]
    assert not check_environment_state(value, snapshot([preserved()]))[0]


def test_inconsistent_snapshot_ids_reject_instead_of_silent_overwrite():
    with pytest.raises(ValueError, match="unique"):
        check_environment_state(check(), snapshot([preserved(), event(), event()]))
