# Conversation parking robustness / observability gate — 2026-10-03

Branch: `exp/parking-supervisor-gate-20261003`. Production default remains parking OFF. This follow-up uses the experimental three-lane supervisor and a locally built Strata 0.1.38 engine with appended parking counters in the backward-compatible `DONE` line.

## Live telemetry smoke — PASS

`server.py /metrics` receives `parked_conversations`, `parked_bytes`, and `park_evictions` from the live engine. The supervisor status endpoint now best-effort polls each private lane and exposes the same aggregate engine-truth values. After six alternating smoke sessions each lane reported one parked conversation and roughly 354–366 MiB parked, with zero evictions.

## Client cancellation / disconnect — PASS

A streaming request was terminated client-side after ~1.5 s. The benchmark trace recorded `completion_reason=client_disconnect`; queue and busy counts returned to zero; all three lane wrappers remained healthy; and a subsequent request using the same affinity key completed normally.

## Engine-child death / restart — PARTIAL, BLOCKS PROMOTION

Killing only lane2's Strata child preserved the Python lane wrapper. A temporary experiment separating `engine loaded` from `wrapper routable` showed the wrapper remained reachable (`alive=false`, `routable=true`), but using wrapper health for scheduler eligibility broke existing supervisor scheduling tests and still did not provide transparent recovery. That scheduler change was therefore reverted. The first proxied requests during the dead-engine window returned HTTP 503. A direct request to the private lane successfully triggered the existing `server.py` restart path and returned HTTP 200 after the model came back. Once reloaded, the same affinity key again routed through the supervisor to lane2 and completed with `cached_tokens=0`, as expected because process-local parked snapshots were lost.

This exposes a pre-existing recovery-contract gap rather than a parking-state corruption: the wrapper can restart a dead child, but the supervisor proxy does not yet turn that restart window into transparent request recovery. Production parking must remain disabled until this path is resolved or explicitly specified as retryable failure behavior.

## Eviction / recompute fallback — PASS

Fifteen short stable sessions were admitted through the supervisor with four parking slots per lane. Engine-truth status reached one eviction on lane0 and three, then four, on lane2. Returning `evict05` to its remembered lane2 produced `cached_tokens=0` and a successful response, demonstrating safe ordinary prompt recomputation after snapshot eviction. Affinity ownership remained same-lane even though snapshot residency had been lost.

## Current gate decision

Parking correctness and performance mechanisms continue to look sound: live telemetry, cancellation cleanup, eviction accounting, and post-eviction recompute all pass. **The robustness gate remains open solely on dead-engine recovery semantics.** Next work should isolate why a proxied generation request receives 503 while a direct private-lane request can trigger `server.py` restart, then validate one transparent/retryable recovery contract. Do not run another throughput sweep before that is fixed.
