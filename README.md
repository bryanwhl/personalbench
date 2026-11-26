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

An unqualified Personalbench run uses the past **60 days** of available local Codex and Claude sessions. Distill the whole window into independent goals, consolidate equivalent fork histories and repeated tasks, and keep coverage across projects and task types. Independent goals with no user request or clarification inside the window are excluded; earlier messages remain context for recent goals. Missing historical files or connected sources remain explicit gaps; a transcript alone does not make a task replayable.

```bash
personalbench evaluate --config /path/to/candidate.json \
  --output .personalbench/history --allow-unsafe-local
```

This single command discovers Codex and Claude sessions, freezes the 60-day window, recovers read-only historical inputs, compiles independent goals, runs fresh attempts, and writes `.personalbench/history/run/report.html`. The compiler and judge default to the candidate's explicitly selected model with tools disabled; use `--compiler` and `--judge` for separate configurations. Native workspace controls still require trusted tasks.

Preparation and execution each default to three workers and a 30-minute batch budget. Change them with `--preparation-workers`, `--workers`, `--max-preparation-seconds`, and `--max-total-seconds`. Stop and continue the same evaluation with the same command plus `--resume`; completed compilation and candidate attempts are retained. An interrupted output assessment can be retried without rerunning the candidate. Changing model configuration or scope requires a new output directory. After fixing preparation, add `--resume --refresh-preparation` to revalidate the frozen inputs and cached goal compilation; changed benchmarks and runs receive a new revision while prior reports stay intact. Completed outcomes are reused only when the full task inputs, requirements, candidate setup, and grading context are unchanged; failures are retained as well as successes. Newly ready or changed goals receive new attempts.

`--days`, `--as-of`, `--source`, repeatable `--session-root`, and repeatable `--exclude-session` override the default. `--exclude-session-id` excludes a chat and its descendants. The current Codex chat and internal agent threads are excluded automatically. Compressed `.jsonl.zst` logs use the optional `zstd` executable with decoded size and time limits. Later activity in a resumed chat does not exclude earlier eligible work: compiler messages and tool evidence are frozen at the cutoff.

Every user request is mapped to a goal or an explicit preparation exclusion. Generated tasks include a mandatory validity check, so a high average rubric score cannot hide a missing core deliverable or fabricated evidence. Read-only source outputs are recovered by source ID, goal time bounds, timezone, and required source kinds; later mailbox observations cannot stand in for earlier ones. Tool documentation, generated answers, and mutations are excluded. Exact fork prefixes and equivalent goals are consolidated without merging different dated account snapshots. Genuine missing inputs and unavailable live capabilities remain visible coverage gaps, rather than invented scores.

The candidate receives only the prepared brief and preserved inputs. The compiler receives normalized messages and a source catalog; the judge receives criteria, preserved inputs, and the candidate's answer. Personal artifacts remain local; configured model providers receive that selected content. Raw logs, frozen sources, compilation records, criteria, execution evidence, and the report stay in the private output directory. No collector or automatic publication is installed.

For preparation without execution, use `personalbench build --compiler /path/to/compiler.json --output .personalbench/benchmark.json --allow-unsafe-local`. `build` without a session uses the same 60-day corpus; without a compiler it produces an explicitly labeled draft. `sessions` lists the source inventory without printing conversation contents. Review unknown evidence and category coverage when interpreting a score; a graded subset is not a whole-history score.

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
- Workspaces are disposable. Native harness controls and an isolated directory are **not a security boundary for hostile code**. Native harnesses, custom commands, and command graders require `--allow-unsafe-local`. Use this only for trusted local work; run untrusted tasks inside a separate container or VM.
- Research and planning quality can use an automated rubric judge. Judge output is evidence with limitations, not an independent proof. Original answers are not treated as unquestionable answer keys.
- Historical conversion cannot reconstruct absent files, sources, timestamps, or billing. Unknown evidence is reported; tasks may need preparation before fresh execution.
- Graders support output/file assertions, separately stored executable tests, and model rubrics. The [examples](examples/README.md) include a coding repair with behavioral tests and research/planning rubrics.
- No telemetry, automatic sharing, hosted task registry, or automatic model downloads. Do not commit your private sessions, benchmarks, captures, or reports.

## Contribute

Useful first contributions include an importer fixture, a harness adapter, a grader example, or a clearer quickstart. Read [CONTRIBUTING](CONTRIBUTING.md), [architecture](docs/architecture.md), and [security](SECURITY.md).

Project code is licensed under [Apache-2.0](LICENSE). Your imported sessions and task content retain their own ownership and sharing conditions.
