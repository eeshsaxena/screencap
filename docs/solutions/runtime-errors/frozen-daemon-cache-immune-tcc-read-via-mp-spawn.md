---
title: "Frozen-daemon cache-immune TCC reads: multiprocessing-spawn, not in-process Quartz or `python -c`"
slug: frozen-daemon-cache-immune-tcc-read-via-mp-spawn
date: 2026-06-24
category: runtime-errors
severity: medium
problem_type: runtime-behavior
modules:
  - src/screencap/engine/permission_policy.py
  - src/screencap/engine/_screen_perm_probe.py
  - src/screencap/session.py
  - src/screencap/recorder.py
tags:
  - macos
  - tcc
  - permissions
  - multiprocessing
  - frozen-binary
  - daemon
  - screen-recording
symptoms:
  - "Mid-recording Screen Recording revocation on the daemon/shell path is not detected — the recording keeps running and captures nothing useful"
  - "An in-process CGPreflightScreenCaptureAccess() inside the recording worker keeps returning the granted-at-start value after the user revokes"
  - "The capture-health labeller reports the revocation as advisory capture_unhealthy instead of terminal permission_lost"
root_cause: >
  macOS caches a process's TCC answer for the process lifetime, so the
  long-running recording worker's in-process Quartz read is pinned to the
  granted-at-start value. The CLI's fresh-subprocess workaround
  (`[sys.executable, "-c", code]`) is broken in the frozen daemon because the
  bundled binary's Click entry point rejects `-c`. The only frozen-safe,
  cache-immune live read is to do the preflight in a fresh `multiprocessing`
  spawn child.
---

# Frozen-daemon cache-immune TCC reads: multiprocessing-spawn, not in-process Quartz or `python -c`

## Problem

On the daemon / SwiftUI-shell recording path, the engine worker ran with the
`Noop` permission policy, so a genuine **mid-recording** Screen Recording
revocation had no robust teardown. Two independent live-read mechanisms that
work elsewhere both fail inside the frozen daemon worker, so neither the
capture-health labeller nor any poll could see the revocation. (SCR-106.)

## Symptoms

- User revokes Screen Recording mid-recording in the macOS app → the recording
  keeps running, capturing only a desktop/wallpaper frame, with no `stop()`.
- Repeated in-process `CGPreflightScreenCaptureAccess()` returns the stale
  granted value captured at worker start.
- The capture-health supervisor emits the advisory `capture_unhealthy` (or
  nothing), never the terminal `permission_lost`.

## What Didn't Work

- **In-process Quartz (`CGPreflightScreenCaptureAccess`) as a live detector.**
  macOS caches TCC state per process for the process lifetime (see the sibling
  doc on the per-process cache). The recording worker is long-lived, so its
  in-process read is pinned to the granted-at-start answer. This is fine for
  *attribution after* an independently-observed symptom, but useless as a live
  mid-recording detector.
