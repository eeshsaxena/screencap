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
applies_when:
  - "A third-party SDK call blocks for a long time with no progress callback"
  - "A UI renders a determinate progress bar from a value the backend only updates at the endpoints"
  - "Adding a sampler that feeds an already-throttled event publisher"
---

# Progress for a blocking SDK call that has no progress callback

## Context

`screencap model download` fetches a ~1.7 GB model through one blocking call to
`huggingface_hub.snapshot_download`. That call offers no byte-level callback, so
the engine reported progress only at its two endpoints — `progress_cb(0, total)`
before the fetch (`src/screencap/models/download.py:296`) and
`progress_cb(total, total)` after both the transfer and the sha256 verification
(`:340`).

Everything downstream was correct and still useless. The daemon job faithfully
reported `bytes_done=0`, the CLI faithfully printed 0%, and SwiftUI faithfully
rendered `ProgressView(value: 0.0)`. A healthy multi-minute download was
pixel-identical to a hang, and a QA tester reported it as stuck and skipped it.

The general shape: **when the only progress signal is a function that does not
call you back, a determinate progress UI is lying by construction.** Every layer
can be individually correct while the composed system is indistinguishable from
frozen.

## Guidance

**Sample the observable side effect, not the library's internals.**

The fetch writes files to a staging directory. That directory is the progress
signal — measurable from outside the SDK, on any version, and identically under
a test double.

```python
# src/screencap/models/download.py
@contextmanager
def _progress_sampler(staging, total, progress_cb, interval=_PROGRESS_SAMPLE_INTERVAL_S):
    if progress_cb is None:
        yield
        return
    done = threading.Event()

    def _sample() -> None:
        while not done.wait(interval):
            try:
                staged = min(_staged_bytes(staging), total)
                # Re-check after the walk: `done` may have been set while we were
                # measuring, and the join below is bounded.
                if done.is_set():
                    return
                progress_cb(staged, total)
            except Exception:  # a broken consumer must never fail the download
                log.debug("model download progress sample failed", exc_info=True)

    thread = threading.Thread(target=_sample, name="model-download-progress", daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join(timeout=2.0)
        if thread.is_alive():
            log.warning("model download progress sampler did not stop within 2s")
```

Wrapping only the blocking call keeps the change tiny:

```python
with _progress_sampler(staging, total, progress_cb):
    snapshot(variant.repo, variant.revision, staging, filenames)
```

Three details carry most of the value:

**1. Reject the library's own seam when it measures the wrong thing.**
`snapshot_download` does accept a `tqdm_class`, which looks like the obvious
hook. It counts *files*. This model is 9 files where one weight file is ~99% of
the bytes, so a file-granular bar jumps 0% -> 11% -> 100% and is barely better
than no bar. A seam existing is not a reason to use it — check what unit it
actually reports in.

**2. Order the shutdown so a stale sample cannot overtake the terminal one.**
The sampler re-checks its stop flag *after* measuring and *before* reporting
(`:149`). Without that, a reading computed before `os.replace(staging, target)`
could be delivered after the caller's terminal 100%, dragging the reported count
backwards. A bounded `join` (`:161`) cannot carry this guarantee alone — it is
allowed to give up. Prefer three cheap cooperating guarantees (stop-flag
re-check, bounded join, slow work between them) over one comment asserting an
invariant the code does not enforce.

**3. Re-check every downstream throttle when call frequency changes.**
`ModelDownloadJob._should_emit` throttles `/v0/events` publishes on
"≥1% delta **OR** ≥0.5s elapsed" (`src/screencap/daemon/model_download_job.py:62-63`).
That elapsed-time branch was harmless while `progress_cb` fired twice per
download. At 4 samples/sec it becomes an event storm during any stall:
byte count unchanged, timer expired, republish the identical payload to every
subscriber twice a second, forever. The fix is a value-equality guard ahead of
the time check (`:212`):

```python
# Unchanged byte count -> never emit. Time is a heartbeat for *slow* progress,
# not for no progress.
if bytes_done == self._last_emit_bytes:
    return False
```

## Why This Matters

A frozen determinate bar is worse than an indeterminate spinner: it does not
just fail to inform, it actively asserts "0% done" — so users cancel work that
was progressing fine. That is what happened here.

The failure also hides from tests and from code review, because no single
component is wrong. Reviewing `_default_snapshot` in isolation shows a correct
download; reviewing `ModelDownloadJob` shows correct reporting of what it was
given. The defect lives in the *gap* between them, which is why it survived to
a QA report.

The event-storm consequence generalizes past this feature: a throttle written
for one call frequency encodes an assumption about that frequency. Raising the
frequency without revisiting the throttle silently converts "rate limit" into
"minimum publish rate."

## When to Apply

Reach for this when all of these hold:

- A dependency does long work behind one blocking call and exposes no usable
  progress callback
- The work leaves a measurable trace — files on disk, rows written, objects
  uploaded
- Something downstream renders a determinate progress indicator

Do **not** reach for it when the library exposes a byte-accurate callback, or
when the operation is short enough that an indeterminate spinner is honest. And
prefer an indeterminate spinner over a determinate bar whenever no real
signal exists — an honest "working" beats a precise-looking lie.

The sampler is best-effort by construction: it swallows its own errors and never
fails the underlying operation. Keep that property. Progress reporting is an
observability feature and must never be able to break the thing it observes.

## Examples

Before — progress reported only at the endpoints, silence in between:

```python
progress_cb(0, total)
snapshot(variant.repo, variant.revision, staging, filenames)   # minutes
# ... sha256 verification of 1.7 GB ...
progress_cb(total, total)
```

After — the same endpoints, with the gap filled by disk sampling:

```python
progress_cb(0, total)
with _progress_sampler(staging, total, progress_cb):
    snapshot(variant.repo, variant.revision, staging, filenames)
# ... sha256 verification ...
progress_cb(total, total)
```

Measuring the trace, symlink-safe and tolerant of files vanishing mid-walk as
the SDK renames them into place (`:90`):

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

## Known limitations

Recorded so a future reader does not assume more rigor than exists:

- The sampled count has an upper clamp but no monotonic floor, so if the SDK
  ever shrank bytes-on-disk mid-fetch the bar could move backwards.
- `_staged_bytes` counts once across the `.incomplete` -> final rename because
  huggingface_hub renames rather than copies. Verified against 0.36.2 only;
  `pyproject.toml` still floors the dependency at `>=0.20.0`.
- Sampling does not make a genuine stall *recoverable* — it only makes it
  visible. Stall detection is tracked separately in SCR-292.

Shipped in PR #437.
