"""Process-startup helpers shared by every screencap entry point that
uses :mod:`multiprocessing`.

This module is imported eagerly — BEFORE any ``multiprocessing``
primitive is created — from ``screencap.recorder``,
``screencap.session``, and any other subprocess entry point so that the
``multiprocessing.resource_tracker`` subprocess inherits the correct
``PYTHONWARNINGS`` filter at spawn time.
"""

from __future__ import annotations

import os

_FILTER = "ignore::UserWarning:multiprocessing.resource_tracker"


def suppress_resource_tracker_warnings() -> None:
    """Silence the ``resource_tracker: N leaked semaphore objects`` warning.

    The filter must be in ``os.environ`` BEFORE the resource_tracker
    subprocess is lazily spawned (which happens on the first Queue /
    Event / Value creation), because that subprocess re-initialises the
    ``warnings`` module from the inherited environment. Calling this
    function at module import time — ahead of the first primitive — is
    the only reliable way to make the warning go away.
    """
    existing = os.environ.get("PYTHONWARNINGS", "")
    if _FILTER in existing:
        return
    os.environ["PYTHONWARNINGS"] = f"{existing},{_FILTER}" if existing else _FILTER


def close_queues_safely(*queues: object) -> None:
    """Drain and close multiprocessing queues at shutdown, best-effort.

    Each ``multiprocessing.Queue`` allocates internal POSIX semaphores
    that the resource tracker expects to see released explicitly. The
    sequence is: drain pending messages so the feeder thread has
    nothing left to flush, call ``cancel_join_thread()`` so ``close()``
    doesn't wait on a feeder that's stalled on a dead peer, then
    ``close()`` to release the semaphores.

    The drain loop is bounded so a pathological queue that never
    raises ``Empty`` can't wedge shutdown.
    """
    drain_max = 10_000
    for q in queues:
        if q is None:
            continue
        drained = 0
        try:
            while drained < drain_max:
                q.get_nowait()  # type: ignore[attr-defined]
                drained += 1
        except Exception:
            pass
        try:
            q.cancel_join_thread()  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            q.close()  # type: ignore[attr-defined]
        except Exception:
            pass


# Apply the filter immediately at module import so callers only need to
# ``import screencap._startup`` to get the effect.
suppress_resource_tracker_warnings()
