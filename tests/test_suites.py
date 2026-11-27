"""Replayable suites keep unavailable catalogue work explicit and adaptations labelled."""

import copy

import pytest

from personalbench.schema import ValidationError, canonical_hash
from personalbench.suites import build_replayable_suite


def task(identifier, readiness="ready"):
    return {
        "id": identifier,
        "category": "planning",
        "brief": f"Complete the {identifier} outcome",
        "inputs": {"context.txt": "Preserved starting context"},
        "readiness": readiness,
        "criteria": [
            {"id": "outcome", "kind": "contains", "description": "Required result", "value": "done"}
        ],
        "reference": {"time_seconds": 30, "user_prompts": 1, "cost_usd": None},
        "historical": {"output": "Historical evidence remains private"},
    }


def bench(tasks):
    return {
        "schema_version": 1,
        "name": "Synthetic catalogue",
        "tasks": tasks,
        "scoring": {"success_threshold": 85},
    }


def variant(original, identifier="simulated", fixture_origin="synthetic"):
    value = task(identifier)
    value["environment"] = {
        "kind": "calendar-mail-v1",
        "fixture_origin": fixture_origin,
        "now": "2026-11-26T12:00:00Z",
        "timezone": "UTC",
        "calendars": [
            {
                "id": "primary",
                "events": [
                    {
                        "id": "planning",
                        "summary": "Planning",
                        "start": {"dateTime": "2026-11-27T09:00:00Z"},
                        "end": {"dateTime": "2026-11-27T10:00:00Z"},
                    }
                ],
            }
        ],
        "messages": [],
    }
    value["brief"] = (
        "Move the existing simulated Planning event to 10:00–11:00 UTC on November 27, 2026; preserve its ID and unrelated events."
    )
    value["execution_requirements"] = ["calendar-mail-v1"]
    value["criteria"].append(
        {
            "id": "rescheduled-state",
            "kind": "environment_state",
            "critical": True,
            "description": "Existing Planning event moves to the requested time with no duplicate or unrelated change.",
            "value": {
                "calendar_id": "primary",
                "events": [
                    {
                        "id": "planning",
                        "summary": "Planning",
                        "start": {"dateTime": "2026-11-27T10:00:00Z"},
                        "end": {"dateTime": "2026-11-27T11:00:00Z"},
                    }
                ],
                "preserve_existing": True,
                "no_extra_events": True,
            },
        }
    )
    value["provenance"] = {
        "adapted_from": original["id"],
        "adapted_from_hash": canonical_hash(original),
        "adaptation": "Replace unavailable historical account state with an explicitly synthetic simulated calendar/mail fixture.",
    }
    return value


def test_98_case_catalogue_produces_35_ready_cases_without_claiming_98_families():
    originals = [task(f"goal-{i}", "ready" if i < 34 else "incomplete") for i in range(98)]
    catalogue = bench(originals)
    catalogue["history"] = {"semantic_curation": {"family_count": 98}}
    simulated = variant(originals[34])
    suite = build_replayable_suite(catalogue, bench([simulated]))
    assert len(suite["tasks"]) == 35
    assert all(t["readiness"] == "ready" for t in suite["tasks"])
    assert "history" not in suite
    audit = suite["suite"]
    assert audit["catalogue_metadata"]["history"]["semantic_curation"]["family_count"] == 98
    assert audit["catalogue_hash"] == canonical_hash(catalogue)
    assert audit["total_tasks"] == 98
    assert len(audit["excluded"]) == 63
    assert audit["origin_counts"] == {"frozen_original": 34, "simulated": 1}
    assert audit["mode"] == "mixed_frozen_original_and_simulated"
    assert audit["variant_mapping"] == {"goal-34": "simulated"}
    assert audit["environment_fixture_origin_counts"] == {"historical": 0, "synthetic": 1}
    accounted = set(audit["included_original_ids"]) | {
        row["original_id"] for row in audit["excluded"]
    }
    assert accounted == {t["id"] for t in originals}
    assert suite["scoring"] == catalogue["scoring"]


def test_originals_and_variants_are_unchanged_and_source_objects_are_not_mutated():
    original = task("original")
    missing = task("missing", "unsupported")
    catalogue = bench([original, missing])
    adapted = variant(missing, fixture_origin="historical")
    variants = bench([adapted])
    before = copy.deepcopy((catalogue, variants))
    result = build_replayable_suite(catalogue, variants)
    assert result["tasks"] == [original, adapted]
    assert (catalogue, variants) == before
    result["tasks"][0]["inputs"]["context.txt"] = "changed suite"
    assert catalogue["tasks"][0]["inputs"]["context.txt"] == "Preserved starting context"
    assert result["suite"]["environment_fixture_origin_counts"] == {"historical": 1, "synthetic": 0}


