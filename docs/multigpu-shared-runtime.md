# Experimental multi-GPU shared runtime

This fork provides an opt-in Linux runtime that keeps one ordinary Strata engine process per GPU lane while sharing the large host expert arena between processes.

Detailed reference-host hardware, tuning values, benchmark tables, and validation records live in the separate public recipe repository: [`rhgo1749/qwen3.8-flash-next-strata-gpu-per-lane-recipe`](https://github.com/rhgo1749/qwen3.8-flash-next-strata-gpu-per-lane-recipe).

## Runtime contract

- One Strata engine process runs per GPU lane.
- The large host expert arena is backed by a shared Linux mapping when the opt-in shared-arena settings match the exact expert allocation size.
- CUDA state, hot-expert cache, resident KV, session state, and process lifecycle remain lane-local.
- Host-KV capacity is configured per lane rather than through a dynamic cross-lane allocator.
- CPU affinity is partitioned between lanes by default so independent expert worker pools do not collide on the same physical cores.
- Generation requests are leased to one lane for the duration of the request or stream.
- Conversation requests preserve lane affinity when the supervisor can identify a stable session, so lane-local KV/checkpoints survive across turns instead of being discarded by request-level round-robin routing.
- The default single-GPU Strata path is unchanged when the shared-arena mode is not enabled.

## Shared arena

Strata 0.1.30 provides the file-backed arena primitive upstream through `--shared-expert-arena FILE` (originating from this fork's upstream PR #129). On Linux, upstream `PinnedArena` maps one shared file with a 4 KiB header containing the arena geometry and pack fingerprint; incompatible size or pack identity is refused before the arena is used. The ordinary anonymous/hugetlb path remains unchanged when the option is absent.

The lane supervisor now composes that upstream primitive rather than intercepting `mmap`: it strips any inherited shared-arena option, adds the same explicit `--shared-expert-arena` path to every shared lane, and removes the option entirely for the private-arena benchmark arm. The default backing lives under `/dev/shm`, matching upstream's tmpfs contract; an explicit `--arena-file` can select another operator-managed tmpfs path.

Lane startup is currently sequential because each ordinary engine initialization still populates the mapped arena. A future leader/follower initialization mechanism can remove that repeated source load without changing the steady-state sharing model.

## Lane-local state

Every lane keeps its own normally resident GPU weights, GPU hot-expert tier, CUDA streams and graphs, GPU-resident KV window, conversation/session state, and failure boundary. The authoritative long-context KV continues to use Strata's existing host-memory streaming path.

## CPU and topology tuning

The supervisor can automatically partition physical CPU cores between lanes. It also exposes optional per-lane CPU, PCIe/cache, context, and resident-KV controls for asymmetric hosts.

Those values are intentionally not prescribed here. They are hardware-specific and should be measured on the target system; the public recipe repository contains one concrete reference-host example.

## Hardware guidance

The multi-GPU path is not tied to one GPU model or lane count. The practical rule is that **every selected GPU must first be able to run one usable single-GPU Strata lane**, while the host must have enough RAM, CPU and PCIe capacity for all lanes concurrently.

A concise starting guide:

- shared-arena multi-process mode: Linux;
- GPU count: 2 or more NVIDIA GPUs;
- VRAM: satisfy the chosen single-GPU Strata configuration on every lane; 16 GB+ per GPU is a useful multi-lane target for additional hot-cache/KV headroom;
- RAM: one shared expert arena + every lane's host-KV + OS/runtime headroom;
- CPU: the current automatic partitioner needs at least 2 physical cores per lane; 4–6 physical cores per active lane is a more practical starting target when available;
- PCIe: confirm the negotiated link for every card and measure asymmetric lanes rather than copying another host's `pcie-frac` values;
- storage: SSD, preferably NVMe;
- NVLink: not required by the normal GPU-per-lane decode path.

The validated 3-lane IQ3_XXS reference configuration uses 128 GB RAM, three 16 GB GPUs, 262K context per lane and a 16-core CPU. Those values are a **validated reference**, not universal minimum requirements.

See [`multigpu-hardware-guide.md`](multigpu-hardware-guide.md) for the sizing rationale, RAM model, CPU/PCIe guidance and bring-up checklist.

## Host-KV capacity

The current implementation treats host-KV as per-lane capacity, not as a dynamic cross-lane pool. Each engine process owns an independent host-KV budget and requests are scheduled to one free lane at a time.

Operators can choose equal or asymmetric context limits according to system RAM and workload requirements. If every lane can expose the required full model window, a dynamic cross-lane KV allocator may provide little practical benefit relative to the coordination complexity it adds.

GPU-resident KV is also per lane. Increasing it can displace the GPU hot-expert cache, so it should be tuned together with expert residency rather than maximized in isolation.

## Request-level parallelism

The production parallelism unit is a whole request or session, not a token, tensor, layer, or expert. This avoids mandatory cross-GPU communication in the normal decode path and preserves comparatively small failure domains.

The supervisor prefers explicit `X-Strata-Session-Id`, conversation/session/thread identifiers in the request, and otherwise derives a privacy-safe best-effort key from the first user message. It keeps a bounded LRU mapping from session keys to lane indices, so several conversations may remain associated with the same engine and use Strata's per-engine prompt-cache checkpoints. A later turn waits for its remembered lane when that lane is busy rather than spilling to another GPU and forcing a full prompt reread; while it waits, that lane is reserved from newly awakened sessions so condition-variable wake-up order cannot steal the cache-rich lane. New sessions use a compatible FIFO wait queue: capability-constrained work such as vision does not block unrelated lanes, but otherwise older compatible waiters receive a newly released lane first. Among eligible idle lanes, placement prefers a truly empty live state (`live_request_bytes == 0`), then the smallest live request state, then the least-recently-used live state, with the rotating cursor as the final tie-breaker. This policy is lane/GPU agnostic: no GPU index, model, or PCIe-width preference is hard-coded. Using a lane for another conversation does not erase older session-to-lane mappings. Requests with no derivable session key still participate in the same busy-lane exclusion, compatible FIFO queue, and live-state placement policy; they simply do not receive cross-turn affinity.

The trade-off is explicit: preserving a live session can leave another GPU idle briefly or add queueing behind that session's lane. That is preferable to repeatedly paying long-context prefill for the same conversation. More tightly coupled multi-GPU designs remain roadmap challengers and must demonstrate an end-to-end win before promotion.

For controlled serving measurements, `--bench-trace-jsonl FILE` is an opt-in observability path and does not alter the ordinary serving policy. Trace schema 2 writes a `benchmark_manifest` at supervisor startup with the fork commit when available, config hashes, lane topology/capabilities, context/KV/CPU/PCIe settings and shared-arena identity. Each generation request then writes a `lane_lease` record with queue-entry, admission, lane-start, first-response-byte and release timestamps; exact scheduler queue wait, service and end-to-end time; HTTP/completion/error outcome; and response byte count. For streaming requests, first response body byte is also reported as supervisor-observed TTFT; non-streaming first-byte time is retained separately and is not mislabeled as token TTFT.

The same lease record contains an auditable scheduler snapshot: active/queued work, session turn when a stable identity exists, the selected reason, and per-lane health/capability/live-state/placement-key components. Until engine-truth cache overlap is exported, reusable-prefix/new-prefill work is explicitly labeled as `routing_history_exact_message_prefix_bytes_v1`: the supervisor hashes canonical messages and compares only identical leading history, retaining byte counts rather than prompt text. This is an experiment-only approximation, not a claim about tokenizer blocks or resident KV. Bench clients may additionally supply measured token counts and experiment labels through `X-Strata-Benchmark-Input-Tokens`, `X-Strata-Benchmark-Reusable-Prefix-Tokens`, `X-Strata-Benchmark-New-Prefill-Tokens`, `X-Strata-Benchmark-Output-Target-Tokens`, `X-Strata-Benchmark-Workload`, `X-Strata-Benchmark-Cache-State`, and `X-Strata-Benchmark-Interference-Arm`.

The proxy also exposes `X-Strata-Lane-Index`, `X-Strata-Admission-Rank`, `X-Strata-Queue-Wait-Ms` and an echoed benchmark request ID on generation responses. `/__multigpu/status` exposes current new-session queue depth/bytes, affinity/vision waiter counts and busy-lane count so an external sampler can retain queue-depth trajectories without reaching into scheduler internals.

## Current promoted engine baseline

The shared-lane runtime is based on upstream Strata **0.1.30**, promoted at fork commit `dcdd46ff37b1baf5172a96389fbdc0c7b51a7dbc`. The 3-lane reference contract remains one engine per GPU, 262144 context and 32768 resident KV per lane, disjoint CPU partitions, asymmetric per-lane PCIe tuning, and one upstream-native shared expert arena.

The 0.1.30 compatibility pass keeps upstream's resident-expert/cache-complement path, conversation snapshot/cache machinery, sampler/MTP changes, RoPE-scaling support, multi-GPU layer split, and native shared-arena implementation while retaining the fork's adaptive tracing, supervisor, exact benchmark lease tracing, and session-aware lane scheduler. The supervisor strips inherited `gpu` / `layer_split` settings so a lane cannot accidentally re-expand into a coupled layer split.

The shared-arena implementation is no longer a fork-owned mmap wrapper: the supervisor passes upstream's native `--shared-expert-arena` option to each lane. It explicitly forces `--conversation-cache-mib 0` until parked-conversation locality is modeled, and it preflights public/private ports before expensive model loading so stale listeners cannot satisfy readiness for a new lane.

The promoted generation passed the full Python serving suite (125 tests, 5 skipped), the sm_120 CUDA 13.4 build, 38 of 41 registered CTests, and real shared-lane generation smoke. The remaining three CTests depend on absent external model/pack fixtures (`ple_parity`, `expert_parity`, `pool_test`) and are recorded as unavailable rather than passes.

Reference-host measurements then completed the promotion matrix on the same generation: 1→2→3 independent-lane scaling, native shared-arena PSS, RTX 5070 Ti + RTX 5060 Ti isolation, workload sensitivity, rotated mixed serving, exact-queue 3/4/6/9-request overload, and a matched independent-lanes ↔ three-GPU layer-split A/B. The A/B shows the intended workload regions rather than one universal winner: layer split improves a single warm request, while independent lanes provide much higher aggregate throughput with three simultaneous requests. Exact hardware-specific values and raw retained observations live in the public recipe repository.

Adaptive hot-expert replacement remains engine-local to each lane. Optional `STRATA_ADAPT_TRACE` instrumentation records first routed misses, adaptive swap selection/publication, and the first later GPU-resident hit without changing the default serving path when tracing is disabled. Reference-host timing and A/B results live in the public recipe repository.

Controlled reference-host evidence for the architecture also includes **1→2→3 lane scaling, private-vs-shared arena PSS, and mixed RTX 5070 Ti + RTX 5060 Ti isolation**. The measurements, conditions, and caveats live in [`docs/systems-ablation-20260929.md`](https://github.com/rhgo1749/qwen3.8-flash-next-strata-gpu-per-lane-recipe/blob/main/docs/systems-ablation-20260929.md); this implementation document intentionally does not duplicate hardware-specific result tables.

## Compatibility and limitations

The supervisor proxies Strata's OpenAI-compatible generation endpoints and preserves streaming. Functional serving behavior is covered by repository tests and reusable probes; concrete reference-host soak counts belong in the recipe repository.

Current boundaries:

1. Shared-arena mode is Linux-only.
2. Host-KV capacity is statically configured per lane.
3. Source expert loading is still repeated during sequential lane startup.
4. Hot-expert caches are lane-local; there is no required cross-GPU ownership scheme.
5. The supervisor is focused on generation serving and may route other endpoints through one lane.
6. Session affinity is only as strong as the available identity. Explicit session/conversation/thread IDs are authoritative; the first-user-message fallback is best effort and can change if a client rewrites or compacts away the first user turn.
7. Context lengths beyond the model/runtime's validated range remain experimental.
