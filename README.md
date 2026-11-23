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

## Make your own benchmark

Start with a small session whose goal and starting inputs are available. Codex sessions are commonly stored under `~/.codex/sessions/`; select a particular JSONL file rather than copying the whole directory into this repository.

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

Each run records its configuration and benchmark/scoring identity. Incompatible benchmark versions, coverage, or baseline types receive a warning instead of a misleading score delta. Use `--resume` with the same run configuration to retain completed attempts after interruption.

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
