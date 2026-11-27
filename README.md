# Personalbench

**Your work. Your benchmark. Your choice of coding setup.**

Personalbench turns coding-agent sessions into a reusable personal benchmark. Score your current setup, then rerun the same tasks with a different model, harness, or settings. It supports coding, research, planning, and other text-based agent work.

```text
Local sessions → complete task briefs + preserved inputs → versioned benchmark
                                                            ↓
                                          chosen harness + model → local report
                                                            ↓
                                             another setup → comparison
```

This is an experimental local-first release. The CLI performs imports, capture, execution, grading, and reporting; the included skill helps prepare meaningful tasks. Your sessions and results are not uploaded to a Personalbench service. A configured model provider still receives the content needed for preparation, execution, or judging.

## Try it without a model account

Requires macOS or Linux and Python 3.11 or later. Install from the repository; a package-index release is not required.

```bash
git clone https://github.com/bryanwhl/personalbench.git
cd personalbench
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
personalbench doctor
personalbench run examples/benchmark.json \
  --config examples/fixture.json --output .personalbench/demo
personalbench report .personalbench/demo/report.json \
  --output .personalbench/demo/report.html
```

Open `.personalbench/demo/report.html` in your browser. The fixture adapter returns predetermined synthetic answers: this demonstrates the workflow and **does not measure any real model**.

## Evaluate your recent work (default)

An unqualified Personalbench run uses the past **120 days** of available local Codex and Claude sessions containing human requests. Explicit automation-origin runs are excluded by default; use `--include-automations` to include them. Distill the whole window into independent goals, consolidate equivalent fork histories and repeated tasks, and keep coverage across projects and task types. Independent goals with no user request or clarification inside the window are excluded; earlier messages remain context for recent goals. Missing historical files or connected sources remain explicit gaps; a transcript alone does not make a task replayable.

```bash
personalbench evaluate --config /path/to/candidate.json \
  --output .personalbench/history --allow-unsafe-local
```

This single command discovers Codex and Claude sessions, freezes the 120-day window, recovers read-only historical inputs, compiles independent goals, runs fresh attempts, and writes `.personalbench/history/run/report.html`. The compiler and judge default to the candidate's explicitly selected model with tools disabled; use `--compiler`, `--curator`, and `--judge` for separate configurations. The curator defaults to the compiler model and runs with tools disabled. Codex workspace attempts use a named filesystem profile that grants minimal runtime reads and the supplied workspace, with network access disabled. Native execution still requires trusted tasks.

Preparation and execution each default to three workers and a 30-minute batch budget. Change them with `--preparation-workers`, `--workers`, `--max-preparation-seconds`, and `--max-total-seconds`. Stop and continue the same evaluation with the same command plus `--resume`; completed compilation and candidate attempts are retained. An interrupted output assessment can be retried without rerunning the candidate. Changing model configuration or scope requires a new output directory. Plain resume retains the original window and automation scope even when current defaults differ. Native adapter and input-boundary identities are recorded; preparation refresh starts fresh when the previous adapter differs, and comparisons reject different input boundaries. After fixing preparation, add `--resume --refresh-preparation` to revalidate the frozen inputs and cached goal compilation; changed benchmarks and runs receive a new revision while prior reports stay intact. Completed outcomes are reused only when the full task inputs, requirements, candidate setup, and grading context are unchanged; failures are retained as well as successes. Newly ready or changed goals receive new attempts.

`--days`, `--as-of`, `--source`, repeatable `--session-root`, and repeatable `--exclude-session` override the default. `--exclude-session-id` excludes a chat and its descendants. The current Codex chat and internal agent threads are excluded automatically. Compressed `.jsonl.zst` logs use the optional `zstd` executable with decoded size and time limits. Later activity in a resumed chat does not exclude earlier eligible work: compiler messages and tool evidence are frozen at the cutoff.

Every user request is mapped to a goal or an explicit preparation exclusion. Generated tasks include a mandatory validity check, so a high average rubric score cannot hide a missing core deliverable or fabricated evidence. Read-only source outputs are recovered by source ID, goal time bounds, timezone, and required source kinds; later mailbox observations cannot stand in for earlier ones. Tool metadata, the goal's own generated answers, and mutations are excluded. Read-only runtime or source observations from earlier independent work can support a later goal when request and mutation timing proves they are starting evidence. Exact fork copies are consolidated first. A separate global semantic curation stage groups equivalent underlying work across sessions, dates, people, filenames, and data snapshots. A narrower repeated request is consolidated when its entire outcome is covered by a fuller version of the same technical task. Each family names the original cases covering all its requirements; one of those unchanged cases is chosen before execution for evidence completeness. Routine integration tasks do not become different families because they process one item versus several or receive the same fields from different sources; calendar-event creation from messages or booking details is one family. Distinct technical subjects, algorithms, APIs, bug triggers, and requested behavioral changes stay separate. Repeated daily calendar/email briefs form one family even when automation inclusion is enabled; building or debugging a briefing tool is different work. Local grouping is followed by a mandatory audit across every original goal and all batch boundaries. Full occurrences, family memberships, merge reasons, and selection proof remain private audit artifacts. Curation failure stops evaluation rather than running an uncurated substitute. Genuine missing inputs and unavailable live capabilities remain visible coverage gaps, rather than invented scores.