- **The CLI's fresh-subprocess probe `[sys.executable, "-c", code]`
  (`_check_permission_fresh` in `src/screencap/recorder.py`).** It dodges the
  cache by spawning a fresh `python3`, which works in dev — but in the **frozen
  daemon** `sys.executable` is the bundled binary, whose Click entry point
  rejects `-c` with a UsageError. The probe returns `None` (couldn't determine)
  and the gate is a no-op (this is the SCR-69 / SCR-76 frozen-binary trap).
- **Frame-content heuristics** (None-check, dhash-static check) to detect a
  denied screen reader. Unreliable: under denial `screencapture` may return
  `None`, a black frame, OR a *changing* wallpaper/backdrop frame, so the
  capture-health attempt-vs-output gap may never open. Documented as a deferred
  SCR-76 limitation.

## Solution

Do the live TCC read in a **fresh `multiprocessing` spawn child**. A spawned
(not forked) child is a brand-new process image, so it does a fresh TCC lookup
with no inherited cache — and `multiprocessing` is the *same* machinery the
worker and its reader/writer children are already spawned with, so it works
identically in dev and in the frozen bundle (no `-c`, no new CLI subcommand).

`src/screencap/engine/_screen_perm_probe.py` — the probe:

```python
def _probe_child(q) -> None:
    try:
        import Quartz  # NOT DarwinPlatform.is_screen_recording_enabled():
        # that helper returns True on ImportError ("assume enabled"), which
        # would break the fail-open tri-state contract below.
        q.put(bool(Quartz.CGPreflightScreenCaptureAccess()))
    except Exception:
        q.put(None)

def probe_screen_recording_granted(timeout: float = 2.0) -> bool | None:
    import multiprocessing as mp
    p = q = None
    try:
        ctx = mp.get_context("spawn")            # spawn, NOT fork (fork inherits cache)
        q = ctx.Queue()
        p = ctx.Process(target=_probe_child, args=(q,), daemon=True)
        p.start()
        val = q.get(timeout=timeout)
    except Exception:
        val = None
    finally:
        # bounded reap: term -> kill; close the queue (runs on a loop for the
        # whole recording, so an un-reaped child or queue would accumulate)
        if p is not None:
            try:
                p.join(timeout=0.5)
                if p.is_alive(): p.terminate(); p.join(timeout=0.5)
                if p.is_alive(): p.kill(); p.join(timeout=0.5)
            except Exception:
                pass
        if q is not None:
            try:
                q.close(); q.join_thread()
            except Exception:
                pass
    return val if isinstance(val, bool) else None   # non-bool / error -> None (fail-open)
```

`FreshScreenWatch` (`src/screencap/engine/permission_policy.py`) wraps it as a
`PermissionPolicy` and is wired into the worker in place of `Noop`
(`src/screencap/session.py`). Its `poll()` runs the probe on a slow cadence and
raises `PermissionRevoked("screen_recording")` on a **debounced explicit
denial**, reusing the existing `_run_screen_recorder` teardown
(`permission_lost` + `stop()`). `preflight()` is a no-op — startup TCC stays the
worker's own fail-fast check.

## Why This Works

- **Cache-immune:** a fresh process performs a fresh TCC DB lookup; there is no
  per-process cache to be stale.
- **Frozen-safe:** `multiprocessing` spawn re-executes the bundled binary the
  way the worker's own reader/writer children already do — no `-c`, no extra
  entry point. (Empirically the spawned child reads the correct *live* grant in
  ~0.2 s.)
- **Content-independent:** it reads the permission directly and never inspects a
  captured frame, so the wallpaper-frame ambiguity that defeats the
  capture-health detector is irrelevant.

## Prevention

- **For any mid-recording TCC check in the daemon, reach for a fresh
  `multiprocessing` spawn probe — not in-process Quartz (stale) and not
  `[sys.executable, "-c"]` (broken in the frozen binary).**
- **Fail-open is mandatory.** Map any spawn/timeout/PyObjC error to `None`
  ("couldn't determine"), debounce explicit denials, and only act on a clean
  `False`. A transient hiccup must never tear down a healthy recording. Pinned
  by `tests/engine/test_screen_perm_probe.py` and
  `tests/engine/test_permission_policy.py`.
- **Bound the probe.** It runs synchronously on the supervisor loop, so hard-cap
  the read timeout and escalate the reap to SIGKILL — otherwise a wedged child
  blocks stop/SIGTERM responsiveness and orphans accumulate over a long
  recording.
- **Use `get_context("spawn")` explicitly.** A forked child would inherit the
  parent's cached TCC answer and defeat the entire mechanism.

## Related

- `docs/solutions/runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md`
  — the per-process TCC cache root cause and the SwiftUI shell's `Quit &
  Relaunch` workaround. This doc is its Python-daemon sibling: same root cause,
  different (engine-side) solution.
- SCR-106 (this fix), SCR-76 (capture-health detection + the deferred
  mid-recording gap), SCR-69 (frozen-binary `-c` probe trap).
