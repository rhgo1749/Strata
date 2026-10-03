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

## Matched hot-expert sweep (session 2)

A second matched decode-only sweep reran the control and 500/750/1000/1250/1500-slot RTX 5060 Ti helper arms in one session. Every arm used the same primary GPU, CPU affinity, fixed ~1.5K prompt, two warmups and seven retained 768-token completions. `STRATA_SPLIT_TIMING=1` and the engine's native remote-helper counters were retained alongside 1 s GPU telemetry.

| Helper slots | TG mean ± sd | Accept | Acceptance-adjusted ΔTG | Helper util | Helper power | Helper VRAM | Remote entries/request | Returned MiB/request | Host stage ms/request | Helper wait ms/request | Final stage0 pool+plan ms/window |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | **68.49 ± 1.41** | 0.663 | control | 0% | 4.24 W idle | 2 MiB | — | — | — | — | 8.512 |
| 500 | 68.46 ± 1.03 | 0.666 | -0.23 | 1.08% | 19.08 W | 1.52 GiB | 2,652 | 25.9 | 26.1 | 6.4 | 8.247 |
| 750 | 67.79 ± 1.37 | 0.647 | +0.07 | 1.56% | 19.99 W | 1.99 GiB | 4,160 | 40.6 | 36.6 | 11.9 | 8.057 |
| 1000 | 68.23 ± 0.86 | 0.649 | **+0.42** | 2.13% | 20.68 W | 2.47 GiB | 5,519 | 53.9 | 43.9 | 17.1 | 7.900 |
| 1250 | 67.76 ± 1.43 | 0.649 | -0.02 | 2.76% | 21.45 W | 2.96 GiB | 7,156 | 69.9 | 53.9 | 23.0 | 7.897 |
| 1500 | 68.34 ± 1.05 | 0.660 | -0.02 | 3.15% | 21.88 W | 3.43 GiB | 8,385 | 81.9 | 59.3 | 28.6 | 7.733 |

The acceptance-adjusted model uses all 42 retained samples (`decode_tok_s ~ speculative_acceptance + arm`, control reference). The best point, 1000 slots, is only **+0.42 tok/s with SE 0.30 tok/s**, so its approximate 95% interval crosses zero. No slot count in this sweep establishes a repeatable decode-throughput win.

The mechanistic signal is still useful: as slots increase, the verifier's cumulative `pool + plan` term falls from 8.512 to 7.733 ms/window, showing real CPU-side work reduction, while helper entries, returned bytes, staging time, wait time, VRAM and power all rise monotonically. The current host-staged helper path therefore trades less CPU expert work for more cross-device coordination without a proven end-to-end TG gain. A shallow optimum may exist around ~1000 slots, but it is not yet distinguishable from run-to-run/speculative-acceptance variance.

**Decision:** keep decode assist experimental and do not promote it into production scheduling. A future follow-up should only proceed if it can reduce the host-staged synchronization cost or measure a workload where CPU expert drain is materially worse than this warm 1.5K/decode regime; simply adding more helper slots is not supported by this sweep.

## Transport and critical-path follow-up (session 3)

This session tested three concrete explanations for the missing decode gain at the 1000-slot operating point.

1. **Mapped-host zero-copy vs explicit async copies.** `STRATA_REMOTE_ZEROCOPY=0` measured 68.11 tok/s versus the retained default-zero-copy result around 68.23 tok/s at effectively identical speculative acceptance. The default zero-copy path is not the observed bottleneck.
2. **Helper CUDA scheduling.** `STRATA_REMOTE_SPIN=0` measured 68.06 tok/s and did not improve the result. Spin scheduling is not the observed bottleneck.
3. **Repeated CUDA device switching.** An experimental single-helper-only sticky-device patch held the helper CUDA device across the CPU-pool interval, cutting the primary↔helper context-switch pair from two scopes to one. On the same patched binary, OFF measured 68.13 ± 1.20 tok/s and ON measured 68.11 ± 1.41 tok/s. Native helper timing was also unchanged (`begin` about 44 ms/request, `wait` about 18 ms/request). The device-switch hypothesis is rejected for this workload.

The stronger explanation is **critical-path slack**. In the matched sweep, stage-0 timing is roughly 19 ms/window waiting for the primary GPU versus only ~8 ms/window in `pool + plan`. The CPU expert pool therefore completes well before the primary GPU path. A helper can reduce CPU work without reducing wall time because that work is already hidden under primary-GPU execution.

A mechanism probe with `--pool-workers 4` confirms the same operating regime:

| Arm | Warm TG | Spec accept | Final GPU wait | Final pool+plan |
| --- | ---: | ---: | ---: | ---: |
| 4 CPU workers, no helper | **68.58** | 0.665 | 18.991 ms/window | 8.608 ms/window |
| 4 CPU workers + 1000-slot 5060 Ti helper | **68.56** | 0.654 | 19.437 ms/window | 7.859 ms/window |

Even four CPU workers remain comfortably off the critical path on the reference 9950X3D host. The helper measurably lowers CPU pool work but cannot accelerate decode until CPU expert work approaches or exceeds the primary-GPU stage time.

**Current conclusion:** the P2P-free helper is not a useful production accelerator on this reference host/workload. Its likely applicability is a more CPU-constrained host, a workload/configuration with materially higher CPU expert drain, or a future execution path that changes the primary-GPU critical path. Further slot tuning or micro-optimizing helper context/staging on this host is not justified by the current evidence.
