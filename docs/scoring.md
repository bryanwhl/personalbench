# Scoring

Personalbench keeps task success, output quality, and efficiency separate. A fast failure must not look like a better overall result.

## Success and output quality

A criterion returns a score between 0 and 1. Output quality is the weighted mean of a task's criterion scores, displayed as a percentage. Each criterion carries its evidence and grading type. A judge can return an unknown score when crucial verification evidence was not captured; missing evidence is not a fabricated zero.

A task succeeds when its quality reaches the benchmark's `success_threshold` and every critical criterion passes. The default threshold is 80%. Critical criteria are appropriate for requirements whose violation invalidates the result, such as fabricating a required source or ignoring a prohibition.

Tasks are aggregated equally, so ten assertions in one task do not outweigh another goal with two assertions. Repeated attempts are summarized within each task before suite aggregation. The report retains the number of distinct tasks and attempts.

Missing grading evidence does not become a failure or a pass. An incomplete run has explicit coverage and unavailable headline metrics where a complete score cannot be supported. Observed metrics are labeled separately; a setup cannot improve its headline by leaving hard tasks unscored.

## Efficiency

Each task freezes three positive reference values with the benchmark:

```json
{"time_seconds": 300, "user_prompts": 1, "cost_usd": 0.10}
```

For an observed metric, the component score is:

```text
component % = 100 × min(1, reference / observed)
```

Zero measured resource use receives 100%; missing resource use remains unknown. Equal component weights are the default. Weights sum to one and are stored with the benchmark. References are scoring budgets, not claims about a typical developer or model.

For example, a successful task that uses twice its time reference, one prompt against a one-prompt reference, and half its dollar reference receives:

```text
time = 50%    prompts = 100%    cost = 100%
efficiency = (50 + 100 + 100) / 3 = 83.3%
```

Repeated successful attempts use median resource measurements. Failed scored attempts contribute zero through the success fraction; unavailable grading or execution measurements make the full efficiency score unavailable. The report keeps the underlying measurements so a composite never hides the reason for a change.

### What the measurements include

- **Task time:** candidate execution duration. Preparation, grading, and human idle time are separate. Reconstructed historical duration may be unavailable or only an estimate.
- **User prompts:** instruction messages from a user, including the initial request. Tool calls and model messages are not prompts. Single-brief fresh attempts normally use one; this cannot measure how much correction a real user would have needed.
- **Token dollars:** provided charges or an estimate from recorded token usage and explicit per-million prices. Cached input must not be billed twice. Without applicable usage and prices, cost stays unknown. Subscription access does not establish a per-run dollar charge.

## Comparability

Keep the benchmark, criteria, references, weights, and judge configuration fixed. Reports record benchmark and scoring identities. Changing any of those creates a different scoring context.

Eligibility is checked per metric. Complete quality scores can be compared even when token costs make efficiency unavailable.

Recorded work and fresh runs are separate baseline types. Historical work may include extra user help and later clarifications; fresh runs receive the consolidated brief at the start. Reports do not represent the two as a controlled comparison.

One attempt is a preview. Several attempts can reveal variability but do not establish statistical confidence about all future work. Judge agreement is likewise not proof of correctness. Preserve evidence and inspect task-level differences before choosing a setup.
