# Multi-GPU runtime roadmap

This roadmap covers the experimental multi-GPU runtime in this fork. It is intentionally conservative: the current independent-lane design remains the production baseline until a more complex design proves a repeatable advantage on real serving workloads.

Concrete reference-host hardware, tuning values, benchmark tables, and production validation records live in the separate public recipe repository: [`rhgo1749/qwen3.8-flash-next-strata-gpu-per-lane-recipe`](https://github.com/rhgo1749/qwen3.8-flash-next-strata-gpu-per-lane-recipe).

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

## Latest promotion checkpoint — Strata 0.1.24 (2026-09-30)

The independent-lane baseline was revalidated after syncing upstream Strata 0.1.24 into the fork (`82a5161`). The existing 3-lane launch contract remained unchanged: 262144 context and 32768 resident KV per lane, 5/6/5 physical-core partitioning, 0.55/0.25/0.55 lane PCIe fractions, and one shared expert arena.

On the reference 3 × RTX 5070 Ti host, the 0.1.24 candidate passed 52 server/multi-GPU tests and the production CUDA build. IQ3_XXS clean warm three-request wall aggregate ranged **218.4–233.7 tok/s** (mean **225.5 tok/s**); 15K no-reuse prompt processing measured **2453.5 / 2046.0 / 2450.8 tok/s** across the x8/x4/x8 lanes. IQ3_S clean warm aggregate ranged **176.8–199.4 tok/s** (mean **187.1 tok/s**); 15K PP measured **2381.8 / 1852.2 / 2371.8 tok/s**.

The same binary kept upstream layer-split operational: IQ3_XXS clean warm single-request decode measured **93.5–99.9 tok/s** with 15K PP **1144.4 tok/s**, while IQ3_S measured **74.2–81.7 tok/s** with 15K PP **938.0 tok/s**. A no-reuse ~140K prompt was also served concurrently on all three IQ3_S lanes without OOM or lane death. The independent-lane path therefore remains the production baseline for concurrent agent serving; layer-split remains the single-request challenger.

A controlled IQ3_S single-lane A/B also confirmed that adaptive hot-expert replacement is materially useful on this workload: two retained 512-token adaptive rounds averaged **69.43 tok/s** versus **54.14 tok/s** with `--adapt-swaps 0` (**+28.3%**), while hit rate rose from about **61%** to **86.5–87%**. Detailed miss/swap/residency timing and caveats belong in the public recipe repository.

Reference-host benchmark detail and historical comparisons belong in the public GPU-per-lane recipe repository; this document records only the architecture-level promotion outcome.

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
