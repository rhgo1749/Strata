# Same-lane parking dead-engine recovery follow-up — 2026-10-03

Branch: `exp/parking-supervisor-gate-20261003`. This follow-up isolates the only remaining robustness blocker from the previous parking gate: a lane wrapper could survive while its Strata child died, but the supervisor discarded that lane's affinity and/or rejected proxying before `server.py` could exercise its existing synchronous child restart path.

## Root cause

The supervisor used child-engine liveness for two distinct contracts:

1. **new-session eligibility** — correctly requires a currently loaded engine; and
2. **affinity ownership lifetime** — incorrectly dropped affinity whenever the child engine was down, even if the private Python lane wrapper was still alive and capable of restarting it.

A second post-acquire `lane_engine_alive()` guard also prevented a returning affinity request from reaching the wrapper, so direct private requests could restart the child while supervisor-proxied requests returned immediate 503.

## Minimal fix

Keep new sessions restricted to loaded engines. Preserve an existing affinity while its lane **wrapper process** remains alive. For a returning affinity only, if the child is down but `/health` says the wrapper is reachable, select that same lane with `selected_reason=session_affinity_restart` and allow the request to reach `server.py`. That request then uses the existing synchronous `ensure_loaded()/restart()` path. If the wrapper itself is gone, affinity is still dropped as before.

No general wrapper-health scheduling was introduced. The earlier attempt to treat every wrapper-alive lane as a normal candidate was rejected because it broke the established scheduler contract.

## Live validation

Three sessions were first spread across the three lanes; `rr2` owned lane2. The lane2 Strata child was then SIGKILLed while its Python wrapper stayed alive.

- Immediately after death: lane2 `alive=false`, `routable=true`, affinity_sessions=1.
- An unrelated new session was admitted on healthy lane1, **not** lane2.
- That unrelated request did **not** erase `rr2`'s lane2 affinity.
- A single supervisor request for returning `rr2` then waited for lane2's child restart and returned **HTTP 200 on lane2** in **5.07 s**.
- The response reported `cached_tokens=0`, which is correct because the killed process lost its in-process parked snapshots; the full prompt safely recomputed.
- Final status returned lane2 to `alive=true`, `routable=true`, affinity_sessions=1.

This closes the prior 503 recovery gap without letting new work spill onto an unloaded/dead child.

## Regression / memory-bound checks

- `serve.test_multigpu_server`: **63/63 PASS** after the affinity-restart change.
- `serve.test_server`: **119/119 PASS** from the same experimental parking/telemetry branch; this follow-up changes only supervisor scheduling/recovery semantics.
- focused host-side `conversation_cache_test`: PASS.
- focused `conversation_memory_test`: PASS, including exact floor admission, below-floor rejection, unknown-memory fail-closed, and overflow guards.

## Decision

The previously open **engine-child death/restart blocker is resolved on the reference host**. Combined with the earlier three-lane performance, cancellation, live telemetry, eviction, post-eviction recompute, and bounded cache/memory checks, the experimental lane-local parking path has now passed the planned mechanism, control-plane, and robustness gates.

Do **not** flip the production default in this experiment branch. The next roadmap step is a controlled production canary/opt-in with bounded per-lane cache, status telemetry, and an immediate rollback to `--conversation-cache-mib 0` if RAM pressure, recovery failures, or latency regressions appear.
