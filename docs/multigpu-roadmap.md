# Multi-GPU runtime roadmap

This roadmap covers the experimental multi-GPU runtime in this fork. It is intentionally conservative: the current independent-lane design remains the production baseline until a more complex design proves a repeatable advantage on real serving workloads.

Concrete reference-host hardware, tuning values, benchmark tables, and production validation records live in the separate public recipe repository: [`rhgo1749/strata-gpu-per-lane-serving-recipe`](https://github.com/rhgo1749/strata-gpu-per-lane-serving-recipe).

## Current production baseline

The architectural baseline is:

- one ordinary Strata engine process per GPU lane;
- one shared host-RAM expert arena backed by `MAP_SHARED`;
- lane-local CUDA state, hot-expert cache, resident KV, and session state;
- per-lane context and resident-KV budgets chosen for the target host;
- disjoint CPU affinity between lanes;
- optional lane-specific PCIe/cache tuning where measurements justify it;
- sequential lane startup while the shared arena is populated through the ordinary engine initialization path;
- routing above the engines rather than token-, layer-, or expert-level synchronization between GPUs.

The baseline should stay easy to disable and should not alter the default single-GPU numerical path.

## Design rule

Prefer coarse-grained request/session parallelism while it wins on the workload that matters.

A more sophisticated multi-GPU mechanism is not automatically an improvement. Cross-GPU expert routing, token-level synchronization, dynamic KV movement, or a single-process distributed engine all introduce synchronization and data movement. They should only replace the current lane model after an A/B test shows a material and repeatable gain without reducing stability, required context capacity, or API/agent correctness.

## Phase 1 — Benchmark and observability contract

Before changing the architecture, make the comparison reproducible.

- Keep a fixed 1-, 2-, and 3-request benchmark set that includes representative real workloads.
- Record per-lane and wall-clock TTFT, prompt-processing throughput, decode throughput, queue delay, and end-to-end latency.
- Record hot-expert hit rate, resident-KV pressure, host-RAM use, CPU utilization/affinity, PCIe traffic, GPU utilization, and wall power when available.
- Separate cold-start, cold-expert, warm-expert, and prompt-cache cases.
- Preserve long-context admission and tool/streaming/cancellation correctness as hard gates, not optional benchmark dimensions.
- Store enough environment/config metadata to reproduce each result.

Exit condition: architecture experiments can be compared against the independent-lane baseline without relying on anecdotal single runs.

## Phase 2 — Smarter lane scheduling without changing the engine

Improve utilization while keeping lanes independent.

- Preserve session affinity when it avoids unnecessary state/cache churn.
- Make dispatch queue-aware and lane-health-aware rather than only "first free lane".
- Include lane-specific topology/capability information in scheduling decisions only where it measurably matters.
- Avoid sending new work to a degraded or restarting lane.
- Keep cancellation and failure isolated to the affected lane.
- Measure whether prompt length, expected generation length, or current hot-cache state are useful scheduling signals before depending on them.

Exit condition: measurable end-to-end or reliability improvement on representative mixed concurrency with no regression in failure isolation.

## Phase 3 — Remove duplicated startup/runtime overhead

Target overhead that does not require distributed inference.

Candidates:

- leader/follower or equivalent initialization so the shared expert arena is populated once rather than re-read once per lane;
- faster readiness/restart behavior for an individual lane;
- clearer ownership/lifecycle of the shared arena backing file;
- stronger startup validation for arena size/model/config mismatches;
- optional persistence/reuse mechanisms only where they do not compromise correctness after model/config changes.

Do not add a dynamic unified host-KV allocator merely for symmetry. Revisit it only if a future workload, model, or memory limit creates a real capacity/utilization problem.

## Phase 4 — Experimental architecture challengers

These are benchmark branches/feature flags, not assumed destinations.

### A. Single-process multi-GPU execution

Prototype only if Strata can share enough scheduling/runtime state to make one request use multiple GPUs efficiently.

Compare against the lane baseline for:

- single-request decode throughput;
- multi-request aggregate throughput;
- TTFT and prompt-processing throughput;
- inter-GPU/PCIe traffic and synchronization overhead;
- required long-context capacity;
- fault isolation and restart cost;
- wall power / tokens per joule.

A single-request win is not sufficient if the normal concurrent-serving workload becomes worse overall.

### B. Distributed or coordinated hot-expert cache

Test non-overlapping or coordinated expert residency only behind an experimental path.

Required evidence:

- fewer costly host expert misses;
- cross-GPU traffic remains below the cost it replaces;
- no narrower or contended link becomes a persistent decode bottleneck;
- aggregate decode improves under the actual MoE routing distribution;
- failure and fallback behavior remain defined.

If expert ownership forces frequent GPU-to-GPU transfers on decode, reject the design even if aggregate VRAM utilization looks cleaner.

### C. Dynamic/shared KV allocation

Defer by default. Reopen only when static per-lane allocation becomes a real constraint, for example on a lower-memory host, a future model with materially larger KV requirements, or a workload with costly uneven context demand.

Any implementation must justify migration/coordination cost and must not make session failure recovery more fragile than the current lane-local model.

## Promotion gate for a challenger

The independent-lane runtime remains the production default unless a challenger demonstrates all of the following on repeated runs:

1. a material improvement in the target workload, not only a synthetic microbenchmark;
2. no regression in the required production context/admission contract;
3. no regression in tool-call, streaming, cancellation, and malformed-input behavior;
4. stable memory use with no lane/GPU death under soak;
5. an explainable gain after accounting for cold/warm cache state;
6. acceptable power and interconnect cost for the throughput gained;
7. a clean fallback path to the independent-lane runtime.

As an initial engineering target, treat a repeatable ~10% or larger end-to-end/aggregate improvement as clearly worth investigating. Smaller gains can still be accepted when they materially improve latency, power, reliability, or operational simplicity, but should not justify a large increase in architectural coupling by themselves.

## Explicit non-goals

Until measurements justify them, this roadmap does **not** assume that Strata should:

- become a general tensor-parallel or pipeline-parallel engine;
- require GPU-to-GPU expert traffic for normal serving;
- merge lane-local session state into one distributed failure domain;
- replace the existing single-GPU numerical path;
- implement a unified KV allocator solely because the current implementation is statically partitioned;
- optimize benchmark aesthetics at the expense of real serving behavior.

## Near-term order

1. Freeze the benchmark and observability contract.
2. Improve scheduler/observability while keeping lanes independent.
3. Remove redundant startup overhead.
4. Run architecture challengers as isolated A/B experiments.
5. Promote a challenger only when fresh measurements clear the promotion gate.

The default bias is deliberate simplicity: keep the current coarse-grained architecture until evidence shows that finer-grained multi-GPU coupling is actually better for the target hardware and workload.
