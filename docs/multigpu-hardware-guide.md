# Multi-GPU hardware guidance

This guide is for this fork's **Linux GPU-per-lane serving mode**. It is intentionally separate from the upstream single-GPU requirements.

The runtime is not tied to one GPU count or one exact machine. The practical rule is:

> **Every selected GPU must be able to run one usable single-GPU Strata lane, while the host must have enough RAM, CPU and PCIe capacity for all lanes at once.**

The large expert arena is physically shared between lane processes, but host-KV, GPU-resident KV, CUDA state, hot-expert cache and session state remain lane-local.

## Hardware guidance

| Component | Practical starting point | Recommended for multi-lane use | Validated reference host |
| --- | --- | --- | --- |
| OS | Linux | Current 64-bit Linux | Ubuntu Linux |
| GPU count | 2 NVIDIA GPUs | 2–4 GPUs, each independently usable by Strata | 3 GPUs |
| VRAM per GPU | Must satisfy the chosen single-GPU Strata configuration; upstream Strata starts at 12 GB for supported model sizes | **16 GB+ per GPU** gives more room for hot-expert cache and resident KV | 3 × RTX 5070 Ti 16 GB |
| System RAM | One shared expert arena + **all lane-local host-KV** + OS/runtime headroom | Size from the actual model/quant/context plan; **128 GB is the validated recommendation for the 3-lane 262K ×3 IQ3_XXS reference configuration** | 128 GB |
| CPU | At least 2 physical cores per lane for the current automatic partitioner | **4–6 physical cores per active lane** is a useful starting target for CPU-expert work | Ryzen 9 9950X3D, 16C/32T; split 5 / 6 / 5 |
| PCIe | Each GPU needs a stable usable link | Prefer wider links where available; measure asymmetric lanes instead of assuming equal settings | Gen5 x8 / x4 / x8 |
| Storage | SSD | NVMe SSD | NVMe |
| NVLink | Not required | Not required | None |
| PSU / cooling | Must sustain the selected GPUs and CPU together | Size for simultaneous multi-GPU load with normal electrical and thermal headroom | Host-specific |

These are **guidelines, not universal minimums**. Model quantization, context length, resident KV, expert-cache target, CPU performance and PCIe topology all change the useful configuration.

### RAM sizing

Do not derive multi-GPU RAM needs by multiplying the expert arena by the number of GPUs. The fork shares that large arena physically.

A better mental model is:

```text
required host RAM ≈
    one shared expert arena
  + lane 0 host-KV
  + lane 1 host-KV
  + ...
  + OS / server / filesystem-cache headroom
```

For example, the validated Qwen3.8-Flash-Next IQ3_XXS reference host uses one shared expert arena of about 39.97 GiB and three independent 262,144-token host-KV capacities inside a 128 GB machine.

That does **not** make 128 GB a universal requirement. Smaller quants, fewer lanes or shorter contexts can require less; more lanes, larger contexts or a larger model can require more.

### GPU guidance

Mixed-performance GPUs are valid because one request is leased to one lane and the normal decode path does not require token-by-token synchronization across GPUs.

A slower GPU therefore affects the request assigned to that lane rather than directly setting the token rate of every other lane. Each GPU still needs enough VRAM for its own dense/QSA/MTP state, hot-expert tier, CUDA state and configured resident-KV window.

Start by proving that **each GPU works as a normal single-GPU Strata configuration** before enabling the multi-lane supervisor.

### CPU guidance

The current supervisor partitions physical cores and keeps SMT siblings together. Automatic partitioning requires at least two physical cores per lane, but that is a correctness floor rather than a performance recommendation.

CPU expert work can materially affect decode throughput. A practical tuning workflow is:

1. keep lane CPU sets disjoint;
2. begin around 4–6 physical cores per lane when the host allows it;
3. measure every lane alone;
4. then measure all lanes concurrently;
5. change the split only when the concurrent result justifies it.

### PCIe guidance

There is no single portable `pcie-frac` value. Link width, generation, root-complex sharing and CPU behavior all matter.

The reference machine validated Gen5 x8 / x4 / x8 and benefited from asymmetric per-lane tuning. Treat that as evidence that an x4 lane can be useful, **not** as proof that every x4 topology will behave the same way.

Use `nvidia-smi` / system topology tools to confirm the negotiated links on your own host and tune `--lane-pcie-fracs` from measurements.

## Before enabling multi-GPU serving

1. Get a normal single-GPU Strata configuration working on every GPU you plan to use.
2. Confirm available system RAM after the model's shared expert arena is loaded.
3. Pick conservative per-lane contexts and resident-KV values.
4. Use automatic CPU partitioning first unless you have a measured reason to override it.
5. Validate one lane, then two lanes, then full concurrency.
6. Test cold long prompts, not only short warm decode.
7. Validate streaming, tool calls, cancellation and lane recovery before treating the setup as production-ready.

For a concrete measured example, see the public recipe repository:

[`rhgo1749/qwen3.8-flash-next-strata-gpu-per-lane-recipe`](https://github.com/rhgo1749/qwen3.8-flash-next-strata-gpu-per-lane-recipe)
