# Contributing

Start with a small reproducible improvement: an importer edge case, a harness adapter, a grading example, or onboarding feedback. For a new integration or scoring policy, open an issue describing the workflow and proposed behavior before making a large change.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest
ruff check .
```

Use Python 3.11-compatible code. Runtime dependencies are deliberately minimal. Tests must run without paid provider access, personal accounts, or local session files. Use the fixture adapter or mocked subprocess output for ordinary tests; keep real-provider validation opt-in and local.

## Pull requests

Explain the problem, resulting behavior, and validation. Include a failing/passing behavioral test for meaningful logic changes. Conventional commit prefixes such as `fix:`, `feat:`, and `docs:` make the history easier to scan.

Preserve these invariants:

- Benchmark definitions remain independent of candidate configurations.
- Missing evidence stays unknown. A partial run cannot improve its score by dropping difficult tasks.
- User-prompt counts exclude model messages and tool calls.
- Candidate inputs exclude reference answers and hidden checks.
- Model or harness failures do not silently switch configurations.
- Reports distinguish deterministic checks, rubric judgment, historical grading, and fresh runs.
- No raw personal sessions, credentials, provider outputs, or private repository contents enter fixtures or screenshots.

Before submitting, inspect your staged diff and run the scanner:

```bash
git diff --cached
personalbench scan . --git
```

Automated secret detection is a useful check, not proof that content is safe to publish. Replace real details with deliberately synthetic examples. See [SECURITY](SECURITY.md) for vulnerability reports.

Contributions are accepted under the project's Apache-2.0 license. Only contribute content you have the right to share. Please follow the [Code of Conduct](CODE_OF_CONDUCT.md).
