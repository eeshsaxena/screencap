---
module: models/download
date: 2026-07-28
problem_type: design_pattern
component: background_job
severity: medium
related_components:
  - daemon/model_download_job
  - macos/Screencap/Controllers/ModelDownloadController
tags:
  - progress-reporting
  - blocking-call
  - background-thread
  - huggingface
  - event-throttling
  - observability
  - stall-detection
applies_when:
  - "A third-party SDK call blocks for a long time with no progress callback"
  - "A UI renders a determinate progress bar from a value the backend only updates at the endpoints"
  - "A blocking dependency call has no wall-clock bound of its own"
  - "Adding a sampler that feeds an already-throttled event publisher"
---

# Progress and stall detection for a blocking SDK call with no callback

## Context

`screencap model download` fetches a ~1.7 GB model through one blocking call to
`huggingface_hub.snapshot_download`. That call offers no byte-level callback and
no overall deadline, which produced two defects in sequence:

1. **Invisible progress** (SCR-291, PR #437). The engine reported only at its two
   endpoints — `progress_cb(0, total)` before the fetch and
   `progress_cb(total, total)` after transfer plus sha256 verification. A healthy
   multi-minute download was pixel-identical to a hang; a QA tester reported it
   stuck and skipped it.
2. **Unrecoverable hangs** (SCR-292). Making a stall *visible* does not make it
   *recoverable*. A wedged transfer still blocked forever: the daemon job stayed
   `running`, the status verb kept saying `downloading`, and the idle-shutdown
   busy predicate pinned the process. Cancel could not land either, because the
   stop flag was only read once the fetch returned.

The general shape: **when the only progress signal is a function that does not
call you back, a determinate progress UI is lying by construction, and an
unbounded call is a liveness bug waiting for a bad network.** Every layer can be
individually correct while the composed system is indistinguishable from frozen.

## Guidance

### 1. Sample the observable side effect — but first prove the work lands there

The fetch writes files to a staging directory, so that directory looks like the
progress signal: measurable from outside the SDK, on any version, and identically
under a test double.

**That assumption was wrong for the shipped model, and it shipped.** Both pinned
variants are Xet-backed, and `hf-xet` is a *core* dependency of `huggingface_hub`
on arm64/x86_64 — not an extra. The Xet path streams chunks into a **global**
cache (`~/.cache/huggingface/xet/chunk-cache`) and assembles the file into
`local_dir` only at the end. Measured on the pinned 1.7 GB weights:

| transfer path | staged bytes | longest flat plateau |
|---|---|---|
| Xet (the default) | frozen at 63,354 B while the HF cache grew | **43.0s of a 45s window** |
| plain HTTP | grew to 482 MB in 30s, in 10 MiB steps | **3.5s** |

So PR #437's bar never actually moved for the model users download. Worse, the
stall detector built on the same signal would have failed *healthy* downloads at
its 300s threshold — strictly worse than the hang it was fixing. The fix is to
force the path the measurement assumes:

```python
@contextmanager
def _xet_disabled():
    """Force the plain-HTTP transfer path so bytes land where we measure."""
```

**The lesson is the check, not the flag.** A side-effect signal encodes an
assumption about *where* the dependency does its work. Verify it against the real
dependency, on the real artifact, before building anything on top — a fake
`snapshot_fn` in CI will confirm your assumption no matter how wrong it is. One
45-second instrumented run against the actual pinned repo would have caught both
defects; nothing in the test suite could.

### 2. One watcher thread, not a sampler beside a blocking call

The original design ran the fetch inline and a sampler thread beside it. Once the
call also needs a deadline, invert it: run the **fetch** on a worker thread and
watch it from the caller's thread. One loop then serves progress, stall
detection, and cancellation:

```python
thread = threading.Thread(target=_work, name="model-download-fetch", daemon=True)
thread.start()
last_bytes = -1        # never equal to a real reading, so sample #1 arms the clock
last_change = time.monotonic()
while True:
    thread.join(interval)
    if not thread.is_alive():
        break
    if stop_event is not None and stop_event.is_set():
        _abandon(_FetchCancelled())
    staged = _staged_bytes(staging)          # RAW, not clamped — see below
    now = time.monotonic()
    if staged != last_bytes:
        last_bytes, last_change = staged, now
    elif now - last_change >= stall_timeout_s:
        _abandon(_FetchStalled(now - last_change))
    if progress_cb is not None:
        progress_cb(min(staged, total), total)
if failure:
    raise failure[0]                          # hand the worker's exception back
```

This *removes* the ordering problem the previous version needed three cooperating
guarantees for (stop-flag re-check, bounded join, slow work between them). With
reporting confined to one thread, a stale sample cannot land after the terminal
reading. **Prefer a structure that makes the race impossible over a comment
asserting an invariant the code does not enforce.**

Two details that are easy to get backwards, both mutation-tested:

- **The stall clock must read the raw count, the consumer the clamped one.** A
  staging dir that grows past the manifest total (metadata sidecars) pins the
  clamped value, which then reads as a stall on a healthy transfer.
- **The worker's exception must be handed back explicitly.** Drop
  `raise failure[0]` and a `ConnectionError` becomes an empty staging dir, which
  surfaces as `missing-file` — reading as a corrupt manifest and sending the user
  somewhere entirely wrong.

### 3. An abandoned thread is a resource, not a fire-and-forget

A blocking socket read cannot be interrupted from outside, so a stalled fetch is
*abandoned*, never stopped — it keeps running and writing. That has consequences
you must design for, not discover:

- **Give each attempt a unique staging dir.** Under a fixed path, an orphan's
  late writes land in the *next* attempt's tree — after the sha256 pass read it,
  while it is being renamed into place. That is precisely the TOCTOU the verify
  pass exists to close.
- **Never `rmtree` a dir a live orphan owns.** rmtree races the writer, the
  recreated subdir makes the final `rmdir` fail with ENOTEMPTY, and
  `ignore_errors=True` swallows it. Track `(thread, staging)` and reclaim once
  `is_alive()` goes false.
- **Reclaim before the idempotent early return.** A download that succeeds on
  retry returns at `already_installed` forever after, so a sweep placed after
  that check never runs again and the orphan's multi-GB tree is permanent.
- **Never retry past a cancel.** Opening a second multi-GB transfer after the
  user pressed Cancel is worse than the stall it is recovering from.

### 4. Re-check every downstream throttle when call frequency changes

`ModelDownloadJob._should_emit` throttles `/v0/events` on "≥1% delta **OR** ≥0.5s
elapsed". That elapsed branch was harmless while `progress_cb` fired twice per
download. At 4 samples/sec it becomes an event storm during any stall: byte count
unchanged, timer expired, republish the identical payload to every subscriber
twice a second, forever. The guard is value-equality ahead of the time check:

```python
# Unchanged byte count -> never emit. Time is a heartbeat for *slow* progress,
# not for no progress.
if bytes_done == self._last_emit_bytes:
    return False
```

### 5. Pick a timeout from the signal's granularity, not a round number

hf streams in 10 MiB chunks, so the staged count advances once per chunk — ~50s
apart at 200 KB/s. A stall window must clear that *and* the dependency's own
internal retry chain (five retries of a 10s socket timeout plus backoff, ~55s per
file), or it fires on healthy slow links. 300s clears both; the floor it implies
is 10 MiB / 300s ≈ 35 KB/s. State the floor in the comment — a threshold whose
derivation is not written down gets "tuned" later by someone who cannot see it.

Note the dependency's retry budget is **reset by every chunk that arrives**, so a
trickling connection never exhausts it. Do not assume a library's internal retry
cap bounds wall-clock time.

## Why This Matters

A frozen determinate bar is worse than an indeterminate spinner: it does not just
fail to inform, it actively asserts "0% done" — so users cancel work that was
progressing fine.

Both defects hid from tests and from code review because no single component was
wrong. Reviewing `_default_snapshot` in isolation shows a correct download;
reviewing `ModelDownloadJob` shows correct reporting of what it was given. The
defect lives in the *gap* between them.

The sharpest lesson is the second-order one: **the fix for the first defect was
built on an unverified assumption about the dependency, and the test suite could
not falsify it** — because the suite injects a fake `snapshot_fn` that writes to
staging by construction. When a test double defines away the exact behavior your
design depends on, the suite passing tells you nothing about that behavior.

## When to Apply

Reach for this when all of these hold:

- A dependency does long work behind one blocking call and exposes no usable
  progress callback
- The work leaves a measurable trace — files on disk, rows written, objects
  uploaded — **and you have verified, against the real dependency, that the trace
  appears where you intend to measure it**
- Something downstream renders a determinate progress indicator, or the call
  needs a liveness bound

Do **not** reach for it when the library exposes a byte-accurate callback, or when
the operation is short enough that an indeterminate spinner is honest. Prefer an
indeterminate spinner over a determinate bar whenever no real signal exists — an
honest "working" beats a precise-looking lie.

Also reject a library seam that measures the wrong unit. `snapshot_download`
accepts a `tqdm_class`, which looks like the obvious hook; it counts *files*.
This model is 9 files where one weight file is ~99% of the bytes, so a
file-granular bar jumps 0% → 11% → 100%. A seam existing is not a reason to use
it — check what unit it actually reports in.

**Progress reporting stays best-effort**: `progress_cb` failures are swallowed and
must never fail the download. The *stall guard* is deliberately the opposite — it
exists to fail the operation. Keep the two separable; do not let a broken consumer
acquire the power to abort a transfer.

## Examples

Measuring the trace, symlink-safe and tolerant of files vanishing mid-walk as the
SDK renames them into place:

```python
def _staged_bytes(staging: Path) -> int:
    total = 0
    for p in staging.rglob("*"):
        try:
            st = p.lstat()
        except OSError:
            continue  # vanished mid-walk (a rename into place)
        if stat.S_ISREG(st.st_mode):
            total += st.st_size
    return total
```

Pinning the host per call rather than through the environment:

```python
# HF_ENDPOINT is read once at import, so setting it here is inert in a daemon
# that already imported the hub. The pin has to be an argument.
snapshot_download(..., endpoint=_HF_ENDPOINT)
```

## Known limitations

Recorded so a future reader does not assume more rigor than exists:

- The sampled count has an upper clamp but no monotonic floor. A retry after a
  stall genuinely restarts from zero, so the reported progress drops — honest,
  but consumers must tolerate a decreasing `bytes_done`.
- Disabling Xet gives up its chunk-level dedup and parallel transfer for this
  download, in exchange for a signal that exists at all. Revisit if
  `huggingface_hub` ever exposes a byte-accurate callback that spans both paths.
- An abandoned fetch thread is reclaimed only when a later `download_model` call
  runs. If the process exits first, its staging tree persists until the next
  download sweeps it.
- Verified against `huggingface_hub` 0.36.2, with the floor raised to `>=0.23.0`
  (below that, `snapshot_download` filled the global cache and copied at the end,
  which would starve the stall detector the same way Xet does).

Progress shipped in PR #437; stall detection, cancellation, and the Xet fix in
SCR-292.
