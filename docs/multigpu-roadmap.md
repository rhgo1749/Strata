# Multi-GPU runtime roadmap

This roadmap covers the experimental multi-GPU serving runtime in this fork. It is deliberately conservative: the independent-lane design remains the production baseline until a more complex mechanism demonstrates a repeatable end-to-end advantage on real serving workloads.

Roadmap authority is GitHub Issue #1 and its child Issues. This document summarizes the durable direction. Concrete reference-host hardware, tuning values, benchmark tables, and production validation records live in the separate public recipe repository: [`rhgo1749/qwen3.8-flash-next-strata-gpu-per-lane-recipe`](https://github.com/rhgo1749/qwen3.8-flash-next-strata-gpu-per-lane-recipe).

## Current production baseline

The current promoted engine generation is **Strata 0.1.31** in this fork, integrated from upstream `9259cad4cfa3543cd3b8decab5962672b968c649`.

The architectural baseline is:

- one ordinary Strata engine process per GPU lane;
- one upstream-native shared host expert arena;
- lane-local CUDA state, hot-expert cache, resident KV, session state, and failure boundary;
- per-lane context / resident-KV budgets;
- capability-aware routing such as vision-lane constraints;
- session affinity plus hardware-agnostic live-state-aware placement;
- routing above the engines rather than token-, layer-, or expert-level GPU synchronization.

Matched validation keeps the intended workload split: upstream layer-split remains a useful single-request challenger, while independent lanes remain the production baseline for concurrent serving. Hardware-specific measurements and caveats belong in the recipe repository.

## Design rule

Prefer coarse-grained request/session parallelism while it wins on the workload that matters.

More sophisticated mechanisms are not automatically better. Cross-GPU expert routing, migration, dynamic shared KV, learned control, or single-process distributed execution add synchronization, coupling, and larger failure domains. Introduce them only after the simpler serving-control stages below leave a measured gap.

## Phase 1 — Benchmark, observability, and interference characterization (completed)

Before changing policy, make the real decision variables observable.

Measure at least:

- queue delay, TTFT, E2E latency, TPOT/ITL where available, and throughput/goodput;
- reusable prefix / new-prefill work;
- active and queued work;
- session turn / stable session identity where available;
- live prompt/KV footprint;
- hot-expert hit/miss behavior;
- host RAM, CPU, PCIe/interconnect, GPU/VRAM, and power where measurable;
- lane health/restart and hard capabilities such as vision.

The workload matrix must separate cold/no-reuse, warm-prefix, multi-turn continuation, long-context, heterogeneous lengths, capability-constrained routing, `M > N` overload, cancellation/failure, and representative agent workloads.

### Matched interference probes

Run the same target request/lane under solo and concurrent conditions. Vary the other lanes' work while holding the target workload fixed.

The goal is to determine whether target-lane service cost is adequately explained by local work/load or whether a repeatable residual tracks shared host-memory / PCIe / expert-arena pressure.

Do not introduce a coupled cost model merely because resources are shared.

**Exit condition:** the baseline can be replayed, routing decisions are auditable, and shared-resource interaction is either shown immaterial or characterized well enough to test as a scheduler signal.

## Phase 2 — Stateful serving control while lanes remain independent (completed)

Phase 2 was evidence-gated and ordered. The promoted new-session placement policy is `balanced-additive-new-prefill-retained-state-proxy-v1`; the retained shared-pressure placement coefficient, bounded-admission default for the all-complete-immediately workload, and workload-regime adaptation were all evaluated and not promoted. `safe-affinity-live-state-v1` remains the rollback control.

### 2A — Strong simple placement baselines

Compare:

- first-free / round-robin;
- least-loaded;
- current session-affinity + live-state control;
- cache-aware + imbalance fallback;
- additive new-prefill + load cost;
- multiplicative new-prefill × load cost;
- session-first balance + cache-aware continuation.

Prefer the simplest policy that captures most of the gain. Benchmark-only online challengers keep existing-session affinity and all health/capability/FIFO constraints intact; they may change only new-session placement among otherwise eligible idle lanes. Because an idle candidate has no active decode load under the current one-request-per-lane contract, retained-state heuristics must be named and reported as proxies rather than mislabeled as engine-truth least-loaded scheduling.

### 2B — Coupling-aware cost only if Phase 1 proves it is useful

If matched interference experiments leave a reproducible residual, add the smallest observable shared-pressure term that improves held-out prediction and end-to-end serving.

Validate on unseen workload combinations, especially high-demand/high-pressure cases. Keep resource telemetry as the mechanism evidence.

### 2C — Admission and tail control

Placement is insufficient when the system is overloaded.

Under `M > N`, evaluate a bounded decision:

```text
route now | wait for a better lane | defer
```

Measure p50/p95/p99 queue delay and TTFT, E2E latency, goodput/TPS, starvation/fairness, active-session/cache pressure, and cancellation while queued/deferred.

