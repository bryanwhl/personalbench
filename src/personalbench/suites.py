"""Build runnable suites while retaining the source catalogue's coverage accounting."""

from __future__ import annotations

import copy
from collections import Counter

from .schema import ValidationError, canonical_hash, validate_benchmark

SUITE_VERSION = "replayable-suite-v1"


def _catalogue_families(catalogue: dict) -> dict[str, str]:
    """Use declared semantic families for identity, never invent new task families."""
    history = catalogue.get("history", {})
    curation = history.get("semantic_curation", {}) if isinstance(history, dict) else {}
    families = curation.get("families", []) if isinstance(curation, dict) else []
    if not isinstance(families, list):
        raise ValidationError("Catalogue semantic families must be an array")
    result = {}
    for family in families:
        if not isinstance(family, dict):
            raise ValidationError("Catalogue semantic families must be objects")
        family_id = family.get("family_id")
        if not isinstance(family_id, str) or not family_id.strip():
            raise ValidationError("Catalogue semantic families need nonempty family IDs")
        members = family.get("member_ids", [])
        if not isinstance(members, list) or any(not isinstance(member, str) for member in members):
            raise ValidationError("Catalogue semantic family members must be task IDs")
        for member in members:
            if member in result and result[member] != family_id:
                raise ValidationError("Catalogue task belongs to multiple semantic families")
            result[member] = family_id
        representative = family.get("representative_task_id")
        if representative is not None and (
            not isinstance(representative, str) or representative not in members
        ):
            raise ValidationError("Catalogue family representative must be one of its members")
    return result


def build_replayable_suite(catalogue: dict, variants: dict | None = None) -> dict:
    """Select ready originals or explicitly traced environment replacements, once per goal.

    This creates a new benchmark identity. Missing original state stays missing in the
    catalogue; a supplied simulation is labelled as an adaptation, never a recovery.
    The builder neither creates fixtures nor generates candidate/reference answers.
    Current adaptations require a critical calendar action outcome assertion. Read-only
    mail adaptations need a future outcome grader; a text acknowledgement is insufficient.
    """
    validate_benchmark(catalogue)
    originals = {task["id"]: task for task in catalogue["tasks"]}
    families = _catalogue_families(catalogue)
    replacements = {}
    if variants is not None:
        validate_benchmark(variants)
        for variant in variants["tasks"]:
            provenance = variant.get("provenance")
            if not isinstance(provenance, dict):
                raise ValidationError("Every variant needs adaptation provenance")
            original_id = provenance.get("adapted_from")
            if not isinstance(original_id, str) or original_id not in originals:
                raise ValidationError("Variant adapted_from must identify a catalogue task")
            if original_id in replacements:
                raise ValidationError("Only one variant per original catalogue task is allowed")
            if provenance.get("adapted_from_hash") != canonical_hash(originals[original_id]):
                raise ValidationError("Variant adapted_from_hash does not match the original task")
            adaptation = provenance.get("adaptation")
            if not isinstance(adaptation, str) or not adaptation.strip():
                raise ValidationError("Variant needs a nonempty explanation of its adaptation")
            if variant.get("readiness", "ready") != "ready":
                raise ValidationError("Variants must be ready before entering a replayable suite")
            environment = variant.get("environment")
            if not isinstance(environment, dict) or environment.get("kind") != "calendar-mail-v1":
                raise ValidationError("Variants need a declared calendar-mail-v1 environment")
            if not any(
                criterion["kind"] == "environment_state" and criterion.get("critical") is True
                for criterion in variant["criteria"]
            ):
                raise ValidationError(
                    "Calendar/mail action variants need a critical environment_state outcome assertion"
                )
            requirements = variant.get("execution_requirements", [])
            if (
                not isinstance(requirements, list)
                or any(not isinstance(requirement, str) for requirement in requirements)
                or set(requirements) - {"calendar-mail-v1", "workspace"}
            ):
                raise ValidationError(
                    "Variants retain unsupported execution requirements; only calendar-mail-v1 and workspace are replayable"
                )
            # Benchmark validation checks all fixture fields and the historical/synthetic label.
            claimed_family = provenance.get("family_id")
            original_provenance = originals[original_id].get("provenance", {})
            if not isinstance(original_provenance, dict):
                raise ValidationError("Catalogue task provenance must be an object")
            expected_family = families.get(original_id) or original_provenance.get("family_id")
            if claimed_family is not None and claimed_family != expected_family:
                raise ValidationError("Variant cannot invent or change its original task family")
            replacements[original_id] = variant

    tasks, included, excluded, mapping = [], [], [], {}
    seen_ids, seen_families = set(), set()
    origin_counts = Counter({"frozen_original": 0, "simulated": 0})
    fixture_counts = Counter({"historical": 0, "synthetic": 0})
    for original_id, original in originals.items():
        if original_id in replacements:
            chosen = replacements[original_id]
            mapping[original_id] = chosen["id"]
            origin = "simulated"
        elif original.get("readiness", "ready") == "ready":
            chosen, origin = original, "frozen_original"
        else:
            readiness = original["readiness"]
            excluded.append(
                {
                    "original_id": original_id,
                    "readiness": readiness,
                    "reason": f"Catalogue task is {readiness}; no explicit ready environment variant supplied.",
                }
            )
            continue
        if chosen["id"] in seen_ids:
            raise ValidationError("Suite task IDs collide between originals and variants")
        family_id = families.get(original_id)
        if family_id is not None:
            if family_id in seen_families:
                raise ValidationError("Replayable suites permit only one task per catalogue family")
            seen_families.add(family_id)
        seen_ids.add(chosen["id"])
        tasks.append(copy.deepcopy(chosen))
        included.append(original_id)
        origin_counts[origin] += 1
        if chosen.get("environment"):
            fixture_counts[chosen["environment"]["fixture_origin"]] += 1
    if not tasks:
        raise ValidationError("No ready tasks or explicit ready environment variants form a suite")
    mode = (
        "mixed_frozen_original_and_simulated"
        if all(origin_counts.values())
        else "simulated_only"
        if origin_counts["simulated"]
        else "frozen_original_only"
    )
    suite = {
        "version": SUITE_VERSION,
        "catalogue_hash": canonical_hash(catalogue),
        "catalogue_name": catalogue["name"],
        "total_tasks": len(originals),
        "included_original_ids": included,
        "variant_mapping": mapping,
        "excluded": excluded,
        "origin_counts": dict(origin_counts),
        "environment_fixture_origin_counts": dict(fixture_counts),
        "mode": mode,
        "catalogue_metadata": copy.deepcopy(
            {key: value for key, value in catalogue.items() if key not in {"tasks", "scoring"}}
        ),
    }
    if variants is not None:
        suite["variants_hash"] = canonical_hash(variants)
    result = {
        "schema_version": catalogue["schema_version"],
        "name": f"{catalogue['name']} — replayable suite",
        "tasks": tasks,
        "scoring": copy.deepcopy(catalogue.get("scoring", {})),
        "suite": suite,
    }
    return validate_benchmark(result)
