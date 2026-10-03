# Hermes eval live-use parking validation — 2026-10-03

This record captures the first live-use validation of lane-local conversation parking through Hermes itself rather than a direct synthetic HTTP harness.

## Contract

- public path: `127.0.0.1:8087` idle/wake proxy → `127.0.0.1:18087` three-lane supervisor;
- three production lanes: RTX 5070 Ti ×3 (GPU0/GPU1/GPU2);
- RTX 5060 Ti excluded from the serving pool;
- Strata 0.1.38 with the promoted parking/telemetry/recovery patchset;
- per-lane parking: **4096 MiB / 4 slots / 8192 MiB MemAvailable floor**;
- Hermes profile: `eval`, with provider/model overridden per invocation to the configured local Strata provider and `qwen3.8-flash-next-strata-iq3-s`;
- real Hermes named session persistence and `--pass-session-id`;
- six named sessions (A–F), repeatedly alternated and resumed.

No prompt text is retained in this repository. Only serving-level observations are recorded.

## Observed behavior

Hermes' normal system/tool prompt made each returning request roughly **25K tokens**, substantially larger than the earlier synthetic parking probes.

Representative healthy parking hits:

| Lane | Prompt tokens | Reused | Freshly read | Prompt time |
| ---: | ---: | ---: | ---: | ---: |
| 0 | 25,840 | 25,592 | 248 | 1.253 s |
| 0 | 25,863 | 25,613 | 250 | 1.278 s |
| 1 | 25,882 | 25,618 | 264 | 2.149 s |
| 2 | 25,875 | 25,617 | 258 | 1.275 s |

For comparison, first-use / no-parking reads of approximately 25.1K tokens took roughly **11.5–11.7 s** of prompt processing on the x8 text lanes in the same session.

## Real eviction / fallback

Because Hermes snapshots are large, the **4 GiB byte budget became the limiting factor before the 4-slot count** on lane1.

After repeated alternating turns:

- lane0: 3 affinity sessions, 2 parked, ~1.73 GiB parked, 0 evictions;
- lane1: 3 affinity sessions, 2 parked, ~2.86 GiB parked, **1 eviction**;
- lane2: 1 affinity session, 1 parked, ~1.10 GiB parked, 0 evictions.

A revisit after lane1's eviction completed successfully with:

- prompt tokens: 25,865;
- reused: **16,384**;
- freshly read: **9,481**;
- prompt time: ~8.00 s;
- conversation continuity preserved.

This is the desired failure mode: snapshot residency is an optimization, not the source of truth for the conversation. Stable same-lane affinity remains intact and a miss/eviction falls back to partial or full prompt recomputation.

## Host resource observation

During the Hermes validation window, host `MemAvailable` moved from roughly **52.4 GiB** to a minimum of **43.7 GiB**, ending around **44.8 GiB**. This includes ordinary model/runtime/cache movement and must not be interpreted as parking-only allocation. Engine-reported parked bytes are the cleaner parking accounting signal.

All three lanes ended idle with zero queue depth and no failed requests.

## Decision

The live-use Hermes validation passes.

On the reference host:

- production uses **three RTX 5070 Ti lanes only**;
- lane-local conversation parking is enabled at **4096 MiB / 4 slots / 8192 MiB floor per lane**;
- the RTX 5060 Ti remains excluded from the serving pool;
- rollback remains `--conversation-cache-mib 0`.

The important operational conclusion is that for large agent prompts, **byte budget matters more than the nominal slot count**. Increasing `conversation-cache-slots` without increasing or re-evaluating the MiB budget may not increase the effective number of parked Hermes sessions.
