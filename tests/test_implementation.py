"""Synthetic policy changes prove migration never rewrites grading evidence."""

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from personalbench.engine import evaluate
from personalbench.implementation import aggregation_identity, implementation_sources


def bench():
    return {
        "schema_version": 1,
        "name": "Synthetic source migration",
        "tasks": [
            {
                "id": "a",
                "category": "coding",
                "brief": "Return done",
                "inputs": {},
                "criteria": [
                    {
                        "id": "result",
                        "kind": "contains",
                        "description": "Return done",
                        "value": "done",
                    }
                ],
            }
        ],
    }


def config():
    return {
        "name": "Synthetic fixture",
        "harness": "fixture",
        "model": "synthetic",
        "responses": {"a": "done"},
        "cost_usd": 0.1,
    }


def sources():
    old = implementation_sources()
    new = {
        **old,
        "scoring.py": old["scoring.py"]
        + b'\n\ndef comparison_presentation_only():\n    return "comparison"\n',
    }
    return old, new


def test_comparison_only_upgrade_keeps_trials_and_never_repeats_candidate(tmp_path):
    old, new = sources()
    with patch("personalbench.engine.implementation_sources", return_value=old):
        first = evaluate(bench(), config(), tmp_path)
    original = (tmp_path / "report.json").read_bytes()
    with (
        patch("personalbench.engine.implementation_sources", return_value=new),
        patch(
            "personalbench.engine.run_candidate",
            side_effect=AssertionError("Paid candidate repeated"),
        ),
    ):
        upgraded = evaluate(bench(), config(), tmp_path, resume=True)
        again = evaluate(bench(), config(), tmp_path, resume=True)
    assert upgraded["trials"] == first["trials"]
    assert upgraded["judge_calls"] == first["judge_calls"]
    assert upgraded["summary"] == first["summary"]
    assert upgraded["scoring_hash"] != first["scoring_hash"]
    assert len(again["implementation_migrations"]) == 1
    migration = upgraded["implementation_migrations"][0]
    assert Path(migration["report_backup"]).read_bytes() == original
    for source in (old["scoring.py"], new["scoring.py"]):
        digest = hashlib.sha256(source).hexdigest()
        assert (tmp_path / "implementations" / f"scoring-{digest}.py").read_bytes() == source


@pytest.mark.parametrize("problem", ["missing", "tampered", "aggregation", "judge", "grading"])
def test_policy_upgrade_refuses_unproven_or_material_changes(tmp_path, problem):
    old, new = sources()
    with patch("personalbench.engine.implementation_sources", return_value=old):
        first = evaluate(bench(), config(), tmp_path)
    digest = hashlib.sha256(old["scoring.py"]).hexdigest()
    previous = tmp_path / "implementations" / f"scoring-{digest}.py"
    kwargs = {}
    if problem == "missing":
        previous.unlink()
    elif problem == "tampered":
        previous.write_text("unknown source")
    elif problem == "aggregation":
        new["scoring.py"] = old["scoring.py"].replace(
            b"return 100.0 * min(1.0, reference / observed)",
            b"return 50.0 * min(1.0, reference / observed)",
        )
    elif problem == "judge":
        kwargs = {"judge_config": config(), "allow_unsafe_local": True}
    else:
        new["grading.py"] += b"\n# grader revision\n"
    with (
        patch("personalbench.engine.implementation_sources", return_value=new),
        patch(
            "personalbench.engine.run_candidate", side_effect=AssertionError("Unexpected candidate")
        ),
        pytest.raises(ValueError, match="Cannot resume"),
    ):
        evaluate(bench(), config(), tmp_path, resume=True, **kwargs)
    assert (
        json.loads((tmp_path / "report.json").read_text())["scoring_hash"] == first["scoring_hash"]
    )


def test_aggregation_proof_includes_loaded_helpers_and_global_aliases():
    original = b"from __future__ import annotations\ndef helper():\n return 1\nalias=helper\ndef summarize():\n return alias()\ndef compare():\n return 0\n"
    harmless = original.replace(b"return 0", b"return 7")
    changed = original.replace(b"return 1", b"return 2")
    assert aggregation_identity(original) == aggregation_identity(harmless)
    assert aggregation_identity(original) != aggregation_identity(changed)