The objective is to avoid throughput wins that hide tail collapse or sustained cache/state thrashing.

### 2D — Workload-regime adaptation only if necessary

Test the selected fixed policy across session-heavy, short-request, bursty, long-context-heavy, capability-mixed, and heterogeneous-length workloads.

If one fixed policy remains robust, stop.

If it degrades materially, adapt only a small set of interpretable policy weights/thresholds from recent telemetry. Learned/RL control is not justified unless simpler feedback leaves a measured gap.

**Exit condition:** the serving control improves a declared end-to-end or tail objective over strong simple baselines without regressing correctness, fairness, or failure isolation.

## Phase 3 — Startup/runtime lifecycle overhead (implemented / validated)

The promoted design removes repeated shared-arena source loading without changing the independent-lane inference model.

Implemented contract:

- lane 0 is the authoritative population leader for each supervisor generation;
- the shared header is marked incomplete before population and ready only after the full source load succeeds;
- later sequential lanes attach as followers, verify size/pack identity/readiness, and skip the repeated source expert load;
- the supervisor holds one non-blocking ownership lock for the arena pathname, preventing concurrent supervisors from repopulating the same backing;
- a new leader repopulates an existing compatible backing rather than trusting stale contents as persistent cache state;
- default supervisor-managed tmpfs backing is removed on graceful exit; explicit `--arena-file` lifetime remains operator-owned.

The reference-host promotion gate showed a matched 3-lane startup reduction while preserving text, vision, multi-turn affinity, malformed-input, cancellation, and recovery behavior. Hardware-specific timing belongs in the recipe repository.

Automatic child-process respawn and persistent cross-run arena reuse are not introduced by this phase. They remain separate lifecycle work only if a measured operational need justifies them.

## Conditional architecture challengers

These are evidence-triggered branches, not mandatory phases.

### Single-process multi-GPU execution

Prototype only if single-request underutilization is a material target bottleneck. Compare single-request latency/throughput, concurrent aggregate throughput, synchronization/interconnect cost, context capacity, power, and failure-domain cost.

### Distributed/coordinated hot-expert cache

Prototype only if host expert misses/traffic remain a dominant steady-state cost after scheduling improvements. Promotion requires reduced misses to outweigh new GPU-to-GPU communication and coordination.

### Dynamic/shared KV or migration

Keep deferred while every lane can admit the required context and placement/wait/recompute remain sufficient. Reopen only when measured capacity/utilization or overload behavior justifies the ownership, migration, and recovery complexity.

Research tracker: [#20 — cross-lane parked conversation migration](https://github.com/rhgo1749/Strata-Lanes/issues/20). Keep implementation parked while upstream Strata's conversation snapshot/cache/storage-tier interfaces are evolving; prefer consuming upstream state primitives over forking their snapshot format.

## Promotion gate

A challenger must demonstrate all of the following on repeated runs:

1. material improvement on a declared real workload objective;
2. no regression in required context/admission behavior;
3. no regression in tool/API, streaming, cancellation, malformed-input, and failure behavior;
4. stable memory use and lane health under soak;
5. an explainable gain after cold/warm/cache state is controlled;
6. acceptable power and interconnect cost;
7. held-out workload/regime validation where policy fitting is involved;
8. a clean independent-lane fallback.

## Boundary with model-internal research

This roadmap is **serving/runtime only**.

Sparse-attention/QSA/indexer training, PEFT, and model-internal retrieval policy are separate research. They are not later phases of this serving roadmap.

If Phase 1/2 measurements show a residual request-local long-context bottleneck after placement, queueing, and admission have been addressed, serving may export a neutral benchmark envelope:

- context/session distributions;
- latency decomposition;
- model-stage timing when already observable;
- memory-traffic envelope;
- serving SLO/quality constraints.

A separate model-research track may use that evidence. Any result returns to serving only after it independently demonstrates a useful quality/latency/memory Pareto improvement and can be exposed as a validated capability/profile without making the scheduler depend on the training method.

## Explicit non-goals

Until measurements justify them, this roadmap does **not** assume that Strata should:

- become a general tensor-parallel or pipeline-parallel engine;
- require GPU-to-GPU expert traffic for normal serving;
- merge lane-local session state into one distributed failure domain;
- implement unified KV merely for symmetry;
- introduce learned scheduling before simpler controls fail;
- make QSA/indexer research a prerequisite for serving progress;
- optimize benchmark aesthetics at the expense of real serving behavior.

## Near-term order

1. Keep the completed Phase 1/2 serving-control and Phase 3 lifecycle gates as regression controls on the promoted 0.1.31 baseline.
2. Do not add another mandatory serving phase without a measured residual.
3. Run conditional architecture challengers only when their documented trigger fires; upstream peer/expert-tier work is an execution primitive to evaluate separately, not an automatic replacement for independent lanes.

The default bias remains deliberate simplicity: add coupling only when measurements show it buys something.
