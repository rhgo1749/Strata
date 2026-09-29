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

The trade-off is explicit: when only one request is active, other GPU lanes may be idle. More tightly coupled multi-GPU designs remain roadmap challengers and must demonstrate an end-to-end win before promotion.

## Current promoted engine baseline

The shared-lane runtime has been revalidated after syncing upstream Strata **0.1.24** in merge commit `82a5161`. The 3-lane launch contract remains unchanged: one engine per GPU, 262144 context and 32768 resident KV per lane on the reference host, disjoint CPU partitions, per-lane PCIe tuning, and one shared expert arena.

The promotion passed 52 server/multi-GPU tests, the production CUDA build, IQ3_XXS and IQ3_S per-lane versus layer-split benchmarks, and a 140K-token no-reuse request on all three lanes concurrently. The supervisor also strips inherited `gpu` / `layer_split` settings from lane configs so an upstream multi-GPU config cannot accidentally re-expand a lane into layer-split mode.

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
6. Context lengths beyond the model/runtime's validated range remain experimental.