The candidate receives only the prepared brief and preserved inputs. The compiler receives normalized messages and a source catalog; the judge receives criteria, preserved inputs, and the candidate's answer. Personal artifacts remain local; configured model providers receive that selected content. Raw logs, frozen sources, compilation records, criteria, native tool traces, execution evidence, and the report stay in the private output directory. Candidate coverage contains sufficiency information rather than original-log locators; private provenance retains the full audit. No collector or automatic publication is installed.

For preparation without execution, use `personalbench build --compiler /path/to/compiler.json --output .personalbench/benchmark.json --allow-unsafe-local`. `build` without a session uses the same 120-day corpus; without a compiler it produces an explicitly labeled draft. `sessions` lists the source inventory without printing conversation contents. Review unknown evidence and category coverage when interpreting a score; a graded subset is not a whole-history score.

## Replayable suites and simulated services

The history catalogue and the runnable benchmark are separate artifacts. `evaluate` preserves the full curated catalogue in an immutable `catalogue-<hash>.json`, then executes a suite containing only ready cases. `workflow.json` records both paths and both coverage counts. A suite score is not a claim that every catalogue task was evaluated. Plain legacy resume preserves its prior scope; an explicit preparation refresh introduces the current suite policy.

A live-account requirement is often an environment gap, not a reason to abandon a task. The first stateful backend provides an isolated mailbox and calendar. Each attempt starts from a frozen fixture and can search/read mail and list/create/update/delete calendar events through MCP tools. It changes real simulator state, rather than returning canned success strings. No live account, credentials, or network access is needed. Captured initial/final state and operations are saved with the attempt.

```bash
personalbench run examples/calendar-benchmark.json \
  --config examples/calendar-codex.json --output .personalbench/calendar \
  --allow-unsafe-local
```

The example is explicitly synthetic and uses a real configured model. Choose an accessible model ID in the configuration. `tools: "environment"` exposes only the simulated service; `tools: "workspace"` can also use the preserved filesystem. The first backend currently supports the Codex harness.

Critical `environment_state` criteria check actual calendar outcomes: correct event fields and times, no unexpected additions, and preservation of unrelated events. Expected answers stay outside the candidate and tool server. Changing an initial event is authorized only when the criterion explicitly targets its ID; a new-event goal cannot pass by overwriting another meeting. A fluent claim of success cannot replace a missing mutation. The isolated rubric judge also receives captured environment evidence.

The current calendar-action adaptation requires a critical state-outcome criterion and no unresolved live-account, publication, UI, or training capability. Adding an empty environment or a contains("done") check cannot make an unsupported task runnable.

Simulation variants are explicit new case packages with `provenance.adapted_from`, the exact original task hash, and an adaptation explanation. Fixtures declare `fixture_origin: "historical"` or `"synthetic"`. A historically seeded simulation may cover only the captured scenario, not an entire former account. Preserve missing or ambiguous facts rather than inventing confirmation. A synthetic fixture is a declared adaptation, not recovered historical evidence.

```bash
personalbench suite /path/to/catalogue.json \
  --variants /path/to/simulation-variants.json --output .personalbench/suite.json
personalbench evaluate --config /path/to/candidate.json \
  --variants /path/to/simulation-variants.json \
  --output .personalbench/history --allow-unsafe-local
```

A suite retains one case per source family and records included originals, substitutions, and excluded backlog tasks. Supplied variants are frozen and verified on resume. To withdraw them, use `--resume --refresh-preparation --clear-variants`; plain resume never silently drops fixtures. Missing source files still require recovery or a disclosed reconstruction. A stubbed training job cannot establish actual model quality; a publication simulator measures workflow behavior, not a real deployment.

## Make a benchmark from a selected session

An explicit session or narrower range overrides the default. Codex sessions are commonly stored under `~/.codex/sessions/`.

```bash
personalbench sessions
personalbench import /path/to/session.jsonl \
  --output .personalbench/session.json
personalbench build .personalbench/session.json \
  --output .personalbench/benchmark.json
```

Without a compiler, `build` conservatively combines user requests into one brief and creates an inferred rubric. A compiler configuration can split a session into meaningful goals and consolidate requirement clarifications:

```bash
personalbench build .personalbench/session.json \
  --compiler /path/to/compiler.json --output .personalbench/benchmark.json \
  --allow-unsafe-local
```

The skill can prepare or refine the same task format. Missing pre-change files, sources, or required tools remain missing; the current working tree is not a substitute for historical inputs. Check preparation warnings before treating a task as repeatable.

`personalbench validate /path/to/benchmark.json` checks the task format without calling a model. Valid JSON does not establish that historical inputs are complete or that the task is a faithful evaluation of the original goal.

