---
title: "Masked-video-upload flag: enablement prerequisites & fail-open hardening"
status: pending
priority: high
created: 2026-06-08
source: ce-work execution + code-review of refactor/unified-recording-pipeline
related_plans:
  - docs/plans/2026-06-05-002-refactor-unified-recording-processing-pipeline-plan.md
---

# Masked-video-upload flag: enablement prerequisites & fail-open hardening

`config.get_masked_video_upload_enabled()` defaults **False** and MUST stay OFF until all of the below land. The default-OFF config ships safely (capture-time blocking is the structural cloud-video guarantee). These are the conditions to flip it ON, plus latent fail-open issues in the dormant ON path that the code review surfaced.

## Prerequisites to enable the flag

1. **Live-finalize cutover** (see 001) — until the live in-process upload routes through U6 / the terminal stage, flag-ON ships rich unmasked video via the live path.
2. **Native-redaction-review "what uploads" reconciliation** — the review window still labels video "local-only, not uploaded." It must show that masked video uploads before an operator can meaningfully consent. (Plan scope-boundary / consent-gate.)
3. **SECURITY.md** already documents the trade-off and these gates — keep it in sync when flipping.

## Latent fail-open issues in the dormant ON path (fix before enabling)

- **Upload seam has no video-masked assertion.** Unlike `recording.db` (guarded by `assert_uploadable`), there is no gate at the upload boundary asserting a cloud `chunk_*.mp4` is masked/blocked. Cloud video safety is implicit (relies on the distant capture-time blocking or the terminal masker). Consider an explicit assertion at the cloud-upload enqueue when the flag is ON.
- **Coverage gate proves coverage over the caller-supplied span, not the actual decoded frame extent.** `terminal_stage._chunk_timing` derives uniform `chunk_dur` spans, but recordings are action-gated VFR and the last chunk's real duration differs. Frames decoded outside `[start_ts,end_ts]` get no interval (drawn clear) and a case-(c) chunk still reports MASKED (regions>0) with trailing unmasked frames. Derive the real per-chunk `[start,end]` from the chunk's own PTS extent (and reconcile with `recovery.py`'s chunk-range derivation, which extends the last chunk to `last_ts+1.0`).
- **Scrubbed-copy reuse guard does not hash `masked_video/` contents.** `is_scrubbed_copy_reusable` hashes DB/events/manifests; a stale/under-masked `masked_video/chunk_*.mp4` from an earlier run with a different policy could be re-uploaded without re-masking. Include the masked-video provenance in the reuse check.
