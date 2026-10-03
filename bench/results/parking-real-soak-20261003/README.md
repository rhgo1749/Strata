# Lane-local parking production-path soak — 2026-10-03

This experiment uses the **actual production public path** (`127.0.0.1:8087` idle/wake proxy → `127.0.0.1:18087` three-lane supervisor), not the isolated canary ports. It is still a deterministic mixed synthetic/agent-like workload rather than organic user traffic, so the result is a production-path soak rather than a claim about every real workload.

## Contract and rollback

The promoted production backend was parked before the matched A/B. The production config was temporarily pointed at the already validated patched `build/strata` binary (SHA256 `a1793a6e3f65dc271f8fa1af6148b374aac7398e431b3f94e40010846049a3bd`). Both arms therefore use the same engine binary, model, topology, CPU/PCIe/KV partitions, vision lane, public idle proxy, request bodies, session IDs, and deterministic sampling.

- OFF: `--conversation-cache-mib 0`
- ON: `--conversation-cache-mib 4096 --conversation-cache-slots 4 --conversation-cache-min-free-mib 8192`
- six stable sessions, mixed coding/research/ops/design/debug/planning prompts;
- three concurrent turns through public port 8087;
- six sessions resolve to two affinities per lane.

After measurement the backend was explicitly parked and the production config/launcher were restored to the promoted `engine/strata` binary and parking default `0`.

## Exact matched result

| Turn | Parking OFF wall | Parking ON wall | Wall delta | OFF mean E2E | ON mean E2E | E2E delta | Aggregate completion TPS delta |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 (cold/wake) | 36.185 s | 36.223 s | +0.1% | 33.185 s | 33.228 s | +0.1% | -0.1% |
| 2 (returning) | 9.686 s | **6.232 s** | **-35.7%** | 6.063 s | **4.333 s** | **-28.5%** | **+55.4%** |
| 3 (returning) | 9.688 s | **6.571 s** | **-32.2%** | 5.508 s | **4.091 s** | **-25.7%** | **+59.1%** |

Cold/wake behavior is effectively unchanged. On returning turns, parking ON restored 964–1064 prompt tokens on turn 2 and 1305–1315 on turn 3 for all six sessions. With parking OFF, turn 2 restored zero for all six; turn 3 retained one ordinary live-state hit (`cached_tokens=1305`) while the other five sessions reread their prefixes.

At the end of the ON arm the backend reported:

- exactly two affinity sessions per lane;
- one parked conversation per lane;
- parked bytes 571,567,136 / 571,728,692 / 571,746,968 (~1.60 GiB total);
- zero parking evictions;
- zero queue depth and zero busy lanes.

At the end of the matched OFF arm all three engines reported zero parked conversations/bytes/evictions.

## RAM telemetry caveat

The ON resource monitor began before the idle proxy woke the backend, so its initial `MemAvailable` drop includes shared-arena/model/runtime startup and must **not** be attributed to parking. Engine-reported parked bytes are the clean accounting signal for this run. The 8192 MiB physical-RAM floor remained configured throughout.

## Initial false baseline

An earlier OFF pass in this same directory attached to a production backend that had already been awakened at 22:44:26, before this experiment changed the config at 22:50. That pass correctly showed no parking but is not used for the exact matched conclusion. `soak-patched-off.json` is the authoritative OFF arm for the A/B above.

## Decision

The actual production path reproduces the earlier supervisor/canary benefit with the confound removed: same patched engine, parking as the only intended serving-state difference. This gate passes for mixed synthetic/agent-like returning sessions. The implementation should remain **opt-in/default OFF** until a short organic-use observation confirms bounded RAM/evictions and no latency or recovery regression under the user's natural workload. No further synthetic throughput sweep is required before that observation.
