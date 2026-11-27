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

`evaluate` runs discovery, preparation, recovery, and execution in a checkpointed private directory. It defaults to a trailing 120-day window across local Codex and Claude sources; `build` without a session and `sessions` use the same discovery. Both `.jsonl` and optional-zstd `.jsonl.zst` exports are supported. Selection uses dated user requests, never file modification time. Raw records, compiler context, and tool evidence are frozen through `--as-of`; earlier context remains available while independent goals outside the window are excluded. Current/internal agent chats and metadata-only exports are explicitly accounted for as exclusions.

`--days`, `--as-of`, `--source`, repeatable `--session-root`, `--exclude-session`, and `--exclude-session-id` override discovery. Corpus benchmarks retain source digests, immutable snapshot paths, user-request mappings, non-work exclusions, duplicate/covered-source relationships, cache identity, preparation errors, and goal readiness. Every user request must be mapped or explicitly excluded. Exact inherited prefixes include tool evidence. Raw snapshots retain their dates and contents; later semantic curation can group the same underlying task across dates and select one original snapshot without combining incompatible states. Source-message indices refer to the scoped normalized session recorded in the inventory, not raw log line numbers.

The compiler receives normalized messages and a bounded source catalog. Compiled goals select `source_evidence_ids`, `required_evidence_kinds`, `execution_requirements`, and sorted `source_message_indices`. Recovery validates source existence, original goal/date bounds, timezone, and required kinds before promoting readiness. A full single-file read can recover starting bytes; a directory listing or partial excerpt is not a repository snapshot. `missing_inputs` keeps explicitly absent requirements unresolved. A goal's own reference answer or generated solution never becomes its starting input. Writes, credential reads, memory writes, and tool metadata are excluded. A read-only runtime/source inspection from earlier independent work can support a later goal only with request/mutation timing proof. Candidate coverage omits private archive paths and audit pointers, while provenance retains them.

`--resume` preserves frozen sources, completed compilation, and completed attempts. `--resume --refresh-preparation` revalidates inputs after fixes without replacing source history. Changed preparation produces a new benchmark/run revision. Preparation caches preserve the original compiler goal structure and are revalidated with current recovery rules. Interrupted grading can reuse saved candidate outputs and declared artifacts; arbitrary missing command-grader workspace state cannot be reconstructed. A shortened batch-budget attempt may resume under an explicitly extended budget, with earlier resource use retained. A full task-timeout failure is not retried automatically.


## Semantic task families

New compiled builds and history evaluations run global curation before execution. `history.semantic_curation` records the original occurrence count, unique family count, duplicates consolidated, every membership, unchanged representative IDs, grouping rationale, and pre-execution selection proof. Families record `representative_candidate_ids`: only original cases whose requirements cover every outcome in that family. A repeated narrower request can be consolidated into a fuller version of the same task, but an easier narrow case cannot replace the fuller task. Full original cases remain in a private content-addressed occurrence file. Dates, payload changes, routine item counts, or sources providing the same event fields do not create unique tasks. Calendar-event creation from emails and supplied booking details shares one family. Distinct requested operations and technical requirements do. Explicit automation-origin requests are excluded from the default human-work corpus and can be included with `--include-automations`; repeated briefs still receive one representative. Failed curation is checkpointed as `curation_incomplete` and must be repaired/resumed before candidates can run.


## Stateful calendar/mail environments

`task.environment.kind` is currently `calendar-mail-v1`. The fixture has `fixture_origin` (`historical` or `synthetic`), an offset-bearing RFC3339 `now`, IANA `timezone`, `calendars` (`id` and Google-style `events`), and `messages` (`id`, `threadId`, `from`, `to`, `subject`, `date`, `body`; optional supported mail metadata). See `examples/calendar-benchmark.json` for a complete synthetic package. Unknown fields, unsupported recurrence expansion, invalid timestamps and zones fail validation. Captured recurring-instance metadata can be retained read-only.

The candidate uses `environment_info`, `mail_search`, `mail_get_message`, `calendar_list_events`, `calendar_create_event`, `calendar_update_event`, and `calendar_delete_event`. Mail search implements a documented subset, not every Gmail operator. Tool descriptions specify query and pagination semantics. Unsupported queries fail explicitly. Each attempt has independent state, deterministic IDs, an operation log, and a frozen clock. Runtime state is owned by the trusted tool server outside the editable candidate workspace; the agent cannot substitute a workspace JSON file for it. The stdio service uses the [MCP transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) and supports initialization-based protocol versions `2025-11-25` and `2025-06-18`.

An `environment_state` criterion has a `value` object with `calendar_id` (default `primary`), `events` (expected event subsets), optional `absent_event_ids`, and booleans `preserve_existing` and `no_extra_events` (both default true). At least one event or absence must be asserted. Offset-bearing `dateTime` values compare as instants, so equivalent UTC and local timestamps pass. Each expectation must match its own event; cancelled events do not satisfy active-event goals. An expected explicit `id` authorizes updating that initial event; otherwise unrelated initial events must remain unchanged in final state. These state assertions do not model notification delivery or forbid transient edits that are fully restored; use the captured operation log and additional rubric checks when action history is part of the requirement. Expected state assertions are never sent to the candidate or the tool server.

Attempts retain `environment: {kind, initial_state, final_state, operations}` even after a timeout. This is captured by the trusted runner, not read from a candidate-written artifact. Rubric judgments receive that evidence. Backend implementation hashes are included in native runtime identity; the state grader is included in scoring identity. Simulator changes therefore invalidate incompatible reuse and comparisons.

`personalbench suite` exports only ready originals and explicitly traced simulation variants. It retains source-catalogue coverage under `suite`, not as a misleading full-catalogue task count. The variant's `provenance.adapted_from_hash` must equal the original task's canonical hash. Neither missing inputs nor unsupported actions become ready merely because a variant label exists: the environment and all criteria must validate. Current calendar-action variants require a critical environment_state criterion, and remaining execution_requirements may contain only calendar-mail-v1 and workspace; read-only mailbox adaptations need an appropriate future outcome-grader extension. Every state criterion must refer to a calendar present in its fixture. The builder does not generate fixtures or original answers.
