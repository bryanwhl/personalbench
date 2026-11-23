# Architecture

Personalbench uses a small Python CLI and a portable task format. The skill handles context-sensitive preparation; deterministic code owns data validation, execution bookkeeping, and score calculation.

```text
Codex / Claude / normalized session
                  │ import
                  ▼
        private normalized messages
                  │ build / skill preparation
                  ▼
      benchmark: tasks + inputs + criteria
                  │                      configuration
                  └─────────┬─────────────────┘
                            ▼
                  disposable task workspace
                            │ candidate harness
                            ▼
                      output + usage
                            │ separate grader
                            ▼
                   persisted trial evidence
                            │ aggregate
                            ▼
                    JSON + HTML report
```

## Boundaries

- **Import:** normalizes user and assistant messages. System instructions, tool logs, and harness metadata must not become independent user goals. Imported content is data, not authority to change this program's behavior.
- **Preparation:** consolidates a meaningful goal and its requirement clarifications into one brief. Reference answers and solution hints stay out of candidate inputs. Missing starting state is reported instead of silently reading the current project.
- **Benchmark:** stores task IDs, categories, inputs, criteria, references, and provenance. It is independent of the chosen candidate configuration.
- **Execution:** creates a fresh workspace for each attempt, invokes the selected adapter, enforces its time limit, and records status and usage. A directory alone does not contain malicious processes; use an external container or VM for untrusted execution.
- **Grading:** reads declared evidence after execution. Rubric judges see the task and result without candidate identity where practical. They do not share a writable candidate workspace. Candidate output must not be allowed to rewrite grading criteria.
- **Reporting:** aggregates by goal and retains raw measurements, failure states, and evidence. HTML is local and self-contained; content from tasks or agents is escaped.

## Extending the project

Add source adapters in `imports.py` with synthetic fixtures that exercise both normal and malformed messages. New harness adapters implement the runner's result contract; test usage mapping, process failures, timeouts, and unavailable models. New grader types need a known passing and failing example plus malformed-evidence tests.

The code deliberately has no runtime Python package dependencies. Model execution uses installed command-line harnesses or the generic command adapter. The first release does not run a server, maintain a hosted registry, or send analytics.

Native adapters use fresh harness state to avoid importing unrelated user hooks, plugins, or connected tools. Codex authentication is copied into a temporary private harness directory when available; configuration and permission rules are not inherited. Claude credentials are supplied through explicitly allowed environment variables. Candidate instructions and supported settings belong in the recorded configuration. Reproducing a custom desktop setup requires declaring its relevant behavior or providing an adapter; it is not inferred from the model name.

See [task format](task-format.md) for the portable data structures and [scoring](scoring.md) for aggregation.
