# Lane-local parking production-shaped canary — 2026-10-03

Branch: `exp/parking-supervisor-gate-20261003`. This is the final bounded canary before merging the parking path as an opt-in capability. It does **not** enable parking by default.

## Contract

The canary duplicates the production serving topology while staying on isolated ports:

- 3 independent lanes on physical GPUs 0/1/2;
- lane1 remains vision-capable;
- contexts 262144/262144/262144, KV budget 786432;
- production CPU partitions, PCIe fractions 0.55/0.25/0.55, KV resident 32768/lane, VRAM reserve 1200 MiB/lane;
- one shared expert arena;
- same production model/config and sampling defaults;
- parking opt-in: **4096 MiB / 4 slots per lane**, host `MemAvailable` floor **8192 MiB**.

The public production idle proxy/backend was left parked. The canary ran only on isolated ports 19800/19810-19812.

## Normal-operation canary

Six stable sessions were submitted concurrently for three turns. The scheduler settled at two affinity sessions per lane. No lane changed ownership between turns.

First isolated run:

| Turn | Wall | Mean E2E | Mean queue | Cached prompt tokens |
| ---: | ---: | ---: | ---: | --- |
| 1 | 5.07 s | 3.52 s | 1.18 s | all 0 |
| 2 | **3.05 s** | **2.07 s** | **0.59 s** | 721–779 each |
| 3 | **3.09 s** | **2.00 s** | **0.60 s** | 823–939 each |

After turn 3, all lanes were healthy/idle, each owned two affinity sessions, each reported one parked conversation, and engine-truth eviction count remained zero.

## Production-launcher canary

The local production launcher was extended with bounded opt-in environment variables while preserving `STRATA_CONVERSATION_CACHE_MIB=0` as the default. The launcher was then started on isolated canary ports with 4096/4/8192 parking settings and the same production vision topology.

Second run through that launcher:

| Turn | Wall | Mean E2E | Mean queue | Cached prompt tokens |
| ---: | ---: | ---: | ---: | --- |
| 1 | 5.62 s | 3.75 s | 1.20 s | all 0 |
| 2 | **3.67 s** | **2.31 s** | **0.72 s** | 709–780 each |
| 3 | **3.22 s** | **2.24 s** | **0.72 s** | 823–939 each |

Final status again showed exactly two affinities per lane, one parked conversation per lane, ~518–521 MiB parked per engine, zero evictions, zero queue depth, and all three lanes healthy. Vision lane1 participated normally in text serving.

The launcher-window resource monitor observed ~4.23 GiB peak `MemAvailable` decline from monitor start to minimum. That window includes engine startup/runtime movement and is **not** attributed solely to parking; engine-reported parked bytes (~1.6 GiB total at the final sample) remain the cleaner parking accounting signal.

## Lifecycle / cleanup

Terminating the production launcher closed the public canary port immediately, then completed its existing graceful child shutdown loop within roughly 10–12 seconds. At completion:

- backend PID file removed;
- 47 GiB shared-arena backing file removed;
- ports 19800/19810/19811/19812 all closed;
- no canary supervisor/lane/engine processes remained;
- host `MemAvailable` recovered materially after shutdown.

The 4-second intermediate sample still showed shutdown in progress; this was expected from the launcher's existing up-to-10-second graceful wait and was not an orphan-process leak.

## Decision

The controlled production-shaped canary passes. The lane-local parking implementation is suitable to merge as an **opt-in capability with production default OFF**. The next operational step is not another synthetic throughput sweep: allow an explicitly enabled real-use soak with live parked-count/bytes/evictions and RAM telemetry, retaining immediate rollback to `--conversation-cache-mib 0`.
