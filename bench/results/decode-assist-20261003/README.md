# Decode-assist helper probe — 2026-10-03

Branch: `exp/decode-assist-helpers-20261003`

This is a bounded challenger probe for upstream Strata `--expert-cache-device1` on the reference host. It is not a production promotion.

## Contract

- primary: physical GPU0, RTX 5070 Ti, PCIe Gen5 x8-capable slot
- 5060 Ti helper: physical GPU3, PCIe x4
- 5070 Ti helper reference: physical GPU2, **PCIe Gen5 x8-capable slot**; telemetry observed Gen5 x8 under load
- model: Qwen3.8-Flash-Next GSQ-RCO IQ3_S
- context/KV: 262144 / INT8 / resident 32768
- speculative decode: `--spec 4 --spec-min-p 0.5`
- identical primary CPU affinity for every arm: `1,4,7,10,13,17,20,23,26,29`
- helper mode: upstream `--expert-cache-device1 N`
- cold PP: three fixed 1.5K and three fixed 15K prompts per arm, 16-token completion
- warm decode: two warmups then five retained 768-token completions
- GPU telemetry: 1 s sampling of utilization, VRAM, power, clocks and temperature; the 5070 x8 arm additionally records live PCIe generation/width

## Results

| Arm | Short PP | 15K PP | Warm TG | Spec accept | Warm E2E | Helper mean util | Helper mean power | Helper VRAM |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| control | 866.7 | 2791.3 | **68.52** | 0.654 | 11.29 s | — | — | — |
| 5060 Ti, 5000 slots | 863.1 | 2794.3 | 66.92 | 0.647 | 11.56 s | 6.18% | 22.36 W | 10.21 GiB |
| 5060 Ti, 2500 slots | 864.9 | 2798.7 | 67.10 | 0.635 | 11.53 s | 3.99% | 21.29 W | 5.37 GiB |
| 5060 Ti, 1000 slots | 864.0 | 2794.0 | **68.80** | 0.667 | 11.24 s | 1.53% | 20.42 W | 2.47 GiB |
| 5070 Ti x8, 5000 slots | 862.8 | 2796.2 | 66.92 | 0.650 | 11.56 s | 5.55% | 42.51 W | 10.33 GiB |

Primary GPU0 stayed near saturation in every arm (~95.6–97.5% mean utilization, ~104 W mean power during the benchmark windows). Prompt processing stayed essentially unchanged across helper sizes, consistent with this helper path being decode-oriented.

The 5070 Ti helper's sampled link reached **PCIe Gen5 x8** on both primary and helper while the arm was active. Despite the faster helper GPU and x8 link, the 5000-slot arm reproduced the 5060 Ti 5000-slot result at 66.92 tok/s. That argues against helper arithmetic throughput being the dominant limiter at this operating point and is consistent with host-staged transfer/synchronization overhead dominating the additional expert hits.

## Acceptance-adjusted read

A pooled exploratory regression over the 25 retained warm samples (`decode_tok_s ~ speculative_acceptance + arm`, control reference) gives approximate arm coefficients:

| Arm | Acceptance-adjusted TG delta vs control |
| --- | ---: |
| 5060 Ti, 5000 slots | **-1.32 tok/s** |
| 5060 Ti, 2500 slots | **-0.69 tok/s** |
| 5060 Ti, 1000 slots | **-0.22 tok/s** |
| 5070 Ti x8, 5000 slots | **-1.44 tok/s** |

The 1000-slot raw result slightly exceeds control, but its speculative acceptance is also higher; after adjustment the apparent win disappears. Treat this as a bounded directional probe, not a formal causal estimate.

## Interpretation

The current `--expert-cache-device1` helper path **does preserve prefill**, but this first probe does not show a decode-throughput win on the reference host. Larger helper caches improve the visible expert hit rate while reducing end-to-end decode throughput, and a faster PCIe Gen5 x8 RTX 5070 Ti helper does not rescue the 5000-slot arm. The strongest current hypothesis is that pinned-host round-trip / per-layer synchronization cost exceeds the saved CPU expert work for these helper sizes.

The 1000-slot 5060 Ti arm is the only configuration close enough to control to justify any follow-up. Before changing serving policy, a future probe should measure per-request remote expert counts, CPU-pool drain/phase timing and PCIe bytes directly, and compare a very small hot-expert sweep around 500–1500 slots. Do not promote decode assist from this result.

## Resource evidence

Raw GPU telemetry and request metrics are retained under `raw/`. The initial CPU-affinity-contaminated heterogeneous experiment from the earlier 0.1.38 campaign is unrelated and is not used here.
