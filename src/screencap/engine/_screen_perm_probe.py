"""Frozen-safe, cache-immune Screen-Recording TCC probe (SCR-106).

macOS caches a process's TCC answer for its lifetime, so an in-process
``CGPreflightScreenCaptureAccess()`` call inside the long-running recording
worker returns the value captured at worker *start* — never a mid-recording
revocation (see
``docs/solutions/runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md``).
The only robust live read is to do the preflight in a *fresh* process.

The CLI's ``_check_permission_fresh`` spawns ``[sys.executable, "-c", code]``,
which works in dev but is broken in the frozen daemon: the bundled binary's
Click entry point rejects ``-c`` (SCR-69). ``multiprocessing`` spawn is the
frozen-safe equivalent — it is the same machinery the recording worker and its
reader/writer children are already spawned with — and a spawned (not forked)
child is a brand-new process image, so it does a fresh TCC lookup with no
inherited cache.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import multiprocessing


def _probe_child(q: multiprocessing.Queue[bool | None]) -> None:
    """Run in the spawned child: put ``True``/``False``/``None`` on ``q``.

    ``None`` signals "couldn't determine" (PyObjC import/bridge error) — the
    parent treats it as fail-open, never as denied. NOTE: this deliberately
    calls ``Quartz`` directly rather than ``DarwinPlatform.is_screen_recording_enabled()``
    — that helper returns ``True`` on an import error ("assume enabled"), which
    would silently map an inconclusive probe to *granted* and break the
    tri-state fail-open contract this probe exists to provide.
    """
    try:
        import Quartz

        q.put(bool(Quartz.CGPreflightScreenCaptureAccess()))
    except Exception:
        q.put(None)


def probe_screen_recording_granted(timeout: float = 2.0) -> bool | None:
    """Read Screen-Recording TCC state in a fresh process. Tri-state.

    Returns:
        ``True``  — fresh probe says granted.
        ``False`` — fresh probe says denied.
        ``None``  — couldn't determine (spawn failure, timeout, or a PyObjC
                    bridge error in the child). Callers MUST treat ``None`` as
                    fail-open ("don't know"), never as a revocation — otherwise
                    a transient spawn hiccup would tear down a healthy recording.

    Bounded: this runs synchronously on the recording supervisor loop, so the
    total wall-time is hard-capped (``timeout`` for the read, then short reap
    joins escalating to SIGKILL) to keep the loop responsive to stop/SIGTERM.
    The normal read is ~0.2 s; the cap only bites if the child wedges.
    """
    import multiprocessing as mp

    p = None
    q = None
    try:
        ctx = mp.get_context("spawn")
        q = ctx.Queue()
        p = ctx.Process(target=_probe_child, args=(q,), daemon=True)
        p.start()
        val = q.get(timeout=timeout)
    except Exception:
        val = None
    finally:
        # Reap the child and release the Queue's semaphores/feeder thread — this
        # runs on a loop for the whole recording, so a per-call leak or an
        # un-reaped child would accumulate. Escalate term→kill so a child wedged
        # in a Quartz/TCC syscall can't outlive the probe and pile up orphans.
        if p is not None:
            try:
                p.join(timeout=0.5)
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=0.5)
                if p.is_alive():
                    p.kill()
                    p.join(timeout=0.5)
            except Exception:
                pass
        if q is not None:
            try:
                q.close()
                q.join_thread()
            except Exception:
                pass
    return val if isinstance(val, bool) else None
