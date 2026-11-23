# Synthetic examples

Every task and answer in this directory is deliberately synthetic. No example contains a user's real session, project, or provider response.

| File | Purpose |
| --- | --- |
| `benchmark.json` | Small deterministic coding/research/planning workflow demo |
| `fixture.json` | Predetermined answers for that demo; not a real model evaluation |
| `session.json` | A normalized session with a goal and later requirement clarification |
| `coding-benchmark.json` | Repair a Python function, scored with hidden behavioral tests |
| `rubric-benchmark.json` | Research and planning tasks scored with automated rubric judgments |
| `codex.json` | Codex configuration for text-only tasks |
| `codex-workspace.json` | Codex configuration for tool-based coding tasks |
| `claude.json` | Claude Code configuration with explicit API environment-variable access |

Native configuration examples contain `REPLACE_WITH_ACCESSIBLE_MODEL_ID`. Copy them to private storage and choose a model available through your installed harness. No prices are guessed, so dollar-based efficiency is incomplete until a rate schedule or charge is available.

To run the trusted synthetic coding task after configuring a compatible model:

```bash
personalbench run examples/coding-benchmark.json \
  --config /path/to/codex-workspace.json \
  --output .personalbench/coding --allow-unsafe-local
```

To run research and planning rubric tasks, configure a judge as well:

```bash
personalbench run examples/rubric-benchmark.json \
  --config /path/to/candidate.json --judge /path/to/judge.json \
  --output .personalbench/rubric --allow-unsafe-local
```

The local-execution option acknowledges that native harnesses and test commands are not a complete hostile-code sandbox. The coding example executes candidate Python code during grading. Use externally isolated execution for untrusted candidates or tasks.
