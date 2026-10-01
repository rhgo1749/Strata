# Paper v2 evidence snapshot

**Evidence date:** 2026-10-01 (KST)

## Status

- **Only paper v1 has been submitted.**
- No paper v2 manuscript or revision has been submitted as of this snapshot.
- `paper-v2-evidence` is a post-v1 evidence anchor for a possible future revision.
- This does **not** mean that a v2 manuscript already exists or has been submitted.
- Reserve `paper-v2` for the actual revised manuscript/submission state.

## Frozen repository state

### Strata-Lanes

- Repository: `rhgo1749/Strata-Lanes`
- Runtime generation used for retained measurements:
  `dcdd46ff37b1baf5172a96389fbdc0c7b51a7dbc`
- The later promotion commit only changes canonical documentation; it does not change runtime execution code.
- `paper-v2-evidence` is kept aligned with the repository's current `main` so the public evidence anchor is unambiguous.

### Reproducibility recipe / retained evidence

- Repository: `rhgo1749/qwen3.8-flash-next-strata-gpu-per-lane-recipe`
- Retained evidence generation before this manifest:
  `91323755cabc96c97ba311f668f105276d809d81`
- It contains the retained raw data, benchmark summaries, promotion record, and reporting contract.
- `paper-v2-evidence` is kept aligned with the repository's current `main`.

## Engine provenance

- Upstream Strata v0.1.30:
  `30ec18ec7094550fcc594fd948220d511d80464e`
- Measured Strata engine SHA-256:
  `cc4236096662b1786a7730316002cbd38850f7451b517102fe4fec89eadf0147`
- CUDA: 13.4
- NVIDIA driver: 615.71.09
- Model: Qwen3.8-Flash-Next GSQ-RCO IQ3_S

## Key post-v1 evidence

### 1 → 2 → 3 independent-lane scaling

| Lanes | Common-wall aggregate TG |
|---|---:|
| 1 | 70.804 ± 1.546 tok/s |
| 2 | 132.760 ± 3.129 tok/s |
| 3 | 189.486 ± 3.205 tok/s |

- 3-lane speedup: 2.676×
- 3-lane parallel efficiency: 89.2%

### Native shared expert arena

- Private two-engine PSS: 98.924864 GiB
- Shared two-engine PSS: 52.083863 GiB
- Saved: 46.841001 GiB
- Reduction: 47.35%

### Heterogeneous isolation

- RTX 5070 Ti solo: 71.563 ± 1.760 tok/s
- RTX 5070 Ti concurrent with RTX 5060 Ti: 71.163 ± 1.456 tok/s
- RTX 5060 Ti concurrent: 57.575 ± 1.527 tok/s
- Common-wall concurrent aggregate: 113.898 ± 2.985 tok/s

### Mixed three-lane serving

- Common-wall aggregate: 184.669 ± 5.966 tok/s
- n = 9 retained runs

### Exact-queue oversubscription

| Requests | Aggregate TG | Queue p50 / p95 | E2E p50 / p95 |
|---:|---:|---:|---:|
| 3 | 190.35 tok/s | 5.1 / 8.2 ms | 7.96 / 8.28 s |
| 4 | 140.33 tok/s | 6.6 / 7433.7 ms | 7.96 / 14.62 s |
| 6 | 192.39 tok/s | 11.3 / 8300.6 ms | 8.30 / 16.54 s |
| 9 | 195.41 tok/s | 7704.4 / 15744.7 ms | 15.59 / 23.60 s |

- 92 retained requests
- exact server-side queue timing
- no missing queue-wait values
- all retained requests ended with `finish_reason=length`

### Matched independent lanes vs 3-GPU layer split

**One warm request**

- Independent single lane: 70.804 ± 1.546 tok/s
- Three-GPU layer split: 102.976 ± 1.527 tok/s
- Layer split: +45.4% in this single-request region

**Three simultaneous requests**

- Three independent lanes: 189.486 ± 3.205 tok/s common-wall aggregate
- Three-GPU layer split: 102.588 ± 1.629 tok/s common-wall aggregate
- Independent lanes: +84.7% aggregate throughput in this three-request region

The ordinary upstream layer-split server serves the three generation requests serially/FIFO rather than continuously batching them.

## Intended future revision use

If a revision is requested, this evidence can support:

1. A matched Strata 0.1.30 independent-lanes vs layer-split comparison.
2. An explicit workload-region conclusion:
   - layer split for lower single-request decode latency;
   - independent lanes for higher concurrent-serving aggregate throughput.
3. Upstream-native shared-arena memory evidence.
4. Exact overload queueing and tail-latency evidence.
5. Same-generation replacements for the v1 headline scaling, heterogeneous, mixed-workload, PP/TTFT, and provenance measurements.

## Naming convention

- `paper-v1`: submitted manuscript state.
- `paper-v2-evidence`: post-v1 experimental evidence frozen before any revision submission.
- `paper-v2`: reserve for the actual revised manuscript/submission state.
