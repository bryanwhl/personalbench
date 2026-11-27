"""Versioned, dependency-free validation for portable benchmark packages."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import PurePosixPath


class ValidationError(ValueError):
    """An artifact cannot be used without changing its declared meaning."""


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def number(value, label, *, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValidationError(f"{label} must be a finite number")
    if value < minimum or (maximum is not None and value > maximum):
        raise ValidationError(f"{label} is outside its allowed range")
    return value


def safe_relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValidationError("Input path must be a relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(
        p.lower() in {"..", ".git", ".codex", ".claude"} for p in path.parts
    ):
        raise ValidationError("Input path escapes workspace or supplies harness configuration")
    if ":" in value or value in {".", ""} or str(path) != value:
        raise ValidationError("Invalid input path")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be nonempty text")


def validate_benchmark(bench: dict) -> dict:
    if not isinstance(bench, dict) or bench.get("schema_version") != 1:
        raise ValidationError("Expected benchmark schema_version 1")
    _text(bench.get("name"), "name")
    tasks = bench.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValidationError("Benchmark needs at least one task")
    seen = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise ValidationError("Task must be an object")
        tid = task.get("id", "")
        if not isinstance(tid, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", tid):
            raise ValidationError("Task id must be a short filename-safe identifier")
        if tid in seen:
            raise ValidationError("Duplicate task id")
        seen.add(tid)
        _text(task.get("brief"), "brief")
        _text(task.get("category"), "category")
        inputs = task.get("inputs", {})
        if not isinstance(inputs, dict):
            raise ValidationError("inputs must map paths to text")
        input_paths = {PurePosixPath(path) for path in inputs if isinstance(path, str)}
        if any(parent in input_paths for path in input_paths for parent in path.parents):
            raise ValidationError("Input file paths overlap with directories")
        for path, content in inputs.items():
            safe_relative(path)
            if not isinstance(content, str):
                raise ValidationError("V1 inputs must be UTF-8 text")
        if sum(len(v.encode()) for v in inputs.values()) > 20_000_000:
            raise ValidationError("Task inputs exceed the V1 20 MB limit")
        criteria = task.get("criteria")
        if not isinstance(criteria, list) or not criteria:
            raise ValidationError("Task needs automated grading criteria")
        cids = set()
        for criterion in criteria:
            if not isinstance(criterion, dict):
                raise ValidationError("Criterion must be an object")
            _text(criterion.get("id"), "criterion id")
            if criterion["id"] in cids:
                raise ValidationError("Duplicate criterion id")
            cids.add(criterion["id"])
            _text(criterion.get("description"), "criterion description")
            if criterion.get("kind") not in {
                "contains",
                "regex",
                "json",
                "file_contains",
                "judge",
                "command",
                "environment_state",
            }:
                raise ValidationError("Unsupported grader kind")
            number(criterion.get("weight", 1), "criterion weight", minimum=0.000001)
            if criterion["kind"] == "environment_state":
                from .environment_grading import validate_state_check

                try:
                    validate_state_check(criterion.get("value"))
                except (ValueError, TypeError) as exc:
                    raise ValidationError(str(exc)) from exc
                if not task.get("environment"):
                    raise ValidationError("Environment state criteria need a declared environment")
            if criterion["kind"] == "command":
                command = criterion.get("value")
                if (
                    not isinstance(command, list)
                    or not command
                    or not all(isinstance(v, str) and v for v in command)
                ):
                    raise ValidationError("Command criterion needs an argv array")
                if not isinstance(criterion.get("tests", {}), dict):
                    raise ValidationError("tests must map paths to text")
                for filename, text in criterion.get("tests", {}).items():
                    safe_relative(filename)
                    if not isinstance(text, str):
                        raise ValidationError("Grader tests must be text")
                number(
                    criterion.get("timeout_seconds", 30), "grader timeout", minimum=0.1, maximum=60
                )
            if not isinstance(criterion.get("critical", False), bool):
                raise ValidationError("critical must be a boolean")
            target = criterion.get("target", "output")
            if criterion["kind"] == "file_contains" and target == "output":
                raise ValidationError("file_contains requires a relative file target")
            if target != "output":
                safe_relative(target)
            if criterion["kind"] in {"contains", "regex", "file_contains"}:
                _text(criterion.get("value"), "criterion value")
        reference = task.get("reference", {})
        if not isinstance(reference, dict):
            raise ValidationError("reference must be an object")
        if not isinstance(task.get("historical", {}), dict):
            raise ValidationError("historical must be an object")
        for key in ("time_seconds", "user_prompts", "cost_usd"):
            if reference.get(key) is not None:
                number(reference[key], f"reference.{key}", minimum=0.000000001)
        if task.get("weight", 1) != 1:
            raise ValidationError("V1 uses equal task weights")
        if task.get("readiness", "ready") not in {"ready", "incomplete", "unsupported"}:
            raise ValidationError("Invalid task readiness")
        if "environment" in task:
            from .environments import validate_environment

            environment = validate_environment(task["environment"])
            calendar_ids = {calendar["id"] for calendar in environment["calendars"]}
            for criterion in criteria:
                if (
                    criterion["kind"] == "environment_state"
                    and criterion["value"].get("calendar_id", "primary") not in calendar_ids
                ):
                    raise ValidationError(
                        "State criterion refers to a calendar absent from its fixture"
                    )
    scoring = bench.get("scoring", {})
    if not isinstance(scoring, dict):
        raise ValidationError("scoring must be an object")
    number(scoring.get("success_threshold", 80), "success threshold", maximum=100)
    weights = scoring.get("efficiency_weights", {"time": 1 / 3, "prompts": 1 / 3, "cost": 1 / 3})
    if not isinstance(weights, dict) or set(weights) != {"time", "prompts", "cost"}:
        raise ValidationError("Efficiency requires time, prompts, and cost weights")
    for value in weights.values():
        number(value, "efficiency weight", minimum=0.000001, maximum=1)
    if not math.isclose(sum(weights.values()), 1):
        raise ValidationError("Efficiency weights must sum to one")
    canonical_hash(bench)
    return bench


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValidationError("Configuration must be an object")
    allowed = {
        "name",
        "harness",
        "model",
        "timeout_seconds",
        "sandbox",
        "command",
        "prices",
        "reasoning_effort",
        "responses",
        "usage",
        "cost_usd",
        "env_allowlist",
        "tools",
        "instructions",
        "max_output_bytes",
        "price_date",
    }
    if set(config) - allowed:
        raise ValidationError(
            f"Unknown configuration fields: {', '.join(sorted(set(config) - allowed))}"
        )
    _text(config.get("name"), "configuration name")
    _text(config.get("model"), "model")
    if config.get("harness") not in {"codex", "claude", "command", "fixture"}:
        raise ValidationError("Unsupported harness; use the command adapter for custom harnesses")
    number(config.get("timeout_seconds", 120), "timeout_seconds", minimum=0.1, maximum=86400)
    if config.get("sandbox", "read-only") not in {"read-only", "workspace-write"}:
        raise ValidationError("Only read-only and workspace-write harness policies are supported")
    if config.get("tools", "none") not in {"none", "workspace", "environment"}:
        raise ValidationError("tools must be none, workspace or environment")
    if config.get("tools") == "environment" and config["harness"] != "codex":
        raise ValidationError("Stateful environment tools currently require the Codex harness")
    if "prices" in config:
        if not isinstance(config["prices"], dict):
            raise ValidationError("prices must be an object")
        for key in ("input_per_million", "cached_input_per_million", "output_per_million"):
            number(config["prices"].get(key), f"prices.{key}")
        _text(config.get("price_date"), "price_date (record when rates were obtained)")
    if config["harness"] == "command":
        cmd = config.get("command")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) and x for x in cmd):
            raise ValidationError("command must be a nonempty argv array; no shell interpolation")
    env = config.get("env_allowlist", [])
    if not isinstance(env, list) or not all(
        isinstance(x, str) and re.fullmatch(r"[A-Z_][A-Z0-9_]*", x) for x in env
    ):
        raise ValidationError("env_allowlist contains environment variable names only")
    if set(env) & {
        "HOME",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "PYTHONPATH",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
    }:
        raise ValidationError("Cannot override isolation-related environment variables")
    number(
        config.get("max_output_bytes", 16_000_000),
        "max_output_bytes",
        minimum=1024,
        maximum=100_000_000,
    )
    if "instructions" in config and not isinstance(config["instructions"], str):
        raise ValidationError("instructions must be text")
    if "reasoning_effort" in config and config["reasoning_effort"] not in {
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    }:
        raise ValidationError("Unsupported reasoning effort")
    canonical_hash(config)
    return config
