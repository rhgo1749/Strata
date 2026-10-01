# Architecture decision provenance

This fork uses **decision-bearing GitHub Issues as ADR-style decision records**.

The goal is to keep architectural reasoning close to the work that proposes and validates it without creating a second, competing lifecycle for standalone ADR files. Canonical documentation, source, and tests remain the source of truth for the current implemented contract; Issues preserve decision provenance and discussion.

## When an Issue is decision-bearing

Use the decision-bearing format when an Issue proposes or changes one or more of the following:

- architecture or major runtime structure;
- API, protocol, schema, or compatibility behavior;
- ownership or source-of-truth boundaries;
- migration or fallback policy;
- security, safety, or failure-isolation boundaries;
- cross-component contracts;
- durable contributor or operations workflow.

Routine bugs, small implementation tasks, benchmark runs, and investigations do not need ADR-style ceremony unless they also make one of those durable decisions.

## Required sections

A decision-bearing Issue should contain these sections.

### Decision status

Use ordinary prose such as:

- **Proposed** — under consideration; not yet authoritative.
- **Accepted** — explicitly accepted by the maintainer or already established by the canonical contract.
- **Superseded** — replaced by another Issue or canonical contract; link the replacement.

Do not infer acceptance merely because an Issue was opened or implementation work started.

### Context / problem

Describe the concrete problem, current architecture, workload, or constraint that motivates the decision. Link the current canonical document and relevant implementation when they already exist.

### Proposed decision / contract

State the exact architectural or behavioral delta being proposed. Separate current behavior from proposed behavior.

### Rationale / evidence

Record measurements, reproductions, source evidence, operational observations, or other reasons supporting the proposal. Distinguish measured facts from expectations that still need validation.

### Alternatives considered or rejected

Record meaningful alternatives and why they are not the current proposal. An alternative can remain a future experimental challenger instead of being permanently rejected.

### Consequences / trade-offs / non-goals

Document new coupling, complexity, resource cost, compatibility implications, failure modes, and what the decision explicitly does not attempt to solve.

### Acceptance / validation

Define how the proposal will be judged. Prefer measurable gates and real workload validation over aesthetic or implementation-only completion criteria.

## Source-of-truth rule

A decision-bearing Issue is **decision provenance**, not automatically the live contract.

For this fork, current source-of-truth precedence is:

1. current implementation and tests for executable behavior;
2. canonical repository documentation for the intended durable contract;
3. accepted decision-bearing Issues and their linked implementation history;
4. proposed or superseded Issues as historical context only.

When an accepted decision changes durable behavior, update the owning canonical documentation with the implementation and link the relevant Issue/PR.

## Multi-GPU decisions

The current canonical multi-GPU documents are:

- [`multigpu-shared-runtime.md`](multigpu-shared-runtime.md) — implemented shared-arena / independent-lane runtime contract;
- [`multigpu-hardware-guide.md`](multigpu-hardware-guide.md) — hardware sizing, RAM/CPU/PCIe guidance and bring-up checklist;
- [`multigpu-roadmap.md`](multigpu-roadmap.md) — future architecture challengers and promotion gates.

Concrete reference-host hardware, tuning values, benchmark numbers, and production validation records intentionally live in the separate public recipe repository: [`rhgo1749/qwen3.8-flash-next-strata-gpu-per-lane-recipe`](https://github.com/rhgo1749/qwen3.8-flash-next-strata-gpu-per-lane-recipe).

The production baseline remains independent GPU lanes with a shared host expert arena. Strata 0.1.30 at fork commit `dcdd46ff37b1baf5172a96389fbdc0c7b51a7dbc` is the current promoted engine generation after the reference-host implementation, scaling, memory-sharing, heterogeneous-isolation, workload, overload, and matched layer-split gates completed on one consistent generation. Upstream 0.1.30 contains the shared-arena primitive originally proposed by this fork in upstream PR #129, so the runtime now uses upstream's native `--shared-expert-arena` path rather than a fork-owned mmap interception wrapper. The matched reference-host A/B confirms the intended workload split: upstream three-GPU layer-split improves one warm request, while independent lanes provide substantially higher aggregate throughput for three simultaneous requests. Layer-split therefore remains an available single-request challenger rather than replacing request-level lanes as the serving baseline. Hardware-specific numbers and caveats live in the public recipe repository.

## Template for a decision-bearing Issue

```markdown
## Decision status

Proposed

## Context / problem

What exists today, what problem is being solved, and which canonical contract currently owns this behavior?

## Proposed decision / contract

What exact durable change is proposed?

## Rationale / evidence

What measurements, source evidence, or operational observations support it?

## Alternatives considered or rejected

What other approaches were considered, and why are they not the current proposal?

## Consequences / trade-offs / non-goals

What gets more complex, more coupled, more expensive, or explicitly remains out of scope?

## Acceptance / validation

What reproducible conditions must be true before this decision can be accepted or promoted?
```