def test_unavailable_originals_are_excluded_with_reasons_without_becoming_ready():
    catalogue = bench(
        [task("ready"), task("missing", "incomplete"), task("unsupported", "unsupported")]
    )
    result = build_replayable_suite(catalogue)
    assert [t["id"] for t in result["tasks"]] == ["ready"]
    excluded = {row["original_id"]: row for row in result["suite"]["excluded"]}
    assert excluded["missing"]["readiness"] == "incomplete"
    assert excluded["unsupported"]["readiness"] == "unsupported"
    assert all(row["reason"] for row in excluded.values())
    assert result["suite"]["mode"] == "frozen_original_only"
    assert catalogue["tasks"][1]["readiness"] == "incomplete"


def test_no_runnable_tasks_is_an_error():
    with pytest.raises(ValidationError, match="No ready"):
        build_replayable_suite(bench([task("missing", "incomplete")]))


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda t: t["provenance"].update(adapted_from="unknown"), "catalogue task"),
        (lambda t: t["provenance"].update(adapted_from_hash="stale"), "does not match"),
        (lambda t: t["provenance"].update(adaptation="  "), "adaptation"),
        (lambda t: t.update(readiness="incomplete"), "must be ready"),
        (lambda t: t.pop("environment"), "declared.*environment"),
        (lambda t: t["provenance"].update(family_id="invented"), "invent or change"),
    ],
)
def test_invalid_adaptations_are_rejected(mutation, match):
    original = task("original", "unsupported")
    adapted = variant(original)
    mutation(adapted)
    with pytest.raises(ValidationError, match=match):
        build_replayable_suite(bench([original]), bench([adapted]))


def test_multiple_variants_for_same_original_are_rejected():
    original = task("original", "unsupported")
    with pytest.raises(ValidationError, match="one variant per original"):
        build_replayable_suite(
            bench([original]), bench([variant(original, "a"), variant(original, "b")])
        )


def test_variants_replace_ready_original_instead_of_creating_an_extra_family():
    original = task("original")
    adapted = variant(original)
    result = build_replayable_suite(bench([original]), bench([adapted]))
    assert result["tasks"] == [adapted]
    assert result["suite"]["included_original_ids"] == ["original"]
    assert result["suite"]["mode"] == "simulated_only"


def test_variant_id_collision_with_another_original_is_rejected():
    ready, missing = task("ready"), task("missing", "incomplete")
    with pytest.raises(ValidationError, match="IDs collide"):
        build_replayable_suite(bench([ready, missing]), bench([variant(missing, "ready")]))


def test_two_catalogue_cases_cannot_create_duplicate_semantic_family():
    catalogue = bench([task("a"), task("b")])
    catalogue["history"] = {
        "semantic_curation": {
            "families": [
                {
                    "family_id": "family-a",
                    "member_ids": ["a", "b"],
                    "representative_task_id": "a",
                }
            ]
        }
    }
    with pytest.raises(ValidationError, match="one task per catalogue family"):
        build_replayable_suite(catalogue)


def test_invalid_catalogue_and_environment_are_validated():
    invalid = bench([task("a")])
    invalid["tasks"][0]["inputs"] = {"../outside": "content"}
    with pytest.raises(ValidationError):
        build_replayable_suite(invalid)
    original = task("missing", "unsupported")
    adapted = variant(original)
    adapted["environment"]["fixture_origin"] = "unlabelled"
    with pytest.raises(ValueError, match="fixture origin"):
        build_replayable_suite(bench([original]), bench([adapted]))


@pytest.mark.parametrize("critical", [False, None])
def test_variants_require_critical_state_outcome_instead_of_text_claims(critical):
    original = task("missing", "unsupported")
    adapted = variant(original)
    if critical is None:
        adapted["criteria"] = [c for c in adapted["criteria"] if c["kind"] != "environment_state"]
    else:
        adapted["criteria"][-1]["critical"] = critical
    with pytest.raises(ValidationError, match="critical environment_state"):
        build_replayable_suite(bench([original]), bench([adapted]))


@pytest.mark.parametrize(
    "requirement", ["live_accounts", "publication", "ui", "gpu", "unavailable_tool"]
)
def test_variants_cannot_retain_unavailable_execution_capabilities(requirement):
    original = task("missing", "unsupported")
    adapted = variant(original)
    adapted["execution_requirements"].append(requirement)
    with pytest.raises(ValidationError, match="unsupported execution requirements"):
        build_replayable_suite(bench([original]), bench([adapted]))


def test_stateful_variant_can_omit_execution_requirement_declaration_for_compatibility():
    original = task("missing", "unsupported")
    adapted = variant(original)
    adapted.pop("execution_requirements")
    suite = build_replayable_suite(bench([original]), bench([adapted]))
    assert suite["tasks"] == [adapted]


def test_suite_cli_exports_ready_cases_and_preserves_backlog(tmp_path, capsys):
    from personalbench.cli import main
    from personalbench.privacy import read_json, write_json

    catalogue = tmp_path / "catalogue.json"
    output = tmp_path / "suite.json"
    write_json(catalogue, bench([task("ready"), task("missing", "incomplete")]))
    assert main(["suite", str(catalogue), "--output", str(output)]) == 0
    exported = read_json(output)
    assert [t["id"] for t in exported["tasks"]] == ["ready"]
    assert exported["suite"]["excluded"][0]["original_id"] == "missing"
    assert '"catalogue_tasks": 2' in capsys.readouterr().out
