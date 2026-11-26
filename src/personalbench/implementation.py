"""Verify harmless scoring-source changes without executing historical source code."""

from __future__ import annotations

import ast
import hashlib
from datetime import UTC, datetime
from pathlib import Path

from .privacy import private_directory, write_json, write_private
from .schema import canonical_hash


def implementation_sources() -> dict[str, bytes]:
    return {
        name: (Path(__file__).parent / name).read_bytes() for name in ("grading.py", "scoring.py")
    }


def aggregation_identity(source: bytes) -> tuple[str, list[str]]:
    """Hash aggregation's local dependency closure and all module-level bindings.

    All loaded names are followed conservatively, including local names that shadow
    module helpers. Nontrivial defaults/decorators are import-time code, so their
    definitions are also included. Historical source is parsed, never imported.
    """
    if len(source) > 1_000_000:
        raise ValueError("Scoring source exceeds the compatibility inspection limit")
    try:
        tree = ast.parse(source)
    except (SyntaxError, UnicodeError) as exc:
        raise ValueError("Cannot parse historical scoring source") from exc
    definitions = [
        node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    functions = {node.name: node for node in definitions}
    if len(functions) != len(definitions) or "summarize" not in functions:
        raise ValueError("Scoring source needs an unambiguous summarize definition")
    supporting = [node for node in tree.body if node not in definitions]
    postponed = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )
    pending = ["summarize"]
    pending.extend(
        node.id
        for item in supporting
        for node in ast.walk(item)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in functions
    )
    for node in definitions:
        defaults = [*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]
        if (
            node.decorator_list
            or any(not isinstance(value, ast.Constant) for value in defaults)
            or not postponed
        ):
            pending.append(node.name)
    selected = set()
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        selected.add(name)
        pending.extend(
            node.id
            for node in ast.walk(functions[name])
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in functions
            and node.id not in selected
        )
    identity = ast.Module(
        body=supporting + [functions[name] for name in sorted(selected)], type_ignores=[]
    )
    return hashlib.sha256(
        ast.dump(identity, include_attributes=False).encode()
    ).hexdigest(), sorted(selected)


def _immutable(path: Path, raw: bytes):
    private_directory(path.parent)
    if path.is_symlink():
        raise ValueError("Implementation snapshots cannot follow symbolic links")
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("Implementation snapshot hash mismatch")
        return
    write_private(path, raw.decode("utf-8"))


def snapshot_scoring(output: Path, source: bytes) -> Path:
    digest = hashlib.sha256(source).hexdigest()
    path = output / "implementations" / f"scoring-{digest}.py"
    _immutable(path, source)
    return path


def migrate_comparison_only_policy(
    report: dict, policy: dict, source: bytes, output: Path, expected_summary: dict
) -> dict:
    """Upgrade only a proven comparison-only source change under the caller's run lock."""
    old = report.get("scoring_policy", {})
    if canonical_hash(old) != report.get("scoring_hash"):
        raise ValueError("Cannot resume: saved scoring policy hash is inconsistent")
    old_implementation = old.get("grader_implementation", {})
    new_implementation = policy.get("grader_implementation", {})
    if hashlib.sha256(source).hexdigest() != new_implementation.get("scoring.py"):
        raise ValueError("Cannot resume: current scoring source identity is inconsistent")
    prior_hash = old_implementation.get("scoring.py")
    if (
        not isinstance(prior_hash, str)
        or len(prior_hash) != 64
        or any(c not in "0123456789abcdef" for c in prior_hash)
    ):
        raise ValueError("Cannot resume: historical scoring source identity is invalid")
    expected_policy = {
        **old,
        "grader_implementation": {
            **old_implementation,
            "scoring.py": new_implementation.get("scoring.py"),
        },
    }
    if expected_policy != policy:
        raise ValueError("Cannot resume with changed grader, judge, or scoring settings")
    previous_path = output / "implementations" / f"scoring-{prior_hash}.py"
    private_directory(previous_path.parent)
    if previous_path.is_symlink() or not previous_path.is_file():
        raise ValueError("Cannot resume: verified historical scoring source snapshot is missing")
    previous_source = previous_path.read_bytes()
    if hashlib.sha256(previous_source).hexdigest() != prior_hash:
        raise ValueError("Cannot resume: historical scoring source snapshot hash mismatch")
    previous_ast, previous_helpers = aggregation_identity(previous_source)
    current_ast, current_helpers = aggregation_identity(source)
    if previous_ast != current_ast or previous_helpers != current_helpers:
        raise ValueError("Cannot resume with changed scoring aggregation or its dependencies")
    if report.get("summary") != expected_summary:
        raise ValueError(
            "Cannot resume: saved summary does not reproduce under unchanged aggregation"
        )
    current_path = snapshot_scoring(output, source)
    original = (output / "report.json").read_bytes()
    backup_hash = hashlib.sha256(original).hexdigest()
    backup = output / f"report.before-policy-{backup_hash}.json"
    _immutable(backup, original)
    prior_policy_hash = report["scoring_hash"]
    report["scoring_policy"] = policy
    report["scoring_hash"] = canonical_hash(policy)
    report.setdefault("implementation_migrations", []).append(
        {
            "kind": "verified_comparison_only_scoring_change",
            "at": datetime.now(UTC).isoformat(),
            "previous_scoring_hash": prior_policy_hash,
            "current_scoring_hash": report["scoring_hash"],
            "previous_source_sha256": prior_hash,
            "current_source_sha256": new_implementation["scoring.py"],
            "aggregation_ast_sha256": current_ast,
            "aggregation_functions": current_helpers,
            "previous_source_snapshot": str(previous_path),
            "current_source_snapshot": str(current_path),
            "report_backup": str(backup),
            "report_backup_sha256": backup_hash,
        }
    )
    write_json(output / "report.json", report)
    return report
