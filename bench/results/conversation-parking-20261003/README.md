# Lane-local conversation parking probe — 2026-10-03

Branch: `exp/lane-local-parking-20261003`. Strata 0.1.38 reference host, one ordinary RTX 5070 Ti x8 lane, fixed CPU affinity, direct single-lane server. This is a mechanism/pressure gate, not production promotion.

The workload alternates independent exact-prefix conversations. Each returning conversation sends its full prior message history, so parking can restore the same lane's running state instead of rereading that prefix. Distinct first tokens prevent unrelated sessions from sharing a root prefix. No production trace is required for this mechanism gate.

## Four sessions, three turns

Parking OFF rereads every conversation after a switch. Parking ON uses an 8 GiB / 4-slot host-RAM cache.

| Arm | Turn 1 mean E2E | Turn 2 | Turn 3 |
| --- | ---: | ---: | ---: |
| OFF | 3.55 s | 3.65 s | 3.57 s |
| ON, 4 slots | 3.58 s | **2.78 s** | **2.50 s** |

The parking log shows initial snapshots around 262 MB/conversation, park around 41–42 ms, and restores around 10–15 ms. Later incremental captures reuse roughly 25–31 MB of KV bytes and grow snapshots as the conversations grow.

## Capacity pressure: six sessions

| Arm | Turn 1 mean E2E | Turn 2 mean E2E | Result |
| --- | ---: | ---: | --- |
| OFF | 3.52 s | 3.64 s | full reread |
| ON, 4 slots | 3.56 s | 3.69 s | **thrash** |
| ON, 6 slots | 3.55 s | **2.81 s** | restore works |

With 6 sessions / 4 slots, the cache reached 7 evictions and produced no useful E2E gain. With 6 slots, the run had zero evictions, six restores averaging ~11.9 ms, and turn-2 E2E improved from 3.643 s to 2.815 s (**~22.7%**). The parked cache reached about 2.01 GiB; host `MemAvailable` fell by about 2.56 GiB at peak during the benchmark window versus ~0.27 GiB for OFF. The difference includes process/runtime noise, so engine-reported cache bytes are the cleaner parking-memory accounting.

The current `/metrics` request fields report `reused=0` and an unchanged prompt-ms value even when engine logs prove a parked restore occurred. Treat this as an observability gap: engine `conversation cache: restored ...` logs are the source of truth for this probe until parked-token/restore telemetry is exported explicitly.

## Control-plane implication

The Lanes supervisor already retains multiple session-affinity keys per lane and routes a returning affinity session back to its remembered lane, waiting for that lane when necessary. That is sufficient for an initial **same-lane-only** parking experiment: an engine-side snapshot eviction may lose the performance benefit but safely falls back to ordinary prompt processing. The supervisor still does not know which affinity entries are actually parked, so parking must remain experimental until parked ownership/eviction telemetry is surfaced and bounded admission is validated.

## Decision

The **mechanism gate passes**: lane-local parking can materially reduce alternating-session E2E when the cache can hold the active working set. Capacity sizing is not optional; undersized slot counts can erase the gain through deterministic LRU thrash. Do not enable parking in production yet. The next gate is a three-lane supervisor A/B that preserves strict affinity, enables bounded lane-local parking, exports parked-state ownership/eviction telemetry, and tests alternating M>N sessions, cancellation/failure, and miss/recompute fallback.
