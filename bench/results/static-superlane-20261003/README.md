# Static Super-Lane 2+1 probe — 2026-10-03

Experimental branch: `exp/static-superlane-2plus1-20261003`.

Topology: physical GPU0 + GPU2 (both Gen5 x8) form one upstream 2-GPU layer-split logical Super-Lane; physical GPU1 (Gen5 x4) remains one ordinary independent lane. CPU affinity is disjoint: the Super-Lane receives the union of the former GPU0/GPU2 lane CPU sets (20 logical CPUs), while the ordinary lane retains its 12-CPU partition. Both logical lanes share one upstream shared expert arena.

The matched baseline is production `1+1+1`, with M2 using the two x8 ordinary lanes and M3 using all three ordinary lanes. Requests use the same fixed ~1.5K reasoning prompt, fresh session IDs, 512 generated tokens, one exact-concurrency warmup, then five retained runs.

| Topology | M2 aggregate TG | M2 common wall | M3 aggregate TG | M3 common wall |
| --- | ---: | ---: | ---: | ---: |
| 1+1+1 baseline | **132.19 ± 1.68** | 7.75 s | **174.33 ± 2.09** | 8.81 s |
| static 2+1 | **133.84 ± 2.77** | 7.65 s | **147.93 ± 1.67** | 10.38 s |

For M2, static 2+1 is only **+1.24%** (+1.64 tok/s); an approximate two-sided 95% interval on the difference is about [-1.70, +4.99] tok/s, so this run does not establish an advantage. For M3, static 2+1 is **-15.15%** (-26.40 tok/s), with an approximate interval [-29.16, -23.64] tok/s: the queued second request on the Super-Lane loses decisively to three independent lanes.

Additional retained observations from the first pass: the 2-GPU Super-Lane single-request decode measured ~98.86 tok/s versus ~67.62 tok/s on the x4 ordinary lane. Cold ~1.5K PP measured ~1042 tok/s on the 2-GPU Super-Lane, ~876 tok/s on an x8 ordinary baseline lane, and ~454 tok/s on the x4 ordinary lane. These explain why layer split remains useful as a single-request primitive without making static `2+1` the preferred always-on topology.

GPU telemetry is retained as CSV. The static topology used GPU0/GPU2 as the x8 Super-Lane and GPU1 as the x4 ordinary lane; production baseline telemetry is retained separately. No production promotion is made from this probe.

**Decision:** static `2+1` does not pass the promotion gate as an always-on replacement for `1+1+1`. It preserves the known single-request layer-split benefit, but M2 gain is not established and M3 goodput regresses materially. Elastic Super-Lane lifecycle should therefore remain gated unless a scheduler can form it only for a sufficiently valuable single-request window after paying drain/reconfigure/load/restore cost.
