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

The implementation extends `PinnedArena` with an exact-size file-backed mode on Linux. Only the allocation matching the derived expert-arena size uses the shared backing. Other pinned allocations keep the normal path.

The supervisor derives the expert-arena size from the native expert metadata and supplies the same backing file and expected byte size to every lane process.

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

## Current promoted engine baseline

The shared-lane runtime is currently based on upstream Strata **0.1.27**. The 3-lane launch contract remains unchanged: one engine per GPU, 262144 context and 32768 resident KV per lane on the reference host, disjoint CPU partitions, per-lane PCIe tuning, and one shared expert arena.

The 0.1.27 promotion preserved the CUDA/shared-arena path and the supervisor continues to strip inherited `gpu` / `layer_split` settings from lane configs so an upstream multi-GPU config cannot accidentally re-expand a lane into layer-split mode. Post-paper serving hardening adds multi-session lane affinity and live-state-aware placement without changing the engine execution model.

### Strata 0.1.29 sync candidate

An isolated sync branch rebases the lane runtime onto upstream Strata 0.1.29 without changing the one-engine-per-lane execution contract. The compatibility pass keeps upstream's request-cancellation/session reset fixes, prompt-path fatal-CUDA handling, MTP/native-head VRAM reservation, verify-window bounds guards, prompt-kernel changes, and split sampler while retaining the fork's shared-arena wrapper, adaptive tracing, supervisor, and scheduler.

The shared-arena wrapper still embeds the upstream pinned-arena implementation byte-for-byte; the 0.1.29 upstream `src/core/pinned.cu` hash matches the fork's `src/core/pinned_upstream_impl.cu`. Source-level serving tests, an sm_120 CUDA 13.4 build, available CUDA parity tests, real-model single-lane output parity against 0.1.27, stream-cancellation recovery, one-lane shared-arena serving, and a three-lane concurrent smoke run have passed on the reference host. Model-fixture-dependent tests that require the repository's optional `pack/full/experts.bin` or PLE capture fixtures remain unavailable in the isolated worktree and are not counted as passes.

This is intentionally a **sync candidate, not yet the promoted measurement baseline**. The reference recipe and paper-facing headline tables must be regenerated on one consistent 0.1.29 generation before promotion; historical 0.1.27/earlier performance numbers are not evidence for 0.1.29.

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
