# Security and privacy

## Reporting a vulnerability

Use the repository's **Security → Report a vulnerability** action for a private report. Include affected versions, a minimal synthetic reproduction, the impact, and any proposed fix. Do not publish credentials, personal session content, or exploit details in a public issue.

If private reporting is unavailable, open an issue asking for a private reporting channel without disclosing the vulnerability. This is a small volunteer project; response times are not guaranteed. Security fixes target the latest release.

## What stays local

Personalbench has no hosted service or telemetry. Imports, task packages, captures, outputs, and reports are written locally. Default repository exclusions cover common generated-data locations, but files written elsewhere may still be tracked accidentally.

Model-backed preparation, execution, and judging send the required content to the configured provider through the selected harness. Provider retention and account policies still apply. Choose inputs deliberately; local storage is not a promise of offline inference.

## Execution boundary

Task inputs, historical messages, candidate outputs, and contributed benchmarks are untrusted data. Automatic grading does not make arbitrary code safe to execute.

The runner uses disposable workspaces and harness permission settings. These are not a container or a complete operating-system security boundary. Native harnesses and arbitrary command adapters require explicit local-execution authorization. Run untrusted tasks and command graders in a separate container or VM without unrelated credentials, home-directory mounts, or production access.

Reference solutions and grader files stay outside candidate inputs. Command tests are materialized separately after candidate execution. A local process running as the same user may still have broader access; do not mistake this separation for hostile-code isolation.

## Publishing safely

- Keep personal benchmarks and reports in private storage, separate from public code.
- Inspect the staged diff, including filenames, documentation, generated artifacts, and Git history.
- Run `personalbench scan . --git` before publishing. Detection covers recognizable patterns and cannot identify every secret or private fact.
- Use synthetic fixtures. Redaction is not a license to publish someone else's data.
- If a credential was exposed, revoke it. Deleting the latest file does not remove it from Git history, forks, or caches.

Do not put secrets in configuration files or command arguments. Use the selected harness's authentication mechanism or explicitly allowed environment-variable names instead.
