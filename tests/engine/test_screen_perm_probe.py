"""Tests for the fresh-process Screen-Recording TCC probe (SCR-106).

``probe_screen_recording_granted`` reads ``CGPreflightScreenCaptureAccess`` in a
*fresh* multiprocessing child so the value dodges the long-running worker's
per-process TCC cache. The tri-state contract is the load-bearing part: a
spawn/timeout/bridge failure MUST map to ``None`` ("couldn't determine"), never
to ``False`` ("denied") — otherwise a transient spawn hiccup would tear down a
healthy recording. These tests pin the mapping without actually spawning.
"""

from __future__ import annotations

from unittest import mock


class _FakeProc:
    def __init__(
        self, start_error: Exception | None = None, alive_until_kill: bool = False
    ) -> None:
        self._start_error = start_error
        self.started = False
        # When True, the child reports alive through terminate() and only dies
        # after kill() — exercises the reap escalation.
        self._alive = False
        self._alive_until_kill = alive_until_kill
        self.terminate_calls = 0
        self.kill_calls = 0

    def start(self) -> None:
        if self._start_error is not None:
            raise self._start_error
        self.started = True
        self._alive = self._alive_until_kill

    def join(self, timeout: float | None = None) -> None:
        pass

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self.terminate_calls += 1
        # A wedged child ignores SIGTERM; stays alive until kill().

    def kill(self) -> None:
        self.kill_calls += 1
        self._alive = False


class _FakeQueue:
    def __init__(self, value=None, raises: Exception | None = None) -> None:
        self._value = value
        self._raises = raises

    def get(self, timeout: float | None = None):
        if self._raises is not None:
            raise self._raises
        return self._value


class _FakeCtx:
    def __init__(self, queue: _FakeQueue, proc: _FakeProc) -> None:
        self._queue = queue
        self._proc = proc

    def Queue(self):  # noqa: N802 — mirrors multiprocessing API
        return self._queue

    def Process(self, *args, **kwargs):  # noqa: N802
        return self._proc


def _patched_probe(*, value=None, raises=None, start_error=None):
    """Run ``probe_screen_recording_granted`` with a fully faked mp context."""
    from screencap.engine import _screen_perm_probe

    ctx = _FakeCtx(_FakeQueue(value=value, raises=raises), _FakeProc(start_error))
    with mock.patch("multiprocessing.get_context", return_value=ctx):
        return _screen_perm_probe.probe_screen_recording_granted(timeout=0.01)


def test_probe_returns_true_when_child_reports_granted():
    assert _patched_probe(value=True) is True


def test_probe_returns_false_when_child_reports_denied():
    assert _patched_probe(value=False) is False


def test_probe_returns_none_when_child_reports_bridge_error():
    """Child puts ``None`` on a PyObjC failure → fail-open None."""
    assert _patched_probe(value=None) is None


def test_probe_returns_none_on_queue_timeout():
    """A child that never answers (timeout) is 'couldn't determine', not denied."""
    import queue as _queue

    assert _patched_probe(raises=_queue.Empty()) is None


def test_probe_returns_none_on_spawn_failure():
    """A failed ``Process.start()`` maps to None, never to a false revocation."""
    assert _patched_probe(value=True, start_error=OSError("spawn failed")) is None


def test_probe_returns_none_when_get_context_unavailable():
    """Even ``get_context`` raising must fail open to None."""
    from screencap.engine import _screen_perm_probe

    with mock.patch("multiprocessing.get_context", side_effect=RuntimeError("boom")):
        assert _screen_perm_probe.probe_screen_recording_granted(timeout=0.01) is None


def test_probe_non_bool_payload_maps_to_none():
    """Any non-bool payload (e.g. a stray error tuple) is treated as unknown."""
    assert _patched_probe(value=("ERR", "weird")) is None


def test_probe_escalates_to_kill_for_wedged_child():
    """A child that survives terminate() is SIGKILL'd so probes can't pile up
    orphans over a long recording (the probe runs every ~20s for hours)."""
    import queue as _queue

    from screencap.engine import _screen_perm_probe

    proc = _FakeProc(alive_until_kill=True)
    ctx = _FakeCtx(_FakeQueue(raises=_queue.Empty()), proc)
    with mock.patch("multiprocessing.get_context", return_value=ctx):
        result = _screen_perm_probe.probe_screen_recording_granted(timeout=0.01)

    assert result is None  # timeout → fail-open
    assert proc.terminate_calls == 1
    assert proc.kill_calls == 1  # escalated because terminate didn't reap