Choose a configuration file with an accessible model ID. The sample [`examples/codex.json`](examples/codex.json) contains a placeholder that must be replaced. It never silently substitutes another model.

```bash
personalbench run .personalbench/benchmark.json \
  --config /path/to/candidate.json --judge /path/to/judge.json \
  --output .personalbench/baseline --allow-unsafe-local
```

The judge is only needed for rubric criteria. To score recorded work instead, use `grade-recorded`; its report is labeled separately because historical work may include human assistance and incomplete telemetry.

```bash
personalbench grade-recorded .personalbench/benchmark.json \
  --judge /path/to/judge.json --output .personalbench/recorded --allow-unsafe-local
```

## Compare a new setup

Change the candidate configuration and keep the benchmark and judge fixed:

```bash
personalbench run .personalbench/benchmark.json \
  --config /path/to/next-candidate.json --judge /path/to/judge.json \
  --repeats 3 --output .personalbench/next --allow-unsafe-local
personalbench compare .personalbench/baseline/report.json \
  .personalbench/next/report.json --output .personalbench/comparison.json
```

Each run records its configuration and benchmark/scoring identity. Incompatible benchmark versions, coverage, or baseline types receive a warning instead of a misleading score delta. When the same frozen historical gaps exist in both runs, quality/success can compare their identical fully graded prepared subset, explicitly labeled N/M. Missing evidence on a ready goal is not silently excluded. Use `--resume` with the same run configuration to retain completed attempts after interruption.

Runs have per-attempt timeouts. Use `--max-total-seconds` to bound a batch across resumes (increase it explicitly to extend a stopped run), and `--stop-after-cost-usd` to stop scheduling after recorded spend reaches a threshold. The dollar threshold is **not a hard spending cap**: an in-flight request can exceed it, and unknown billing cannot be measured. Provider-side limits remain necessary for a guaranteed dollar ceiling.

| Score | Meaning |
| --- | --- |
| Task success % | Fraction of goals meeting their declared requirements and threshold |
| Output quality % | Attainment of weighted criteria; not a probability of correctness |
| Efficiency % | Normalized time, user-prompt count, and token dollars against fixed task references |

Raw metrics, evidence, coverage, and failures remain visible. Fresh runs receive one complete brief, so their user-prompt count is normally one. Missing cost is unknown, never free. See [scoring](docs/scoring.md).

## Capture new work

```bash
personalbench capture start --source /path/to/task-inputs \
  --output .personalbench/capture
# Do the work in your coding harness, then identify its session file.
personalbench capture finish .personalbench/capture /path/to/session.jsonl \
  --output .personalbench/captured-session.json
personalbench build .personalbench/captured-session.json \
  --output .personalbench/captured-benchmark.json
```

Only explicitly selected inputs are captured. The bounded `watch` command can refresh a normalized session while a harness records new messages; it is not a background daemon.

## Use the skill

Copy `skills/personalbench` into your harness's skill directory, such as `~/.codex/skills/personalbench`. Keep the CLI installed in the environment your harness uses. Then ask:

> Use $personalbench to turn this session into a benchmark and evaluate my current setup.

The skill guides goal selection, state preservation, automated grading, and comparisons. Skill instructions do not grant new access to credentials or publishing destinations.

## Boundaries worth knowing

- Codex, Claude Code, and generic command integrations depend on installed CLIs and compatible accounts/models. A model name accepted by one harness is not automatically supported by another.
- Native adapters start from minimal configuration rather than inheriting your hooks, plugins, or unrelated tools. Declare instructions and supported settings explicitly; use a custom adapter for a richer setup. The report describes the evaluated configuration, not an automatic copy of every desktop preference.
- Workspaces are disposable. The Codex adapter confines reads with a minimal runtime/workspace permission profile, including symlink-escape checks. Other native adapter controls and an isolated directory are **not a security boundary for hostile code**. Native harnesses, custom commands, and command graders require `--allow-unsafe-local`. Use this only for trusted local work; run untrusted tasks inside a separate container or VM.
- Research and planning quality can use an automated rubric judge. Judge output is evidence with limitations, not an independent proof. Original answers are not treated as unquestionable answer keys.
- Historical conversion cannot reconstruct absent files, sources, timestamps, or billing. Unknown evidence is reported; tasks may need preparation before fresh execution.
- Graders support output/file assertions, separately stored executable tests, and model rubrics. The [examples](examples/README.md) include a coding repair with behavioral tests and research/planning rubrics.
- No telemetry, automatic sharing, hosted task registry, or automatic model downloads. Do not commit your private sessions, benchmarks, captures, or reports.

## Contribute

Useful first contributions include an importer fixture, a harness adapter, a grader example, or a clearer quickstart. Read [CONTRIBUTING](CONTRIBUTING.md), [architecture](docs/architecture.md), and [security](SECURITY.md).

Project code is licensed under [Apache-2.0](LICENSE). Your imported sessions and task content retain their own ownership and sharing conditions.
