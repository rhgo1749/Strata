# Three-lane supervisor conversation-parking gate — 2026-10-03

Branch: `exp/parking-supervisor-gate-20261003`. Strata 0.1.38 reference host, production-shaped three independent lanes (GPU0 x8, GPU1 x4 vision-capable, GPU2 x8), shared expert arena, fixed production CPU/PCIe/KV partitions. Production remains parking-off; the challenger uses an explicit experimental supervisor flag with **4096 MiB / 4 slots per lane** and an 8192 MiB host `MemAvailable` floor.

The synthetic workload creates distinct stable session IDs and sends 4, 6, then 9 sessions concurrently. Each scenario has two turns. Turn 1 is cold/new-session work; turn 2 returns every session to the lane selected by the existing strict affinity map, so the only intended difference is whether that lane can restore a parked conversation or must reread the prefix. Exact queue lease records and 1 s host/GPU telemetry are retained.

## Matched result

| Concurrent sessions | Arm | Turn 1 wall | Turn 2 wall | Turn 2 mean E2E | Turn 2 mean queue | Turn 2 aggregate completion TPS |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 4 | OFF | 7.17 s | 5.41 s | 3.30 s | 0.585 s | 71.0 |
| 4 | ON | 7.13 s | **4.38 s** | **3.08 s** | **0.523 s** | **81.5** |
| 6 | OFF | 9.66 s | 9.68 s | 5.74 s | 1.917 s | 59.5 |
| 6 | ON | 9.97 s | **5.68 s** | **3.57 s** | **1.190 s** | **91.9** |
| 9 | OFF | 14.70 s | 14.70 s | 7.79 s | 3.908 s | 58.8 |
| 9 | ON | 14.85 s | **8.86 s** | **5.17 s** | **2.582 s** | **95.0** |

Cold turn-1 behavior is essentially unchanged, which is the desired control. On returning turn 2, parking changes the overloaded regimes materially:

- M=6: wall **-41.3%**, mean E2E **-37.8%**, mean queue **-37.9%**, aggregate completion TPS **+54.3%**.
- M=9: wall **-39.7%**, mean E2E **-33.6%**, mean queue **-33.9%**, aggregate completion TPS **+61.5%**.
- M=4 also improves, but the gain is smaller because only one session needs to share a lane.

Engine logs confirm real same-lane restores, typically restoring ~1219–1322 prompt tokens in roughly 13–20 ms before reading only the fresh suffix. The 4-slot cache also accumulated **1–2 evictions per lane** by the later scenario because the benchmark intentionally ran 4→6→9 distinct sessions without restarting the engines. Those evictions safely fell back to ordinary prompt processing; they are also evidence that the supervisor needs cache occupancy/eviction telemetry rather than assuming every remembered affinity key is still parked.

Host `MemAvailable` fell by about **5.57 GiB** from benchmark-window start to minimum with parking ON versus about **0.99 GiB** with parking OFF. This includes general process/runtime movement, so per-engine parked-byte telemetry is the better future accounting source.

## Experimental integration and observability

This branch adds opt-in supervisor CLI knobs for per-lane parking while preserving the production default of zero. The existing supervisor affinity table remains authoritative: parking never moves a session across lanes, and an engine miss/eviction recomputes normally.

The branch also appends aggregate parking state to the engine's backward-compatible `DONE` line (`parked_conversations`, `parked_bytes`, `park_evictions`) and teaches `serve/server.py` to retain those fields in request history. This addresses occupancy/eviction observability without changing request semantics. Incremental build succeeds; the full `serve.test_server` suite passes (119 tests) and the supervisor suite passes (61 existing + the new parking sanitizer case).

## Decision

The **three-lane performance/control-plane gate passes**: bounded same-lane parking materially improves alternating `M>N` workloads while preserving the independent-lane routing model and safe recompute fallback. It is **not production-promoted yet**. Remaining promotion work is robustness-focused rather than another throughput sweep: live-smoke the new DONE telemetry on the experimental binary, expose per-lane parked-count/bytes/evictions in supervisor status/trace, then validate cancellation, lane/engine death/restart, and post-eviction recompute under parking before changing the production default.
