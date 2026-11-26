# Task and configuration format

All files are UTF-8 JSON. Use `schema_version: 1` for benchmarks and normalized sessions. Keep personal task files outside source control, normally under `.personalbench/` or another private directory.

## Benchmark

```json
{
  "schema_version": 1,
  "name": "My recurring work",
  "scoring": {
    "success_threshold": 80,
    "efficiency_weights": {"time": 0.3333333333333333, "prompts": 0.3333333333333333, "cost": 0.3333333333333333}
  },
  "tasks": [{
    "id": "deduplicate",
    "category": "coding",
    "brief": "Return only a JSON array containing the unique integers in [3, 1, 3, 2, 1], preserving their first occurrence.",
    "inputs": {},
    "criteria": [{"id": "result", "description": "Preserve first-occurrence order", "kind": "json", "value": [3, 1, 2], "target": "output", "weight": 1, "critical": true}],
    "reference": {"time_seconds": 30, "user_prompts": 1, "cost_usd": 0.01},
    "provenance": {"source": "synthetic"}
  }]
}
```

Inputs map safe relative paths to text content. Absolute paths and parent traversal are rejected. Keep test answers, rubric internals, and historical agent outputs out of inputs.

`readiness` can be `ready`, `incomplete`, or `unsupported`. Imported tasks without established starting state remain incomplete. A compiler may set `requires_starting_state: false` only when the goal is fully answerable from the brief itself. This is a preparation decision, not a way to bypass missing repository files or source documents.

Reference measurements may be `null` when unavailable. They must be deliberately established and frozen before interpreting a full efficiency score. Replacing missing references creates a new benchmark identity.

An optional `historical` object contains `output`, `duration_seconds`, `user_prompts`, `cost_usd`, and `usage`. Missing measurements are `null`, not zero. Historical output supports recorded-work grading; it must never be supplied to the candidate as a solution.

When a compiler splits one session into several goals, session-wide measurements cannot safely be assigned to each goal. The importer leaves those per-goal historical values unavailable rather than repeating the whole session's usage or final answer.

## Criteria

| Kind | Check |
| --- | --- |
| `contains` | Required text is present in the output |
| `regex` | A pattern matches the output |
| `json` | Output parses as JSON and equals the specified value |
| `file_contains` | A workspace file at `target` contains the specified text |
| `command` | A declared test command exits successfully against the candidate workspace |
| `judge` | A model assesses the stated criterion and returns a score and evidence |

All criteria have a unique `id`, description, positive weight, and optional `critical` flag. Deterministic string checks establish the declared property, not general semantic correctness. For research or planning, write a rubric describing evidence support, requirement coverage, and material constraints rather than matching a preferred writing style.

A rubric judge returns exactly one finite score from 0 to 1 and nonempty evidence per criterion. Missing, extra, or invalid scores are grading errors, not silently ignored fields.

Command criteria use an argument array in `value`, an optional `tests` map of relative paths to text, and a `timeout_seconds` limit of at most 60 seconds. Test files are materialized in a separate grader directory after execution; the command receives the candidate workspace through `PERSONALBENCH_WORKSPACE`. A nonzero test exit is a failed criterion. A missing executable or timeout is a grading error. Command grading requires `--allow-unsafe-local` and does not isolate hostile code. See [`coding-benchmark.json`](../examples/coding-benchmark.json).

## Candidate configuration

```json
{
  "name": "my-codex-setup",
  "harness": "codex",
  "model": "REPLACE_WITH_ACCESSIBLE_MODEL_ID",
  "timeout_seconds": 120,
  "sandbox": "read-only",
  "tools": "none"
}
```

Supported adapter names are `codex`, `claude`, `command`, and `fixture`. The model is explicit; compatibility and authentication belong to the selected harness. Do not put API keys or other credentials in configuration JSON.

Optional `prices` contains `input_per_million`, `cached_input_per_million`, and `output_per_million` in USD. When supplying prices, also provide `price_date` as a nonempty date string. Use the actual applicable schedule and retain its source in your private configuration notes. Omitted prices leave estimated cost unavailable.

`tools` is `none` by default or `workspace` for tasks requiring tool use. `sandbox` configures supported harness controls; it is not a portable operating-system sandbox. Use `instructions` for explicit candidate instructions and `reasoning_effort` for a supported Codex effort setting. `env_allowlist` lists environment-variable names to pass through, never their values. Unknown configuration fields are rejected to catch misspellings.

The fixture adapter accepts `responses`, a map of task IDs to deterministic output strings, and exists for examples and tests. Fixture runs cannot establish real model performance.

## Generic command adapter

Configure `command` as an argument array, never a shell string. The adapter receives a JSON request on standard input and returns a JSON object on standard output:

```json
{"task": {"id": "goal-1", "brief": "Complete the goal", "inputs": {}}, "model": "selected-model", "workspace": "/temporary/workspace"}
```

Response:

```json
{
  "output": "candidate's final answer",
  "usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 20},
  "cost_usd": null
}
```

Log diagnostics to standard error. Return a nonzero process status for execution failures. The runner records them as infrastructure errors rather than scoring an empty answer as an ordinary task failure. Document the adapter's model selection, token semantics, permissions, and external requests.

Local command execution requires the CLI's explicit unsafe-local option. Use a container/VM wrapper for untrusted tasks. Do not expose grader files, previous attempts, credentials, or unrelated home-directory files to a candidate.

## Compiled historical goals

The optional compiler receives numbered messages and may return `source_message_indices` for each goal. These sorted, unique, zero-based indices identify the user requests and recorded assistant responses used for that goal. Personalbench validates the mapping and retains its inferred provenance. Aggregate session time, cost, and tokens are not divided across goals without evidence. A transcript's final text may not include the actual files it discusses; recorded grading cannot verify absent artifacts.

## History corpus

`build` without a session and `sessions` use a trailing 60-day window across local Codex sessions (including archives) and Claude projects. Selection uses the latest recorded timezone-aware timestamp, never file modification time. Complete selected transcripts retain earlier context. Source-message mappings exclude independent goals whose user requests and clarifications all predate the window; goals with undated user activity remain incomplete. `--days`, `--as-of`, `--source`, repeatable `--session-root`, and repeatable `--exclude-session` override discovery. A session whose latest activity is after an explicit `--as-of` is excluded as a whole; this conservative selection does not reconstruct an earlier transcript snapshot.

Corpus benchmarks include a `history` inventory with source paths, the frozen window, exclusions, preparation errors, normalized-content digests, duplicates, task mappings, and readiness. The CLI checkpoints the inventory in a sibling `.inventory.json` and normalized/compiled sessions in a sibling `.sessions` directory. Exact equivalent transcripts are deduplicated independently of timestamps; exact compiled task duplicates retain their source mappings. Semantic consolidation of fork goals still requires preparation review. No compiler means incomplete fallback tasks, not an automatically runnable benchmark.
